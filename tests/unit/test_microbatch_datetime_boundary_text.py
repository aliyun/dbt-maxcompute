"""The batch window a UTC session can parse, decided without a server.

dbt-core renders a microbatch window from a UTC ``datetime``, so the text it produces carries an
offset: ``event_time >= '2025-05-01 00:00:00+00:00'``. MaxCompute refuses that shape for a
``datetime`` column - ``ODPS-0121095`` while generating the physical plan - which is why the same
model runs on ``timestamp`` and fails outright on ``datetime``. The adapter drops the suffix, and
only where the session clock is UTC so that the instant the window denotes does not move.

Everything asserted here is pure rendering, so it runs on a machine with no MaxCompute
credentials. The server readings behind it - three column types x both text shapes x three
session clocks, and the model end to end - are in
``tests/functional/adapter/incremental/test_microbatch_datetime_event_column.py`` and summarised
in ``docs/microbatch-support.md``.
"""

from datetime import datetime

import pytest
import pytz
from dbt.adapters.base.relation import EventTimeFilter
from odps import options

from dbt.adapters.maxcompute.relation import MaxComputeRelation

UTC_DAY_START = datetime(2025, 5, 1, 0, 0, 0, tzinfo=pytz.UTC)
UTC_DAY_END = datetime(2025, 5, 2, 0, 0, 0, tzinfo=pytz.UTC)
DAY_WINDOW = "event_time >= '2025-05-01 00:00:00' and event_time < '2025-05-02 00:00:00'"


@pytest.fixture
def session_clock():
    """Set pyodps' global session timezone for one test and put it back afterwards."""
    saved = options.local_timezone
    yield
    options.local_timezone = saved


def _render(start=UTC_DAY_START, end=UTC_DAY_END, field="event_time"):
    relation = MaxComputeRelation.create(identifier="microbatch_model")
    return relation._render_event_time_filtered(
        EventTimeFilter(field_name=field, start=start, end=end)
    )


def _dbt_core_text(start=UTC_DAY_START, end=UTC_DAY_END, field="event_time"):
    """What dbt-core renders, and what a ``datetime`` column rejects."""
    return f"{field} >= '{start}' and {field} < '{end}'"


class TestUtcSessionDropsTheOffset:
    """The adapter's default profile pins no zone, and pyodps then submits Etc/GMT."""

    @pytest.mark.parametrize("pinned", [False, "", 0])
    def test_falsey_local_timezone_is_a_utc_session(self, pinned, session_clock):
        options.local_timezone = pinned
        assert _render() == DAY_WINDOW
        assert "+00:00" not in _render()

    @pytest.mark.parametrize("name", ["UTC", "Etc/GMT", "GMT", "Etc/UTC", "Z", "+00:00"])
    def test_named_utc_zones_are_the_same_clock(self, name, session_clock):
        options.local_timezone = name
        assert _render() == DAY_WINDOW

    def test_a_zone_with_a_real_offset_is_decided_by_offset_not_by_name(self, session_clock):
        """`Etc/GMT+8` is UTC-8 (POSIX sign), and neither reading is UTC."""
        options.local_timezone = "Etc/GMT+8"
        assert _render() == _dbt_core_text()

    def test_hour_boundary_keeps_its_hour(self, session_clock):
        options.local_timezone = False
        assert (
            _render(
                start=datetime(2025, 5, 1, 9, tzinfo=pytz.UTC),
                end=datetime(2025, 5, 1, 10, tzinfo=pytz.UTC),
            )
            == "event_time >= '2025-05-01 09:00:00' and event_time < '2025-05-01 10:00:00'"
        )

    def test_only_the_suffix_changes(self, session_clock):
        """Same field, same operators, same wall clock: the offset is the only thing removed."""
        options.local_timezone = False
        assert _render() == _dbt_core_text().replace("+00:00", "")


class TestSubsecondBoundaries:
    """A ``--sample`` window carries microseconds (its end is ``datetime.now(UTC)``)."""

    def test_microseconds_are_kept_and_the_offset_dropped(self, session_clock):
        options.local_timezone = False
        assert _render(
            start=datetime(2025, 5, 1, 9, 0, 0, 500000, tzinfo=pytz.UTC),
            end=datetime(2025, 5, 1, 10, 0, 0, 123456, tzinfo=pytz.UTC),
        ) == (
            "event_time >= '2025-05-01 09:00:00.500000' "
            "and event_time < '2025-05-01 10:00:00.123456'"
        )

    def test_zero_microseconds_render_as_plain_seconds(self, session_clock):
        options.local_timezone = False
        assert _render(start=datetime(2025, 5, 1, 9, tzinfo=pytz.UTC), end=None) == (
            "event_time >= '2025-05-01 09:00:00'"
        )


class TestSessionsLeftAlone:
    """Shifting a window onto a non-UTC clock is a separate change; guessing one is not."""

    @pytest.mark.parametrize("name", ["Asia/Shanghai", "Etc/GMT+8", "America/New_York"])
    def test_other_zone_sessions_keep_dbt_cores_rendering(self, name, session_clock):
        options.local_timezone = name
        assert _render() == _dbt_core_text()

    def test_an_unreadable_zone_name_is_not_guessed_at(self, session_clock):
        options.local_timezone = "Not/AZone"
        assert _render() == _dbt_core_text()

    def test_a_non_string_zone_name_is_not_guessed_at(self, session_clock):
        options.local_timezone = object()
        assert _render() == _dbt_core_text()

    def test_nothing_pinned_means_the_project_default_applies(self, session_clock):
        """`None` is not UTC: it is 'the client pinned nothing', so the window stays as rendered."""
        options.local_timezone = None
        assert _render() == _dbt_core_text()


class TestZonesThatOnlyLookUtc:
    """A zone is judged at the boundary's instant, not by its name or by a fixed date.

    `Europe/London` is UTC+0 in January and UTC+1 in July. Deciding "is this a UTC session" from
    one probe date would drop the suffix in July too, and that shifts the window by an hour while
    `trunc_time()` keeps cutting UTC hours - the silent kind of wrong this file exists to avoid.
    """

    def test_a_summer_boundary_in_a_dst_zone_is_left_as_dbt_core_rendered(self, session_clock):
        options.local_timezone = "Europe/London"
        july_start = datetime(2025, 7, 1, 0, 0, 0, tzinfo=pytz.UTC)
        july_end = datetime(2025, 7, 2, 0, 0, 0, tzinfo=pytz.UTC)
        assert _render(start=july_start, end=july_end) == _dbt_core_text(july_start, july_end)

    def test_a_winter_boundary_in_the_same_zone_reads_the_utc_wall_clock(self, session_clock):
        """January in London *is* UTC, so stating the window without the suffix moves nothing."""
        options.local_timezone = "Europe/London"
        winter_start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=pytz.UTC)
        winter_end = datetime(2025, 1, 2, 0, 0, 0, tzinfo=pytz.UTC)
        assert _render(start=winter_start, end=winter_end) == (
            "event_time >= '2025-01-01 00:00:00' and event_time < '2025-01-02 00:00:00'"
        )


class TestFilterShapes:
    """One-sided and empty windows keep dbt-core's shape, just without the suffix."""

    def test_no_boundaries_render_empty(self, session_clock):
        options.local_timezone = False
        assert _render(start=None, end=None) == ""

    def test_only_end_is_exclusive(self, session_clock):
        options.local_timezone = False
        assert _render(start=None, end=UTC_DAY_END) == "event_time < '2025-05-02 00:00:00'"

    def test_only_start_is_inclusive(self, session_clock):
        options.local_timezone = False
        assert _render(end=None) == "event_time >= '2025-05-01 00:00:00'"

    def test_field_name_is_passed_through(self, session_clock):
        options.local_timezone = False
        assert _render(field="created_at") == (
            "created_at >= '2025-05-01 00:00:00' and created_at < '2025-05-02 00:00:00'"
        )

    def test_a_naive_boundary_is_read_as_utc(self, session_clock):
        """dbt-core makes every boundary UTC-aware; a naive one is not handed an offset either."""
        options.local_timezone = False
        assert _render(start=datetime(2025, 5, 1, 0, 0, 0), end=None) == (
            "event_time >= '2025-05-01 00:00:00'"
        )

    def test_the_wrapped_relation_reads_the_same_window(self, session_clock):
        """The public path - what actually reaches the warehouse - agrees with the predicate."""
        options.local_timezone = False
        relation = MaxComputeRelation.create(
            identifier="microbatch_model",
            event_time_filter=EventTimeFilter(
                field_name="event_time", start=UTC_DAY_START, end=UTC_DAY_END
            ),
        )
        wrapped = relation.render_event_time_filtered("`microbatch_model`")
        assert "`microbatch_model` where " in wrapped
        assert DAY_WINDOW in wrapped
        assert "+00:00" not in wrapped
