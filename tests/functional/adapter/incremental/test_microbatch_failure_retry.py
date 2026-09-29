"""What survives when one batch of a microbatch model fails, and what `dbt retry` re-runs.

A microbatch model is executed as a sequence of batches inside one node, so a failure in the
middle is not the same as a failed table model: some batches have already overwritten their
partitions. This case pins the three things a user needs to know:

* the rows of the batches that finished stay in the target;
* the node reports an error, and the run's `batch_results` says which batch failed;
* `dbt retry` after fixing the cause brings the model up to the full window without duplicating
  the rows the successful batches already wrote.

The failure is driven by a dbt variable rather than by data, so that the poisoned batch is
deterministic: when `poison_day` names the batch being compiled, the model reads a relation that
does not exist. `model.batch` is available while a batch is (re-)compiled and is empty at parse
time, which is what makes the switch per-batch instead of per-run.

Event times sit between 08:00 and 24:00 local so that the batch window and the `trunc_time()`
partition day are the same day under the adapter's default (UTC) session; the session-timezone
hazard itself is covered by `test_microbatch_partition_timezone.py`.
"""

import pytest
from dbt.artifacts.schemas.results import RunStatus
from dbt.tests.util import run_dbt

_input_model_sql = (
    "{{ config(materialized='table', event_time='event_time') }}\n"
    "select 1 as id, TIMESTAMP'2025-05-01 08:30:00' as event_time\n"
    "union all\n"
    "select 2 as id, TIMESTAMP'2025-05-02 08:30:00' as event_time\n"
    "union all\n"
    "select 3 as id, TIMESTAMP'2025-05-03 08:30:00' as event_time\n"
)

_retry_model_sql = (
    "{% set poison_day = var('poison_day', '') %}\n"
    "{% set batch_start = model.batch.event_time_start if model.batch and "
    "model.batch.event_time_start else '' %}\n"
    "{% if poison_day and (batch_start ~ '')[:10] == poison_day %}\n"
    "-- the batch whose window starts on `poison_day` reads a relation that cannot exist\n"
    "select id, event_time from dbt_microbatch_no_such_relation\n"
    "{% else %}\n"
    "select id, event_time from {{ ref('input_model') }}\n"
    "{% endif %}\n"
)

_model_config = (
    "{{ config(\n"
    "    materialized='incremental',\n"
    "    incremental_strategy='microbatch',\n"
    "    unique_key='id',\n"
    "    event_time='event_time',\n"
    "    batch_size='day',\n"
    "    begin=modules.datetime.datetime(2025, 5, 1, 0, 0, 0),\n"
    "    partition_by={'field': 'event_time', 'data_type': 'timestamp', 'granularity': 'day'}\n"
    ") }}\n"
)

microbatch_model_sql = _model_config + _retry_model_sql

WINDOW = ("2025-05-01", "2025-05-04")


def _relation(project, identifier="microbatch_model"):
    return project.adapter.Relation.create(
        database=project.database, schema=project.test_schema, identifier=identifier
    )


def _ids(project):
    rows = project.run_sql(f"select id from {_relation(project)} order by id", fetch="all") or []
    return sorted(int(row[0]) for row in rows)


class TestMicrobatchFailureAndRetry:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "input_model.sql": _input_model_sql,
            "microbatch_model.sql": microbatch_model_sql,
        }

    def test_finished_batches_survive_and_retry_completes_the_window(self, project):
        results = run_dbt(
            [
                "run",
                "--event-time-start",
                WINDOW[0],
                "--event-time-end",
                WINDOW[1],
                "--vars",
                '{"poison_day": "2025-05-02"}',
            ],
            expect_pass=False,
        )
        model_result = next(
            r for r in results if getattr(r.node, "name", None) == "microbatch_model"
        )
        assert (
            model_result.status != RunStatus.Success
        ), "the poisoned batch must not report success: " + "; ".join(
            f"{r.node.name}:{r.status}" for r in results
        )
        # Measured: one failed batch leaves the node in `partial success`, because the batches that
        # finished before it did write their partitions.
        assert str(model_result.status) == "partial success", str(model_result.status)
        failed_batches = getattr(model_result, "batch_results", None)
        assert (
            failed_batches is not None and failed_batches.failed
        ), "the failing batch must be reported in batch_results"

        # Batch 1 (2025-05-01) finished before the failure, so its row is already committed, and
        # the window is provably incomplete. Whether dbt still submits the batches after the
        # failing one is not part of the contract pinned here.
        partial = _ids(project)
        assert 1 in partial, "rows written by a finished batch must survive the failure"
        assert partial != [1, 2, 3], "the poisoned batch must leave the window incomplete"

        # Fix the cause and retry: the model ends up with the whole window, exactly once each.
        run_dbt(["retry", "--vars", '{"poison_day": ""}'])
        assert _ids(project) == [
            1,
            2,
            3,
        ], "retry must complete the window without duplicating the finished batch"

        # A further plain run must be a no-op on top of a complete window.
        run_dbt(["run", "--event-time-start", WINDOW[0], "--event-time-end", WINDOW[1]])
        assert _ids(project) == [1, 2, 3], "re-running a completed window must not duplicate rows"
