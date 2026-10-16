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

from datetime import datetime

import pytest
import pytz
from dbt.adapters.base.relation import EventTimeFilter
from odps import options

from dbt.adapters.maxcompute.relation import MaxComputeRelation

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


def _render(start=UTC_DAY_START, end=UTC_DAY_END, field="event_time"):
    relation = MaxComputeRelation.create(identifier="microbatch_model")
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
