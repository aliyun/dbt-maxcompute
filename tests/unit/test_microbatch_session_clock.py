"""Which clock the batch window is stated on, decided without a server.

dbt-core computes every boundary in UTC. MaxCompute parses a timestamp string on the **session**
timezone, while the partitions a batch overwrites are cut by `trunc_time()` on **UTC**. The window
and the partition key are therefore only the same length of time when the boundary is written the
way the session will read it back. `docs/microbatch-support.md` carries the measurements; this file
pins the rendering that those measurements led to.

The cases are picked to fail for the reasons this class of bug actually happens:

* a **positive** offset alone cannot tell "aligned" from "aligned and eight hours off", because a
  shift with the wrong sign still keeps adjacent windows the same width - hence the negative case;
* a zone is judged **at the boundary's own instant** - `Europe/London` in January and July is
  pinned in `test_microbatch_datetime_boundary_text.py`, where the suffix property is decided;
* a UTC session must come out as the *same text* dbt already produces apart from the offset suffix
  (#40 dropped that suffix), so nothing on the adapter's default profile moves;
* where this cannot state the session clock at all, dbt-core's rendering is returned untouched:
  an unverified shift is worse than the known behaviour, and it is the known behaviour that the
  integration suite still pins in that case.
"""

from contextlib import contextmanager
from datetime import datetime
from unittest import mock

import pytest
import pytz
from dbt.adapters.base.relation import EventTimeFilter
from dbt_common.exceptions import DbtRuntimeError
from odps import options

from dbt.adapters.maxcompute import relation as relation_module
from dbt.adapters.maxcompute.relation import MaxComputeRelation, _unquoted_field_name

UTC_DAY_START = datetime(2025, 5, 1, 0, 0, 0, tzinfo=pytz.UTC)
UTC_DAY_END = datetime(2025, 5, 2, 0, 0, 0, tzinfo=pytz.UTC)


@pytest.fixture
def session_clock():
    """Let a test pin pyodps' session timezone, then put the global back.

    The adapter assigns this per connection (`MaxComputeCredentials._get_odps`), and pyodps reads it
    when it builds every statement's settings, so leaving a zone behind would silently change the
    session clock of the tests that run after this one.
    """
    saved = options.local_timezone
    yield
    options.local_timezone = saved


@contextmanager
def _warehouse_types(types):
    """State what the warehouse would answer for this relation's columns.

    The unit cases here are about which clock a boundary is stated on *given* a type, so the
    lookup is pinned instead of reaching for a server. `types={}` is the honest way to say
    "cannot be read" (a relation that does not exist yet, a connection that cannot describe it);
    the real `ref()` / `source()` path, where the answer comes from the warehouse, is pinned in
    `tests/functional/adapter/incremental/test_microbatch_partition_timezone.py`.
    """
    with mock.patch.object(relation_module, "physical_column_types", lambda relation: dict(types)):
        yield


def _render(
    start=UTC_DAY_START,
    end=UTC_DAY_END,
    field="event_time",
    column_type="timestamp",
    declared_type=None,
):
    types = {} if column_type is None else {_unquoted_field_name(field): column_type}
    relation = MaxComputeRelation.create(
        identifier="microbatch_model", event_field_declared_type=declared_type
    )
    with _warehouse_types(types):
        return relation._render_event_time_filtered(
            EventTimeFilter(field_name=field, start=start, end=end)
        )


def _window(start_text, end_text, field="event_time"):
    return f"{field} >= '{start_text}' and {field} < '{end_text}'"


class TestShiftedSessions:
    """A profile that pins another zone gets that zone's reading of the same instant."""

    @pytest.mark.parametrize(
        "zone,start_text,end_text",
        [
            # +08:00: the UTC boundary 2025-05-01 00:00 is 08:00 on this session's wall clock.
            ("Asia/Shanghai", "2025-05-01 08:00:00", "2025-05-02 08:00:00"),
            # POSIX `Etc/GMT+8` is UTC-08:00, so the reading is the previous day's 16:00. A shift
            # applied with the wrong sign would still produce two windows of one day each.
            ("Etc/GMT+8", "2025-04-30 16:00:00", "2025-05-01 16:00:00"),
            # Half-hour offsets are not rounded to the hour.
            ("Asia/Kolkata", "2025-05-01 05:30:00", "2025-05-02 05:30:00"),
            # 2025-05-01 is EDT (-04:00) in New York: the shift follows the date, not the zone name.
            ("America/New_York", "2025-04-30 20:00:00", "2025-05-01 20:00:00"),
        ],
    )
    def test_the_boundary_is_the_sessions_own_reading(
        self, zone, start_text, end_text, session_clock
    ):
        options.local_timezone = zone
        assert _render() == _window(start_text, end_text)

    def test_hour_granularity_shifts_with_the_instant_not_the_day(self, session_clock):
        """A batch boundary that is not midnight still lands on the session's hour."""
        options.local_timezone = "Asia/Shanghai"
        assert _render(
            start=datetime(2025, 5, 1, 9, tzinfo=pytz.UTC),
            end=datetime(2025, 5, 1, 10, tzinfo=pytz.UTC),
        ) == _window("2025-05-01 17:00:00", "2025-05-01 18:00:00")

    def test_fractional_seconds_survive_the_shift(self, session_clock):
        options.local_timezone = "Asia/Shanghai"
        assert _render(
            end=datetime(2025, 5, 2, 0, 0, 0, 123456, tzinfo=pytz.UTC),
        ) == _window("2025-05-01 08:00:00", "2025-05-02 08:00:00.123456")

    def test_one_sided_windows_shift_on_both_ends(self, session_clock):
        options.local_timezone = "Asia/Shanghai"
        assert _render(end=None) == "event_time >= '2025-05-01 08:00:00'"
        assert _render(start=None) == "event_time < '2025-05-02 08:00:00'"

    def test_a_quoted_field_name_is_carried_through_the_shift(self, session_clock):
        options.local_timezone = "Asia/Shanghai"
        assert "`event time` >= '2025-05-01 08:00:00'" in _render(field="`event time`")

    def test_the_wrapped_relation_gets_the_shifted_window(self, session_clock):
        """What actually reaches the warehouse is the subquery, not the bare predicate."""
        options.local_timezone = "Asia/Shanghai"
        relation = MaxComputeRelation.create(
            identifier="microbatch_model",
            event_time_filter=EventTimeFilter(
                field_name="event_time", start=UTC_DAY_START, end=UTC_DAY_END
            ),
        )
        with _warehouse_types({"event_time": "timestamp"}):
            assert _window(
                "2025-05-01 08:00:00", "2025-05-02 08:00:00"
            ) in relation.render_event_time_filtered("`microbatch_model`")


class TestUtcSessionsAreNotMoved:
    """The adapter's own default: no `timezone` in the profile, so pyodps submits `Etc/GMT`."""

    @pytest.mark.parametrize("pinned", [False, "", 0])
    def test_a_falsey_local_timezone_reads_the_utc_wall_clock(self, pinned, session_clock):
        options.local_timezone = pinned
        assert _render() == _window("2025-05-01 00:00:00", "2025-05-02 00:00:00")

    @pytest.mark.parametrize(
        "name", ["UTC", "Etc/GMT", "GMT", "Etc/UTC", "Z", "+00:00", "Etc/GMT+0", "Etc/GMT-0"]
    )
    def test_a_zero_offset_zone_name_states_the_same_instant(self, name, session_clock):
        options.local_timezone = name
        assert _render() == _window("2025-05-01 00:00:00", "2025-05-02 00:00:00")


class TestSessionsThatCannotBeStatedKeepDbtsRendering:
    """Where the clock is unknowable, neither half of the rule is applied - not even one of them.

    The other cases of this fallback (nothing pinned, a value that is not a zone name) are pinned in
    `test_microbatch_datetime_boundary_text.py`; what matters *here* is that a session which cannot
    be stated does not get a half-applied correction: the window keeps dbt-core's offset-bearing
    text, which is the behaviour the integration suite and `docs/microbatch-support.md` describe.
    """

    @pytest.mark.parametrize("name", ["Not/AZone", "Etc/Whatever", "Asia/Shangai"])
    def test_an_unknown_zone_name_is_not_guessed_at(self, name, session_clock):
        options.local_timezone = name
        assert _render() == _window(f"{UTC_DAY_START}", f"{UTC_DAY_END}")

    def test_the_offset_suffix_survives_exactly_where_the_shift_cannot_be_made(
        self, session_clock
    ):
        options.local_timezone = "Not/AZone"
        assert "+00:00" in _render()


class TestDSTFoldWindowIsRefused:
    """A fold hour cannot be stated as a half-open wall-clock window, so the batch is refused.

    Measured on a real project (2025-11-02, a session that pinned `America/New_York`): the UTC
    boundary 05:00Z and the boundary 06:00Z are both ``01:00:00`` on that wall clock, the server
    resolves ``01:00:00`` to the earlier instant, and the 05:00Z batch selects nothing. Writing that
    anyway is a batch that changes no rows and reports success, and whether the folded hour is
    covered by a neighbour depends on the zone - so a warning was not enough: no statement is
    submitted at all.
    """

    def test_the_fold_hour_is_refused_before_anything_is_written(self, session_clock):
        options.local_timezone = "America/New_York"
        with pytest.raises(DbtRuntimeError) as refused:
            _render(
                start=datetime(2025, 11, 2, 5, tzinfo=pytz.UTC),
                end=datetime(2025, 11, 2, 6, tzinfo=pytz.UTC),
            )
        message = str(refused.value)
        assert "2025-11-02 01:00:00" in message
        assert "refuses to write" in message
        assert "fixed-offset" in message

    def test_the_other_zones_fold_collapses_the_same_way(self, session_clock):
        """Sydney's fold repeats a *different* local hour, and is refused the same way.

        Which side of the fold the server resolves the repeated text to differs by zone (measured:
        New York takes the earlier instant, Sydney the later one), so the refusal is the half of
        this behaviour that is stateable without a server - and it is the half that matters,
        because the batch is not submitted in either zone.
        """
        options.local_timezone = "Australia/Sydney"
        with pytest.raises(DbtRuntimeError) as refused:
            _render(
                start=datetime(2025, 4, 5, 15, tzinfo=pytz.UTC),
                end=datetime(2025, 4, 5, 16, tzinfo=pytz.UTC),
            )
        assert "2025-04-06 02:00:00" in str(refused.value)

    def test_a_naive_field_is_not_refused_on_a_fold_day(self, session_clock):
        """The fold only collapses text that was moved: a naive field is stated on UTC either way.

        This is the case a blanket "a non-UTC session is unsafe" rule would have failed - here the
        two boundaries stay distinct texts and both batches write their own rows.
        """
        options.local_timezone = "America/New_York"
        assert _render(
            start=datetime(2025, 11, 2, 5, tzinfo=pytz.UTC),
            end=datetime(2025, 11, 2, 6, tzinfo=pytz.UTC),
            column_type="timestamp_ntz",
        ) == _window("2025-11-02 05:00:00", "2025-11-02 06:00:00")

    def test_a_normal_hour_is_not_refused(self, session_clock):
        options.local_timezone = "America/New_York"
        assert _render(
            start=datetime(2025, 11, 2, 7, tzinfo=pytz.UTC),
            end=datetime(2025, 11, 2, 8, tzinfo=pytz.UTC),
        ) == _window("2025-11-02 02:00:00", "2025-11-02 03:00:00")


class TestWhichTypeDecidesTheClock:
    """The field's own physical type on the relation being read is the authority.

    Measured on the real `ref()` path (see `docs/microbatch-support.md`): dbt-core hands the
    adapter only a field name and two UTC instants - no type - while the upstream *node* is passed
    to `create_from`, which is where a declaration can be picked up. The warehouse answer wins
    because it is what the server will compare on; a YAML declaration is used only when that answer
    is unavailable, and neither answer on a moved session is a refusal, not a coin toss.
    """

    def test_aliases_and_spelling_variants_are_the_same_type(self):
        from dbt.adapters.maxcompute.relation import clock_kind_of

        for raw in ("TIMESTAMP", " timestamp ", "DATETIME"):
            assert clock_kind_of(raw) == "instant", raw
        for raw in ("TIMESTAMP_NTZ", "timestamp-ntz", "timestamp ntz"):
            assert clock_kind_of(raw) == "naive", raw
        for raw in ("date", "string", "bigint", "", None):
            assert clock_kind_of(raw) == "unknown", raw

    @pytest.mark.parametrize("column_type", ["timestamp", "datetime"])
    def test_clock_bearing_fields_get_the_session_reading(self, column_type, session_clock):
        options.local_timezone = "Asia/Shanghai"
        assert _render(column_type=column_type) == _window(
            "2025-05-01 08:00:00", "2025-05-02 08:00:00"
        )

    @pytest.mark.parametrize("column_type", ["timestamp_ntz", "TIMESTAMP_NTZ"])
    def test_naive_fields_keep_the_utc_reading(self, column_type, session_clock):
        """The shift is what loses rows for a field with no clock - so it is not applied to one."""
        options.local_timezone = "Asia/Shanghai"
        assert _render(column_type=column_type) == _window(
            "2025-05-01 00:00:00", "2025-05-02 00:00:00"
        )

    def test_naive_fields_still_lose_the_offset_suffix(self, session_clock):
        """No shift is not the same as no change: dbt-core's `+00:00` text still cannot be used."""
        options.local_timezone = "Asia/Shanghai"
        text = _render(column_type="timestamp_ntz")
        assert "+00:00" not in text
        assert text == _window("2025-05-01 00:00:00", "2025-05-02 00:00:00")

    def test_a_negative_offset_session_moves_naive_fields_nothing_at_all(self, session_clock):
        options.local_timezone = "America/New_York"
        assert _render(column_type="timestamp_ntz") == _window(
            "2025-05-01 00:00:00", "2025-05-02 00:00:00"
        )

    def test_the_declaration_is_used_only_when_the_warehouse_cannot_answer(self, session_clock):
        """Physical `timestamp` declared `timestamp_ntz` gets the shift: the warehouse outranks YAML.

        Measured pair on the real path - an upstream column that is physically `timestamp` declared
        `timestamp_ntz` in its property file, on a Shanghai session. Trusting the declaration would
        have skipped the shift and reproduced the row loss this branch exists to fix.
        """
        options.local_timezone = "Asia/Shanghai"
        assert _render(column_type="timestamp", declared_type="timestamp_ntz") == _window(
            "2025-05-01 08:00:00", "2025-05-02 08:00:00"
        )
        assert _render(column_type=None, declared_type="timestamp_ntz") == _window(
            "2025-05-01 00:00:00", "2025-05-02 00:00:00"
        )
        assert _render(column_type=None, declared_type="timestamp") == _window(
            "2025-05-01 08:00:00", "2025-05-02 08:00:00"
        )

    def test_an_unverifiable_field_is_refused_on_a_session_that_would_move_it(self, session_clock):
        options.local_timezone = "Asia/Shanghai"
        with pytest.raises(DbtRuntimeError) as refused:
            _render(column_type=None, declared_type=None)
        message = str(refused.value)
        assert "could not be read" in message
        assert "Asia/Shanghai" in message
        assert "guessed" in message

    def test_the_same_unverifiable_field_is_fine_on_a_utc_session(self, session_clock):
        """Nothing is being chosen on a UTC session: both readings are the same text, so no refusal."""
        options.local_timezone = False
        assert _render(column_type=None, declared_type=None) == _window(
            "2025-05-01 00:00:00", "2025-05-02 00:00:00"
        )

    def test_a_date_field_is_refused_instead_of_comparing_to_null_forever(self, session_clock):
        """`date` event columns cannot answer the clock question either.

        Comparing a `DATE` with a timestamp string yields NULL on MaxCompute, so no row falls in
        any window - a separate limitation, scoped out here; what changed is that it is no longer
        silent.

        Before this change, a `date` field on a shifted session produced a window that matched
        nothing and reported success.
        """
        options.local_timezone = "Asia/Shanghai"
        with pytest.raises(DbtRuntimeError) as refused:
            _render(column_type="date")
        assert "date" in str(refused.value)

    def test_a_quoted_field_name_is_matched_against_the_warehouse_answer(self, session_clock):
        options.local_timezone = "Asia/Shanghai"
        assert _render(field="`Event Time`", column_type="timestamp_ntz") == _window(
            "2025-05-01 00:00:00", "2025-05-02 00:00:00", field="`Event Time`"
        )

    def test_an_ephemeral_upstream_falls_to_the_declaration_or_refuses(self, session_clock):
        """A CTE has no relation to describe, so the physical answer cannot be read at all.

        `ref()`ing an ephemeral model between the event column and the microbatch model is the one
        shape where the warehouse cannot answer even though the run is healthy. The declared type is
        then the only signal and is used as such (logged unverified); with no declaration either, the
        window is refused on a moved session rather than guessed - the message tells the user to
        materialise the upstream or declare the column.
        """
        from dbt.adapters.maxcompute.relation import declared_event_field_type

        class _Node:
            identifier = "stg_events"
            config = type("C", (), {"event_time": "event_time"})()
            columns = {
                "event_time": type("Col", (), {"name": "event_time", "data_type": "timestamp"})()
            }

        class _BareNode(_Node):
            columns = {}

        assert declared_event_field_type(_Node(), "event_time") == "timestamp"
        assert declared_event_field_type(_BareNode(), "event_time") is None

        options.local_timezone = "Asia/Shanghai"
        relation = MaxComputeRelation.create(
            identifier="__dbt__cte__stg_events", event_field_declared_type="timestamp"
        )
        with _warehouse_types({}):  # nothing to describe: an ephemeral CTE
            assert relation._render_event_time_filtered(
                EventTimeFilter(field_name="event_time", start=UTC_DAY_START, end=UTC_DAY_END)
            ) == _window("2025-05-01 08:00:00", "2025-05-02 08:00:00")
            bare = MaxComputeRelation.create(identifier="__dbt__cte__stg_events")
            with pytest.raises(DbtRuntimeError) as refused:
                bare._render_event_time_filtered(
                    EventTimeFilter(field_name="event_time", start=UTC_DAY_START, end=UTC_DAY_END)
                )
        assert "could not be read" in str(refused.value)

    def test_the_type_answer_is_read_once_per_relation_per_invocation(self, session_clock):
        """One metadata call per distinct relation, not one per batch boundary or per render.

        The window is decided twice per batch (both boundaries) and a run has many batches, while a
        column type cannot change underneath a run that is already in flight.
        """
        asked = []

        class _Columns:
            def __init__(self, name, dtype):
                self.column, self.dtype = name, dtype

        class _StubAdapter:
            def get_columns_in_relation(self, relation):
                asked.append(relation.identifier)
                return [_Columns("event_time", "timestamp")]

        class _StubFactory:
            @staticmethod
            def get_adapter_by_type(name):
                assert name == "maxcompute"
                return _StubAdapter()

        options.local_timezone = "Asia/Shanghai"
        relation_module._PHYSICAL_TYPE_CACHE.clear()
        relation = MaxComputeRelation.create(database="proj", schema="sch", identifier="up_stream")
        with mock.patch.dict("sys.modules", {"dbt.adapters.factory": _StubFactory}):
            for index in range(3):
                relation._render_event_time_filtered(
                    EventTimeFilter(
                        field_name="event_time",
                        start=datetime(2025, 5, 1 + index, tzinfo=pytz.UTC),
                        end=datetime(2025, 5, 2 + index, tzinfo=pytz.UTC),
                    )
                )
            other = MaxComputeRelation.create(
                database="proj", schema="sch", identifier="other_stream"
            )
            other._render_event_time_filtered(
                EventTimeFilter(field_name="event_time", start=UTC_DAY_START, end=UTC_DAY_END)
            )
        assert asked == ["up_stream", "other_stream"], asked
        relation_module._PHYSICAL_TYPE_CACHE.clear()
