"""The clocks a microbatch model depends on, and the profile field that moves one of them.

A microbatch batch is written by overwriting the partitions its rows fall into, and this adapter
creates those partitions with ``auto partitioned by (trunc_time(<field>, '<granularity>'))``. So
the strategy is only correct while two things read the same clock:

* the batch window. dbt-core renders it as a timestamp string carrying an offset,
  ``event_time >= '2025-05-01 00:00:00+00:00'``, and MaxCompute evaluates that comparison in the
  **session** timezone (measured: `+00:00`, `+08:00` and `-05:00` on the same wall clock all
  produce the same rows, so the offset in the text is not what picks the clock);
* `trunc_time()`, which assigns each row to a partition.

What sets the session timezone is the profile: `MaxComputeCredentials._get_odps` writes
`options.local_timezone`, and pyodps turns that into the `odps.sql.timezone` session setting
(`Etc/GMT` when it is falsey). With no `timezone` in the profile - the adapter's default - the
session runs on UTC, one day window maps onto exactly one partition, and adjacent windows never
touch the same partition. That is what `TestMicrobatchDefaultUtcSession` pins.

`TestMicrobatchProfileTimezone` sets the documented `timezone` field instead. That is the
combination worth watching, because it moves one clock and not the other: the window starts being
read as local wall clock while the rows in the target were written under a UTC session, so a
"day" window can straddle two partitions and a later window can overwrite a partition an earlier
window filled. The assertions state what the documented contract has to guarantee.
"""

import os

import pytest
import yaml
from dbt.tests.util import run_dbt

# Two local days, one row on each side of 08:00 so that an eight-hour session shift moves rows
# between partition days: under a UTC session these are partitions 2025-05-01 / 2025-05-02; under
# a UTC+8 session the rows written at local 01:00 and 09:00 fall into different partitions.
_input_sql = (
    "{{ config(materialized='table', event_time='event_time') }}\n"
    "select 1 as id, TIMESTAMP'2025-05-01 09:00:00' as event_time\n"
    "union all\n"
    "select 2 as id, TIMESTAMP'2025-05-01 20:00:00' as event_time\n"
    "union all\n"
    "select 3 as id, TIMESTAMP'2025-05-02 01:00:00' as event_time\n"
    "union all\n"
    "select 4 as id, TIMESTAMP'2025-05-02 10:00:00' as event_time\n"
)

_model_sql = (
    "{{ config(\n"
    "    materialized='incremental',\n"
    "    incremental_strategy='microbatch',\n"
    "    unique_key='id',\n"
    "    event_time='event_time',\n"
    "    batch_size='day',\n"
    "    begin=modules.datetime.datetime(2025, 5, 1, 0, 0, 0),\n"
    "    partition_by={'field': 'event_time', 'data_type': 'timestamp', 'granularity': 'day'}\n"
    ") }}\n"
    "select id, event_time from {{ ref('input_model') }}\n"
)

DAY1 = ("2025-05-01", "2025-05-02")
DAY2 = ("2025-05-02", "2025-05-03")


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
    """Subclasses choose the session timezone; the scenario is the same three invocations."""

    profile_timezone = None

    @pytest.fixture(scope="class")
    def models(self):
        return {"input_model.sql": _input_sql, "microbatch_model.sql": _model_sql}

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

        _window(*DAY1, select="microbatch_model")
        assert _ids(project) == [1, 2, 3, 4], "replaying window 1 must not disturb window 2"
        per_row, partitions = _partition_of(project)
        assert len(partitions) == 2, f"two day windows, two partitions: {partitions}"


class TestMicrobatchDefaultUtcSession(_DayWindowScenario):
    """No `timezone` in the profile: pyodps sends `odps.sql.timezone=Etc/GMT`."""

    profile_timezone = None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "measured: with `timezone: Asia/Shanghai` in the profile the same fixture ends up with "
        "only the second window's rows ([3, 4] instead of [1, 2, 3, 4]) - a UTC day window is "
        "read on the session clock while trunc_time() cuts partitions on UTC days, so adjacent "
        "windows share a partition and the later overwrite erases the earlier rows. The default "
        "profile is unaffected because the adapter submits odps.sql.timezone=Etc/GMT."
    ),
)
class TestMicrobatchProfileTimezone(_DayWindowScenario):
    """`timezone: Asia/Shanghai` in the profile - the documented way to run on local time.

    This is the combination that has to be fixed or rejected: a user who sets the documented
    `timezone` field gets silent row loss between adjacent microbatch windows.
    """

    profile_timezone = "Asia/Shanghai"
