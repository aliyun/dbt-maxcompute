"""The clocks a microbatch model depends on, and the profile field that used to move one of them.

A microbatch batch is written by overwriting the partitions its rows fall into, and this adapter
creates those partitions with ``auto partitioned by (trunc_time(<field>, '<granularity>'))``. So
the strategy is only correct while two things read the same clock:

* the batch window. dbt-core renders it as a timestamp string carrying an offset,
  ``event_time >= '2025-05-01 00:00:00+00:00'``, and MaxCompute evaluates that comparison in the
  **session** timezone (measured: `+00:00`, `+08:00` and `-05:00` on the same wall clock all
  produce the same rows, so the offset in the text is not what picks the clock);
* `trunc_time()`, which assigns each row to a partition, and cuts on **UTC**.

What sets the session timezone is the profile: `MaxComputeCredentials._get_odps` writes
pyodps' `options.local_timezone`, and pyodps turns that into the `odps.sql.timezone` session
setting (`Etc/GMT` when it is falsey). With no `timezone` in the profile - the adapter's default -
both clocks are UTC and nothing has to be corrected; `TestMicrobatchDefaultUtcSession` pins that
the rendering on that path is untouched.

`TestMicrobatchProfileTimezone` sets the documented `timezone` field instead. Before the window was
stated on the session clock this combination lost rows in silence: the window was read as local
wall clock while `trunc_time()` kept cutting UTC days, so a "day" window straddled two partitions
and the next window's `insert overwrite` erased rows the previous window had written - with the run
still reporting success. That is the behaviour pinned here, in both directions:

* the assertions are the ones a correct adapter has to satisfy, and they are shared by the two
  cells, so a change that fixes one clock pairing and breaks the other cannot pass silently;
* the fixture rows are built with `from_unixtime()`, whose instant does **not** depend on the
  session. Writing them as `TIMESTAMP'...'` text instead would move the data by the same amount the
  window moves, and the two shifts would cancel each other out: the cell would pass whether or not
  the rendering was aligned.
"""

import os

import pytest
import yaml
from dbt.tests.util import run_dbt

# Two UTC days, two rows in each, both far from the day boundary so an eight-hour shift in the
# window cannot move a row between days. `from_unixtime()` yields the same instant whatever the
# session timezone is, which is what makes the cells below comparable: rows written as
# `TIMESTAMP'...'` text would move with the session exactly as the window does, and the two shifts
# would cancel - the cell would then pass whether or not the rendering was aligned.
_EPOCHS = (
    1746061200,
    1746140400,
    1746147600,
    1746226800,
)  # 05-01 01:00/23:00, 05-02 01:00/23:00 UTC


def _input_sql(event_type):
    rows = "\nunion all\n".join(
        f"select {i} as id, cast(from_unixtime({epoch}) as {event_type}) as event_time"
        for i, epoch in enumerate(_EPOCHS, start=1)
    )
    return "{{ config(materialized='table', event_time='event_time') }}\n" + rows + "\n"


def _model_sql(event_type):
    return (
        "{{ config(\n"
        "    materialized='incremental',\n"
        "    incremental_strategy='microbatch',\n"
        "    unique_key='id',\n"
        "    event_time='event_time',\n"
        "    batch_size='day',\n"
        "    begin=modules.datetime.datetime(2025, 5, 1, 0, 0, 0),\n"
        f"    partition_by={{'field': 'event_time', 'data_type': '{event_type}', "
        "'granularity': 'day'}\n"
        ") }}\n"
        "select id, event_time from {{ ref('input_model') }}\n"
    )


DAY1 = ("2025-05-01", "2025-05-02")
DAY2 = ("2025-05-02", "2025-05-03")
DAY1_ROWS = 2
DAY2_ROWS = 2


def _relation(project, identifier="microbatch_model"):
    return project.adapter.Relation.create(
        database=project.database, schema=project.test_schema, identifier=identifier
    )


def _ids(project):
    rows = project.run_sql(f"select id from {_relation(project)} order by id", fetch="all") or []
    return sorted(int(row[0]) for row in rows)


def _partition_of(project):
    """`{id: partition day}` as the server itself groups it, and the partition list."""
    rows = project.run_sql(
        f"select id, trunc_time(event_time, 'day') as part_day from {_relation(project)} "
        "order by id",
        fetch="all",
    )
    per_row = {int(row[0]): str(row[1]) for row in rows or []}
    table = project.adapter.get_odps_client().get_table(
        "microbatch_model", schema=project.test_schema
    )
    return per_row, sorted(p.name for p in table.partitions)


def _window(start, end, select=None):
    args = ["run", "--event-time-start", start, "--event-time-end", end]
    if select is not None:
        args += ["--select", select]
    return run_dbt(args)


class _DayWindowScenario:
    """Subclasses choose the session timezone; the scenario and the expectations are the same."""

    profile_timezone = None
    event_column_type = "timestamp"

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "input_model.sql": _input_sql(self.event_column_type),
            "microbatch_model.sql": _model_sql(self.event_column_type),
        }

    @pytest.fixture(scope="class")
    def dbt_profile_target(self):
        """The shared profile, optionally with the documented `timezone` field set.

        Read the same way `tests/conftest.py` reads it, and restore pyodps' global
        `local_timezone` afterwards: the adapter assigns it per connection, so leaving
        `Asia/Shanghai` behind would silently change the session clock of later test classes.
        """
        from odps import options

        filepath = os.environ.get("DBT_PROFILE_PATH")
        if not filepath:
            pytest.skip("needs a real MaxCompute profile via DBT_PROFILE_PATH")
        saved = options.local_timezone
        with open(filepath) as handle:
            target = yaml.safe_load(handle)
        if self.profile_timezone:
            target["timezone"] = self.profile_timezone
        try:
            yield target
        finally:
            options.local_timezone = saved

    def test_adjacent_day_windows_never_lose_or_duplicate_rows(self, project):
        _window(*DAY1)
        assert _ids(project) == [1, 2], "the first window writes its own rows"
        per_row, partitions = _partition_of(project)
        assert len(partitions) == 1, f"one day window must touch one partition: {partitions}"

        _window(*DAY2, select="microbatch_model")
        assert _ids(project) == [
            1,
            2,
            3,
            4,
        ], "an adjacent window must add its rows without erasing the previous window's"
        per_row, partitions = _partition_of(project)
        assert len(partitions) == 2, f"two day windows, two partitions: {partitions}"

        _window(*DAY1, select="microbatch_model")
        assert _ids(project) == [
            1,
            2,
            3,
            4,
        ], "replaying window 1 across the window-2 partition must not lose window 2's rows"
        per_row, partitions = _partition_of(project)
        assert len(partitions) == 2, f"the replay must not open a third partition: {partitions}"
        counts = {}
        for row_id, part_day in per_row.items():
            counts[part_day[:10]] = counts.get(part_day[:10], 0) + 1
        assert (
            sum(counts.values()) == DAY1_ROWS + DAY2_ROWS
        ), f"each batch day keeps its own rows: {per_row}"
        assert len(counts) == 2, f"one partition per batch day, no window spans two: {per_row}"


class TestMicrobatchDefaultUtcSession(_DayWindowScenario):
    """No `timezone` in the profile: pyodps submits `odps.sql.timezone=Etc/GMT`.

    The adapter's default, and the path whose rendering must not change: the window and the
    partition key already read the same clock.
    """

    profile_timezone = None


class TestMicrobatchProfileTimezone(_DayWindowScenario):
    """`timezone: Asia/Shanghai` in the profile - the documented way to run on local time.

    This is the combination that used to lose rows between adjacent microbatch windows. The window
    is now stated in the session's own reading of the same UTC instant, so it lands on the partition
    `trunc_time()` cuts for that batch, and the two cells above have the same expectations.
    """

    profile_timezone = "Asia/Shanghai"


class TestMicrobatchNegativeOffsetSession(_DayWindowScenario):
    """`timezone: America/New_York` - a session *behind* UTC, and one that observes DST.

    Added because a shift with the wrong sign still keeps adjacent windows the same width: the
    +08:00 cell alone cannot tell "aligned" from "aligned and eight hours off". Here the boundary
    moves to the previous local day (2025-05-01 00:00 UTC is 2025-04-30 20:00 in EDT), so a wrong
    sign moves a row into the wrong window and the assertions below stop holding.
    """

    profile_timezone = "America/New_York"


class TestMicrobatchDatetimeColumnOnShiftedSession(_DayWindowScenario):
    """A `datetime` event column on a session that has to be shifted: `timezone: Asia/Shanghai`.

    Two fixes meet in this cell. A `datetime` column refuses dbt-core's offset-bearing text outright
    (`ODPS-0121095`, refused at physical-plan time - that half was fixed by the suffix removal), and
    a session that pinned `Asia/Shanghai` needs the window shifted onto its own clock or adjacent
    batches overwrite each other's partitions. Stating the instant as the session's reading satisfies
    both at once, and the assertions are the same ones the `timestamp` cells use: no offset suffix in
    the text, and one partition per batch.
    """

    profile_timezone = "Asia/Shanghai"
    event_column_type = "datetime"
