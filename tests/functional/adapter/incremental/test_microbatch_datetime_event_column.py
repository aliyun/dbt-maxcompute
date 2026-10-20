"""A `datetime` event column, batched on a UTC session.

dbt-core renders the microbatch window from a UTC ``datetime``, so the text reaches the warehouse
with an offset: ``event_time >= '2025-05-01 00:00:00+00:00'``. MaxCompute compares that shape
against ``timestamp`` columns but refuses it for ``datetime`` ones, whose string form is exactly
``yyyy-mm-dd hh:mi:ss``. On the adapter's default profile - no ``timezone`` in the profile, so the
session runs on UTC - that made a microbatch model on a ``datetime`` event column fail on every
batch, at physical-plan time::

    ODPS-0130071:[0,0] Semantic analysis exception - physical plan generation failed:
    ODPS-0121095:Invalid argument - in function cast, string datetime's format must be
    yyyy-mm-dd hh:mi:ss, input string is:2025-05-01 00:00:00+00:00

``datetime`` is a documented event-time type, so this measures the fix and, just as importantly,
that the two types which ran before still hit their own batch. The window is only rewritten where
the session clock is UTC: a profile that pins another zone needs the window shifted onto that
clock as well, which is a different change (see ``test_microbatch_partition_timezone.py``).

Measured here, on a real project, on the default profile:

* ``datetime``, day batches - each batch writes its own partition, and replaying one does not
  erase its neighbour (the combination that used to error outright);
* ``datetime``, hour batches - the whole-hour boundary is accepted and lands on the hour
  partition;
* ``timestamp`` / ``timestamp_ntz`` - the same windows select the same rows they did with the
  offset suffix.
"""

from datetime import datetime

import pytest
from dbt.tests.util import run_dbt

# One row per partition day, at 09:00 so that the row is well inside its UTC day and an eight-hour
# session shift would move it across a partition boundary (that is the other test's job).
_input_datetime_sql = (
    "{{ config(materialized='table', event_time='event_time') }}\n"
    "select 1 as id, DATETIME'2025-05-01 09:00:00' as event_time\n"
    "union all\n"
    "select 2 as id, DATETIME'2025-05-02 09:00:00' as event_time\n"
)

_input_datetime_hours_sql = (
    "{{ config(materialized='table', event_time='event_time') }}\n"
    "select 1 as id, DATETIME'2025-05-01 09:30:00' as event_time\n"
    "union all\n"
    "select 2 as id, DATETIME'2025-05-01 10:30:00' as event_time\n"
)

_input_timestamp_sql = (
    "{{ config(materialized='table', event_time='event_time') }}\n"
    "select 1 as id, TIMESTAMP'2025-05-01 09:00:00' as event_time\n"
    "union all\n"
    "select 2 as id, TIMESTAMP'2025-05-02 09:00:00' as event_time\n"
)

_input_timestamp_ntz_sql = (
    "{{ config(materialized='table', event_time='event_time') }}\n"
    "select 1 as id, TIMESTAMP_NTZ'2025-05-01 09:00:00' as event_time\n"
    "union all\n"
    "select 2 as id, TIMESTAMP_NTZ'2025-05-02 09:00:00' as event_time\n"
)


def _model_sql(data_type, batch_size="day"):
    """The model under test, batched and auto-partitioned on a column of `data_type`.

    Rows sit at 09:00 when the grain is an hour so that they are inside the batch the whole hour
    boundary selects, and at midnight otherwise - `begin` has to be a batch boundary or dbt-core
    would truncate it to one anyway.
    """
    partition_by = "{'field': 'event_time', 'data_type': '%s', 'granularity': '%s'}" % (
        data_type,
        batch_size,
    )
    begin_hour = 9 if batch_size == "hour" else 0
    return (
        "{{ config(\n"
        "    materialized='incremental',\n"
        "    incremental_strategy='microbatch',\n"
        "    unique_key='id',\n"
        "    event_time='event_time',\n"
        "    batch_size='%s',\n"
        "    begin=modules.datetime.datetime(2025, 5, 1, %d, 0, 0),\n"
        "    partition_by=%s\n"
        ") }}\n"
        "select id, event_time from {{ ref('input_model') }}\n"
    ) % (batch_size, begin_hour, partition_by)


def _relation(project, identifier="microbatch_model"):
    return project.adapter.Relation.create(
        database=project.database, schema=project.test_schema, identifier=identifier
    )


def _rows(project):
    rows = project.run_sql(f"select id from {_relation(project)} order by id", fetch="all") or []
    return sorted(int(row[0]) for row in rows)


def _partitions(project, grain):
    """The partition values the server actually stored rows in, with their row counts."""
    rows = (
        project.run_sql(
            f"select cast(trunc_time(event_time, '{grain}') as string) as part, count(*) as c "
            f"from {_relation(project)} group by cast(trunc_time(event_time, '{grain}') as string) "
            "order by part",
            fetch="all",
        )
        or []
    )
    return sorted((str(row[0]), int(row[1])) for row in rows)


def _run_window(start, end):
    return run_dbt(
        [
            "run",
            "--select",
            "input_model",
            "microbatch_model",
            "--event-time-start",
            start,
            "--event-time-end",
            end,
        ]
    )


class TestDatetimeEventColumnDayBatches:
    """The combination that used to fail on every batch: `datetime` + default profile."""

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "input_model.sql": _input_datetime_sql,
            "microbatch_model.sql": _model_sql("datetime"),
        }

    def test_each_day_batch_writes_its_own_partition(self, project):
        _run_window("2025-05-01", "2025-05-02")
        assert _rows(project) == [1], "the first window must write exactly its own row"
        assert _partitions(project, "day") == [("2025-05-01", 1)]

        _run_window("2025-05-02", "2025-05-03")
        assert _rows(project) == [1, 2], "the next window adds its row without touching the first"
        assert _partitions(project, "day") == [("2025-05-01", 1), ("2025-05-02", 1)]

        _run_window("2025-05-01", "2025-05-02")
        assert _rows(project) == [1, 2], "replaying a window overwrites its own partition only"
        assert _partitions(project, "day") == [("2025-05-01", 1), ("2025-05-02", 1)]


class TestDatetimeEventColumnHourBatches:
    """A whole-hour boundary is the same shape, so hour batches must work too."""

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "input_model.sql": _input_datetime_hours_sql,
            "microbatch_model.sql": _model_sql("datetime", batch_size="hour"),
        }

    def test_hour_boundaries_land_on_hour_partitions(self, project):
        _run_window("2025-05-01 09:00:00", "2025-05-01 10:00:00")
        assert _rows(project) == [1]

        _run_window("2025-05-01 10:00:00", "2025-05-01 11:00:00")
        assert _rows(project) == [1, 2]
        parts = _partitions(project, "hour")
        assert len(parts) == 2, f"expected two hour partitions, got {parts}"
        assert sorted(count for _, count in parts) == [1, 1]


class TestTheTypesThatAlreadyRanAreUnchanged:
    """`timestamp` / `timestamp_ntz` took the offset text before; they take it without."""

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "input_model.sql": _input_for(self.data_type),
            "microbatch_model.sql": _model_sql(self.data_type),
        }

    data_type = "timestamp"

    def test_day_batches_still_select_their_own_rows(self, project):
        _run_window("2025-05-01", "2025-05-02")
        assert _rows(project) == [1]

        _run_window("2025-05-02", "2025-05-03")
        assert _rows(project) == [1, 2]
        assert _partitions(project, "day") == [("2025-05-01", 1), ("2025-05-02", 1)]


def _input_for(data_type):
    return {"timestamp": _input_timestamp_sql, "timestamp_ntz": _input_timestamp_ntz_sql}[
        data_type
    ]


class TestTimestampNtzEventColumn(TestTheTypesThatAlreadyRanAreUnchanged):
    """Same scenario, the type that ignores the session clock entirely."""

    data_type = "timestamp_ntz"


def _batch_size(grain):
    from dbt.artifacts.resources.types import BatchSize

    return BatchSize(grain)


def test_the_boundary_text_is_a_whole_hour_or_a_whole_day():
    """dbt-core aligns every batch boundary to `batch_size`, so a boundary never has microseconds.

    Pinned as a unit-level fact because the fix relies on it: it is what makes the suffix-free text
    lossless for `datetime`. The one path that can put microseconds into a window is `--sample`,
    whose end is `datetime.now(UTC)` - and a `datetime` column rejects fractional seconds too, so
    that combination stays a compile error rather than being silently rounded (see the residual
    note in `docs/microbatch-support.md`).
    """
    from dbt.materializations.incremental.microbatch import MicrobatchBuilder

    for grain_value in ("hour", "day", "month", "year"):
        moment = datetime(2025, 5, 1, 9, 17, 23, 123456)
        truncated = MicrobatchBuilder.truncate_timestamp(moment, _batch_size(grain_value))
        assert truncated.microsecond == 0
        assert truncated.second == 0
