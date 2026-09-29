"""Window and re-run semantics of the ``microbatch`` incremental strategy on MaxCompute.

Every window here is passed explicitly with ``--event-time-start`` / ``--event-time-end`` and
every event time in the fixtures is a static literal, so a batch boundary is reproducible
without changing a clock - on this machine or on the service. The microbatch coverage that used
to live in this repository was skipped on the grounds that the API could not "freeze time";
these tests replace that assumption with measurements.

Cases:

* adjacent windows - a batch writes exactly ``[start, end)``, including rows that sit exactly on
  a window boundary;
* repeated window / empty window - replay is idempotent, and a window with no matching rows
  creates nothing and deletes nothing;
* late data - a row whose event time falls in an already-written partition only appears once
  that partition's window is replayed;
* duplicate keys - what a batch write does with a repeated ``unique_key`` value;
* configuration combinations - which combinations are rejected, with which message.
"""

import pytest
from dbt.artifacts.schemas.results import RunStatus
from dbt.tests.util import run_dbt, run_dbt_and_capture

# Static event times, one day apart. Two rows sit on a partition boundary on purpose: id 2 is
# exactly the start of day 2, id 4 is exactly the start of day 3. All rows are at 08:00 local so
# that MaxCompute's UTC-based `trunc_time` partitioning agrees with the window's local date;
# `test_microbatch_partition_timezone.py` covers what happens when they do not.
_input_model_sql = (
    "{{ config(materialized='table', event_time='event_time') }}\n"
    "select 1 as id, TIMESTAMP'2025-05-01 08:00:00' as event_time\n"
    "union all\n"
    "select 2 as id, TIMESTAMP'2025-05-02 08:00:00' as event_time\n"
    "union all\n"
    "select 3 as id, TIMESTAMP'2025-05-02 23:59:59' as event_time\n"
    "union all\n"
    "select 4 as id, TIMESTAMP'2025-05-03 08:00:00' as event_time\n"
)

_MC_PARTITION_BY = "{'field': 'event_time', 'data_type': 'timestamp', 'granularity': 'day'}"


def _microbatch_model_sql(*, extra=""):
    return (
        "{{ config(\n"
        "    materialized='incremental',\n"
        "    incremental_strategy='microbatch',\n"
        "    unique_key='id',\n"
        "    event_time='event_time',\n"
        "    batch_size='day',\n"
        "    begin=modules.datetime.datetime(2025, 5, 1, 0, 0, 0),\n"
        f"    partition_by={_MC_PARTITION_BY}{extra}\n"
        ") }}\n"
        "select id, event_time from {{ ref('input_model') }}\n"
    )


_microbatch_model = _microbatch_model_sql()

DAY1 = ("2025-05-01", "2025-05-02")
DAY2 = ("2025-05-02", "2025-05-03")
DAY3 = ("2025-05-03", "2025-05-04")
EMPTY_DAY = ("2025-05-10", "2025-05-11")


def _relation(project, identifier):
    return project.adapter.Relation.create(
        database=project.database, schema=project.test_schema, identifier=identifier
    )


def _ids(project, identifier="microbatch_model"):
    rows = (
        project.run_sql(
            f"select id from {_relation(project, identifier)} order by id", fetch="all"
        )
        or []
    )
    return sorted(int(row[0]) for row in rows)


def _count_in_window(project, start, end, identifier="microbatch_model"):
    sql = (
        f"select count(*) from {_relation(project, identifier)} "
        f"where event_time >= TIMESTAMP'{start} 00:00:00' "
        f"and event_time < TIMESTAMP'{end} 00:00:00'"
    )
    return int(project.run_sql(sql, fetch="one")[0])


def _partition_days(project, identifier="microbatch_model"):
    """Distinct `trunc_time(event_time, 'day')` values actually present in the target."""
    sql = (
        f"select distinct cast(trunc_time(event_time, 'day') as string) as part_day "
        f"from {_relation(project, identifier)} order by part_day"
    )
    return sorted(row[0] for row in project.run_sql(sql, fetch="all") or [])


def _window(start, end, select=None):
    args = ["run", "--event-time-start", start, "--event-time-end", end]
    if select is not None:
        args += ["--select", select]
    return run_dbt(args)


def _outcome(select):
    """Run one model over one window and report ``(passed, text)``.

    ``expect_pass=None`` because this helper is used for runs that are expected to succeed *and*
    for runs that are expected to fail: ``expect_pass=False`` would itself raise when a model
    unexpectedly succeeds, and that exception is not the behaviour under test. Text combines the
    per-node results with dbt's own output, since a Jinja ``raise_compiler_error`` only reaches
    the output, not ``result.message``.
    """
    args = [
        "run",
        "--select",
        "input_model",
        select,
        "--event-time-start",
        DAY1[0],
        "--event-time-end",
        DAY1[1],
    ]
    try:
        results, output = run_dbt_and_capture(args, expect_pass=None)
    except BaseException as exc:  # noqa: BLE001 - a parse-time abort carries the message
        return False, f"{type(exc).__name__}: {exc}"
    text = "\n".join(
        f"{getattr(result.node, 'name', '-')}: {result.status}: {result.message}"
        for result in results
    )
    return all(result.status == RunStatus.Success for result in results), f"{text}\n{output}"


class TestMicrobatchWindows:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "input_model.sql": _input_model_sql,
            "microbatch_model.sql": _microbatch_model,
        }

    def test_adjacent_windows_are_half_open(self, project):
        # The first invocation also materializes input_model; the window only covers day 1.
        _window(*DAY1)
        assert _ids(project) == [1], "a batch must only write its own window"

        _window(*DAY2, select="microbatch_model")
        assert _ids(project) == [1, 2, 3], "an adjacent window must add its own rows only"
        assert _partition_days(project) == ["2025-05-01", "2025-05-02"]

        _window(*DAY3, select="microbatch_model")
        assert _ids(project) == [1, 2, 3, 4]
        assert [_count_in_window(project, *day) for day in (DAY1, DAY2, DAY3)] == [1, 2, 1]

    def test_repeated_window_is_idempotent_and_empty_window_writes_nothing(self, project):
        before = _ids(project)
        _window(*DAY2, select="microbatch_model")
        _window(*DAY2, select="microbatch_model")
        assert _ids(project) == before, "replaying a window rewrites it, it does not append"

        _window(*EMPTY_DAY, select="microbatch_model")
        assert _ids(project) == before, "an empty window must not change the target"
        assert _count_in_window(project, *EMPTY_DAY) == 0
        assert _count_in_window(project, *DAY2) == 2, "an empty window must not clear a neighbour"

    def test_late_row_appears_only_when_its_own_window_is_replayed(self, project):
        project.run_sql(
            f"insert into {_relation(project, 'input_model')} (id, event_time) "
            "values (5, TIMESTAMP'2025-05-02 18:00:00')"
        )
        # Running a different window does not pick the late row up: microbatch reads a window,
        # not "everything newer than the last run". Widening coverage is the user's decision
        # (`lookback`, or replaying the affected window).
        _window(*DAY3, select="microbatch_model")
        assert _ids(project) == [1, 2, 3, 4], "a late row is invisible to unrelated windows"

        _window(*DAY2, select="microbatch_model")
        assert _ids(project) == [1, 2, 3, 4, 5]
        assert _count_in_window(project, *DAY2) == 3
        assert _count_in_window(project, *DAY1) == 1


class TestMicrobatchDuplicateKeys:
    """``unique_key`` is part of the documented microbatch contract, but a batch is written as a
    partition overwrite - so a repeated key inside one window is not an upsert."""

    _dup_input_sql = (
        "{{ config(materialized='table', event_time='event_time') }}\n"
        "select 42 as id, 'a' as label, TIMESTAMP'2025-05-01 09:00:00' as event_time\n"
        "union all\n"
        "select 42 as id, 'b' as label, TIMESTAMP'2025-05-01 10:00:00' as event_time\n"
    )
    _dup_model_sql = (
        "{{ config(\n"
        "    materialized='incremental',\n"
        "    incremental_strategy='microbatch',\n"
        "    unique_key='id',\n"
        "    event_time='event_time',\n"
        "    batch_size='day',\n"
        "    begin=modules.datetime.datetime(2025, 5, 1, 0, 0, 0),\n"
        f"    partition_by={_MC_PARTITION_BY}\n"
        ") }}\n"
        "select id, label, event_time from {{ ref('input_model') }}\n"
    )

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "input_model.sql": self._dup_input_sql,
            "microbatch_model.sql": self._dup_model_sql,
        }

    def _labels(self, project):
        rows = project.run_sql(
            f"select label from {_relation(project, 'microbatch_model')}", fetch="all"
        )
        return sorted(row[0] for row in rows or [])

    def test_same_key_twice_in_one_window_keeps_both_rows(self, project):
        _window(*DAY1)
        assert self._labels(project) == [
            "a",
            "b",
        ], "measured: a batch overwrites its partition; it does not merge on unique_key"
        _window(*DAY1, select="microbatch_model")
        assert self._labels(project) == ["a", "b"], "replaying the window still leaves two rows"


def _config_case(**overrides):
    """The canonical microbatch config with the given keys replaced (``None`` drops a key)."""
    parts = {
        "materialized": "'incremental'",
        "incremental_strategy": "'microbatch'",
        "unique_key": "'id'",
        "event_time": "'event_time'",
        "batch_size": "'day'",
        "begin": "modules.datetime.datetime(2025, 5, 1, 0, 0, 0)",
        "partition_by": _MC_PARTITION_BY,
    }
    select_columns = overrides.pop("select_columns", None) or "id, event_time"
    for key, value in overrides.items():
        if value is None:
            parts.pop(key)
        else:
            parts[key] = value
    lines = [f"    {key}={value}," for key, value in parts.items()]
    return (
        "{{ config(\n" + "\n".join(lines) + "\n) }}\n"
        f"select {select_columns} from {{{{ ref('input_model') }}}}\n"
    )


class _MicrobatchConfigCase:
    """One configuration case per dbt project.

    dbt aborts the whole manifest at parse time as soon as *any* microbatch model is invalid, so
    sharing a project between these cases would only ever surface the first error (measured: all
    six reported `no_begin`'s parse error). Each subclass therefore gets its own project.
    """

    case = ""
    model_sql = ""
    #: `None` means the configuration is expected to run, not to be rejected.
    expected = None

    @pytest.fixture(scope="class")
    def models(self):
        return {"input_model.sql": _input_model_sql, f"{self.case}.sql": self.model_sql}

    def test_outcome(self, project):
        passed, text = _outcome(self.case)
        if self.expected is None:
            assert passed, f"{self.case} was expected to run, but it failed:\n{text}"
            assert _ids(project, self.case) == [1]
            return
        assert not passed, f"{self.case} was expected to fail, but it passed:\n{text}"
        assert self.expected in text, f"{self.case}: expected {self.expected!r} in:\n{text}"


class TestMicrobatchWithoutPartitionBy(_MicrobatchConfigCase):
    case = "no_partition_by"
    model_sql = _config_case(partition_by=None)
    expected = "requires a `partition_by` config"


class TestMicrobatchGranularityMismatch(_MicrobatchConfigCase):
    case = "granularity_mismatch"
    model_sql = _config_case(
        partition_by="{'field': 'event_time', 'data_type': 'timestamp', " "'granularity': 'hour'}"
    )
    expected = "same granularity as its configured `batch_size`"


class TestMicrobatchWithoutEventTime(_MicrobatchConfigCase):
    case = "no_event_time"
    model_sql = _config_case(
        event_time=None,
        partition_by="{'field': 'created_at', 'data_type': 'timestamp', 'granularity': 'day'}",
        select_columns="id, event_time, event_time as created_at",
    )
    # dbt-core rejects this while parsing the model, before this adapter's own validation runs.
    expected = "must provide an 'event_time' (string) config"


class TestMicrobatchWithoutBatchSize(_MicrobatchConfigCase):
    case = "no_batch_size"
    model_sql = _config_case(batch_size=None)
    # Enforced by dbt-core before this adapter's own validation runs.
    expected = "batch_size"


class TestMicrobatchWithoutBegin(_MicrobatchConfigCase):
    case = "no_begin"
    model_sql = _config_case(begin=None)
    expected = "must provide a 'begin'"


class TestMicrobatchWithoutUniqueKey(_MicrobatchConfigCase):
    """The showcase model in this repository claims microbatch requires ``unique_key``; the
    write path is a partition overwrite, so measure whether anything actually enforces it."""

    case = "no_unique_key"
    model_sql = _config_case(unique_key=None)
    expected = None
