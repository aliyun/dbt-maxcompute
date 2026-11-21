"""The window is attached to the *upstream* relation, and the refusal of a DATE event column.

Two things the repository had not pinned before, both surfaced by a DATE-typed event
column that silently selected zero rows in every batch:

* where the batch predicate is applied - dbt-core resolves `event_time` on the relation being read
  and wraps *that* one, so a `cast()` inside the microbatch model's own SELECT is applied after the
  window; it does not rescue a DATE-typed upstream column;
* what this adapter does about a DATE event column now. MaxCompute evaluates
  `DATE >= '2025-05-01 00:00:00+00:00'` as NULL rather than raising, so every batch selected zero
  rows and `dbt run` still reported `PASS` with the target empty (measured: `PASS=2`, 0 rows, in
  `Etc/GMT`, `Asia/Shanghai` and `Etc/GMT+8` alike). `mc_validate_microbatch_config` now refuses the
  declared combination instead of letting it look like a successful run.
"""

import json
import os

import pytest
from dbt.tests.util import run_dbt, run_dbt_and_capture

# A DATE has no clock, so these literals read the same day in every session timezone; the
# from_unixtime() trick was only needed for the instant-valued columns of the timezone case.
_input_sql = (
    "{{ config(materialized='table', event_time='event_date') }}\n"
    "select 1 as id, cast('2025-05-01' as date) as event_date\n"
    "union all\n"
    "select 2 as id, cast('2025-05-01' as date) as event_date\n"
    "union all\n"
    "select 3 as id, cast('2025-05-02' as date) as event_date\n"
    "union all\n"
    "select 4 as id, cast('2025-05-02' as date) as event_date\n"
)

# The remedy, one step up: the relation the window is attached to exposes an instant and declares
# event_time on it.
_staged_sql = (
    "{{ config(materialized='table', event_time='event_time') }}\n"
    "select id, cast(event_date as timestamp) as event_time from {{ ref('input_model') }}\n"
)

_REASON = "warns: this model declares a `date` event partition column"
# The authority is the physical type, asked in the adapter's filter path; its refusal says so.
_PHYSICAL_REFUSAL = "cannot window a microbatch model on the `date` column"

DAY1 = ("2025-05-01", "2025-05-02")
DAY2 = ("2025-05-02", "2025-05-03")


def _microbatch_model(event_field, data_type, select_columns, upstream="input_model"):
    return (
        "{{ config(\n"
        "    materialized='incremental',\n"
        "    incremental_strategy='microbatch',\n"
        "    unique_key='id',\n"
        f"    event_time='{event_field}',\n"
        "    batch_size='day',\n"
        "    begin=modules.datetime.datetime(2025, 5, 1, 0, 0, 0),\n"
        f"    partition_by={{'field': '{event_field}', 'data_type': '{data_type}', "
        "'granularity': 'day'}\n"
        ") }}\n"
        f"select {select_columns} from {{{{ ref('{upstream}') }}}}" + "\n"
    )


class _WindowedScenario:
    model_name = "microbatch_model"
    upstream = "input_model"

    def _args(self, *extra):
        selected = list(dict.fromkeys(["input_model", self.upstream, self.model_name]))
        return ["run", "--select", *selected, *extra]

    def _relation(self, project, identifier=None):
        return project.adapter.Relation.create(
            database=project.database,
            schema=project.test_schema,
            identifier=identifier or self.model_name,
        )

    def _ids(self, project):
        rows = project.run_sql(
            f"select id from {self._relation(project)} order by id", fetch="all"
        )
        return sorted(int(row[0]) for row in rows or [])

    def _partitions(self, project):
        table = project.adapter.get_odps_client().get_table(
            self.model_name, schema=project.test_schema
        )
        return sorted(p.name for p in table.partitions)

    def _target_exists(self, project):
        return project.adapter.get_odps_client().exist_table(
            self.model_name, schema=project.test_schema
        )

    def _window(self, project, start, end, select=None, vars=None):
        args = ["run", "--event-time-start", start, "--event-time-end", end]
        if os.environ.get("MB_DEBUG_SQL"):
            args.append("--debug")
        args += ["--select", "input_model"]
        if self.upstream != "input_model":
            args.append(self.upstream)
        args.append(select or self.model_name)
        if vars is not None:
            args += ["--vars", json.dumps(vars)]
        return run_dbt(args)


class TestMicrobatchDateColumnIsRefused(_WindowedScenario):
    """The reported shape: the event column is also the partition column, declared `date`.

    Two things must be visible in the output. The compile-time diagnostic names the declaration, so a
    user reading the model sees the shape being called out; the *stop* itself is the adapter refusing
    a verified physical `DATE`, which is what guarantees the same shape is refused when the
    declaration says something else (see the class below).
    """

    model_sql = _microbatch_model("event_date", "date", "id, event_date")

    @pytest.fixture(scope="class")
    def models(self):
        return {"input_model.sql": _input_sql, "microbatch_model.sql": self.model_sql}

    def test_run_fails_loudly_instead_of_writing_an_empty_target(self, project):
        _, output = run_dbt_and_capture(
            self._args(
                "--event-time-start",
                DAY1[0],
                "--event-time-end",
                DAY1[1],
            ),
            expect_pass=None,
        )
        assert _PHYSICAL_REFUSAL in output, f"expected the physical-type refusal in:\n{output}"
        assert "NULL" in output, "the refusal has to say why nothing would be selected"
        assert "Nothing has been written" in output, "the refusal has to say nothing was written"
        # Measured ordering, and it is the point: the adapter asks the warehouse while it builds the
        # batch predicate, which happens before the materialization macro reaches
        # mc_validate_microbatch_config. So a model whose declaration *and* physical type are both
        # DATE stops with an error and no warning line at all (PASS=1 WARN=0 ERROR=1). The
        # declaration-only diagnostic is observable on the shape where the physical answer is an
        # instant - see TestMicrobatchDeclaredDateOverAnInstantUpstream below.
        assert "WARN=0" in output and "ERROR=1" in output, output[-400:]
        assert not self._target_exists(
            project
        ), "a refused model must not leave an empty target behind for a downstream to read"


class TestMicrobatchCastInsideModelIsRefused(_WindowedScenario):
    """A `cast()` in the microbatch model's own SELECT does not fix the window - and now stops.

    Measured shape: the batch predicate is attached to the *upstream* relation, whose `event_date`
    is still a physical DATE, while this model declares its own partition field as `timestamp`. The
    declaration-level check therefore says nothing here (and must not refuse: a declaration is not
    the authority), while the physical type read in the filter path does. Characterisation flipped on
    review: this used to assert an empty target behind a successful run, which was the defect.
    """

    model_sql = _microbatch_model(
        "event_date", "timestamp", "id, cast(event_date as timestamp) as event_date"
    )

    @pytest.fixture(scope="class")
    def models(self):
        return {"input_model.sql": _input_sql, "microbatch_model.sql": self.model_sql}

    def test_the_upstream_physical_date_is_refused_not_rendered_empty(self, project):
        _, output = run_dbt_and_capture(
            self._args("--event-time-start", DAY1[0], "--event-time-end", DAY1[1]),
            expect_pass=None,
        )
        assert _PHYSICAL_REFUSAL in output, f"expected the physical-type refusal in:\n{output}"
        assert not self._target_exists(project), (
            "the batch was refused before anything was written, so no empty target is left behind "
            "for a downstream to read"
        )
        # Not asserted here, and deliberately so: a *pre-populated* target staying byte-for-byte
        # unchanged across a refused batch needs a seeding path this suite does not have yet, and a
        # cell that cannot run is not evidence. Follows in the same revision once the seed exists.


class TestMicrobatchDeclaredDateOverAnInstantUpstream(_WindowedScenario):
    """A `date` *declaration* must not stop a model whose filtered column is physically an instant.

    This is the over-refusal the review rendered: the upstream staged relation exposes a real
    `timestamp` (the remedy from the class above), the model still declares its partition field as
    `date`, and the event field and partition field share a name - the exact condition the
    compile-time check used to refuse. Physical type is the authority, so the batches have to select
    their rows, and the declaration is expected to show up as a warning rather than an error.
    """

    # The window attaches to the relation this model reads, so the upstream has to be the staged
    # instant relation; a first attempt left the helper default (the DATE table) in place and the
    # adapter refused *that* column - which is the physical authority working as designed, not the
    # mismatch this class is about. Both logs are kept in evidence.
    upstream = "staged_model"
    model_sql = _microbatch_model("event_time", "date", "id, event_time", upstream="staged_model")

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "input_model.sql": _input_sql,
            "staged_model.sql": _staged_sql,
            "microbatch_model.sql": self.model_sql,
        }

    def test_the_declaration_warns_and_the_rows_still_land(self, project):
        # _window returns dbt's results, not a (output, result) pair - capturing here is the point,
        # because the assertion is about what the console shows: a warning line and no error.
        _, output = run_dbt_and_capture(
            self._args("--event-time-start", DAY1[0], "--event-time-end", DAY1[1])
        )
        assert _REASON in output, f"expected the diagnostic in:\n{output}"
        assert self._ids(project) == [1, 2], "a physically instant column is windowed fine"
        assert "ERROR=0" in output, output[-400:]


class TestMicrobatchPrePopulatedTargetSurvivesARefusal(_WindowedScenario):
    """A refused batch must leave an already-populated target exactly as it was.

    One model, two upstreams, chosen by `--vars`: `read_staged=true` reads the instantiated staged
    relation and writes its rows; `read_staged=false` reads the DATE table, so the physical type the
    filter path reads is `date` and the batch is refused. The declaration (`timestamp`) is identical
    in both runs, so what changes is the warehouse answer and nothing else - which is the whole
    point of moving the authority off the declaration.

    The assertion that matters for dbt-core#17251 is that the *second*, refused run cannot touch the
    target: the rows from the first run are still there, the partition set is unchanged, and the
    batch reported an error rather than a success.
    """

    upstream = "staged_model"
    switched_sql = (
        "{{ config(\n"
        "    materialized='incremental',\n"
        "    incremental_strategy='microbatch',\n"
        "    unique_key='id',\n"
        "    event_time='event_time',\n"
        "    batch_size='day',\n"
        "    begin=modules.datetime.datetime(2025, 5, 1, 0, 0, 0),\n"
        "    partition_by={'field': 'event_time', 'data_type': 'timestamp', 'granularity': 'day'}\n"
        ") }}\n"
        "{% if var('read_staged', 'true') | as_bool %}\n"
        "select id, event_time from {{ ref('staged_model') }}\n"
        "{% else %}\n"
        "select id, event_date as event_time from {{ ref('input_model') }}\n"
        "{% endif %}\n"
    )

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "input_model.sql": _input_sql,
            "staged_model.sql": _staged_sql,
            "microbatch_model.sql": self.switched_sql,
        }

    def test_the_refused_batch_leaves_the_seeded_target_alone(self, project):
        from dbt.tests.util import run_dbt_and_capture

        # 1) 种子：受支持的即时上游，第一天窗口正常写入
        self._window(project, DAY1[0], DAY1[1], vars={"read_staged": True})
        seeded = self._ids(project)
        seeded_partitions = self._partitions(project)
        assert seeded == [1, 2], seeded

        # 2) 同一模型、同一声明，只把被过滤的关系换成物理 DATE ⇒ 必须被拒
        _, output = run_dbt_and_capture(
            self._args(
                "--event-time-start",
                DAY2[0],
                "--event-time-end",
                DAY2[1],
                "--vars",
                json.dumps({"read_staged": False}),
            ),
            expect_pass=None,
        )
        assert _PHYSICAL_REFUSAL in output, f"expected the physical-type refusal in:\n{output}"[
            -400:
        ]

        # 3) 被拒的批次没有动目标表：行与分区都还是种子那一份
        assert self._ids(project) == seeded, "a refused batch must not rewrite the target"
        assert self._partitions(project) == seeded_partitions, "and must not open a new partition"


class TestMicrobatchDateColumnWorkaround(_WindowedScenario):
    """The shape the refusal points at: an instant *at the relation the window is attached to*.

    Measured on the adapter's default session (`timezone` unset, so `odps.sql.timezone=Etc/GMT`).
    Under a profile that sets `timezone`, the DATE->TIMESTAMP reading is taken on the session clock
    and can fall on the previous partition day - that is the session-timezone behaviour pinned in
    `test_microbatch_partition_timezone.py`, not something this case re-litigates.
    """

    upstream = "staged_model"
    model_sql = _microbatch_model(
        "event_time", "timestamp", "id, event_time", upstream="staged_model"
    )

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "input_model.sql": _input_sql,
            "staged_model.sql": _staged_sql,
            "microbatch_model.sql": self.model_sql,
        }

    def test_day_windows_land_on_their_own_partitions(self, project):
        self._window(project, *DAY1)
        assert self._ids(project) == [1, 2], "the first day window writes its own two rows"
        assert len(self._partitions(project)) == 1, "one day window touches one partition"

        self._window(project, *DAY2, select=self.model_name)
        assert self._ids(project) == [1, 2, 3, 4], "the adjacent window adds its rows"
        assert len(self._partitions(project)) == 2, "two day windows, two partitions"

        self._window(project, *DAY1, select=self.model_name)
        assert self._ids(project) == [1, 2, 3, 4], "replaying window 1 must not erase window 2"
        assert len(self._partitions(project)) == 2, "the replay must not open a third partition"
