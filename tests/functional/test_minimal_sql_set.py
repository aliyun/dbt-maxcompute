"""Minimal real-SQL regression set for dbt-maxcompute (``-m integration_smoke``).

This is the fast, always-relevant slice of the adapter contract that only a
live MaxCompute project can answer:

* ``table``, ``view`` and ``incremental`` materializations, each verified by
  reading the object and its rows back from the server
* dbt tests - passing generic tests, a passing singular test and a
  deliberately failing one, so the run proves failures are surfaced
* persisted docs - model and column comments read back through
  ``dbt docs generate``, which takes them from the server, not from the model
* invalid SQL that must come back as an error instead of a green run
* cleanup - every schema *this run* creates is gone again (checked against the
  names this run recorded, not against every ``test*`` schema in the project)

``tests/functional/test_core.py`` stays the broader release-validation suite;
this module is the subset to run on every change, so keep it small and keep the
assertions server-side.

Credential handling lives in :mod:`tests.maxcompute_gating`.  Without usable
credentials each case is *skipped* with a reason, and the module-level gate
skips with the same reason when the configured project cannot host dbt at all,
so a run that never reached a server can never be read as an integration pass.
"""

import json
from pathlib import Path
from typing import List

import maxcompute_gating
import pytest
from dbt.tests.util import check_relation_types, run_dbt

pytestmark = pytest.mark.integration_smoke

TABLE_COMMENT = "dbt integration smoke: model description persisted on the table"
COLUMN_COMMENT = "dbt integration smoke: column description persisted"

SEED_ORDERS_CSV = """id,name,amount
1,alice,10.5
2,bob,20.5
3,carol,30.5
""".lstrip()

SEED_ORDERS_YML = """
version: 2
seeds:
  - name: seed_orders
    config:
      transactional: true
"""


def server_report(case, **facts):
    """Print what the server actually returned, so a run shows evidence."""
    detail = " ".join(f"{key}={value}" for key, value in facts.items())
    print(f"SERVER[{case}] {detail}", flush=True)


def statuses(results):
    return [str(result.status) for result in results]


#: Every schema created by the classes in this module, in creation order.
CREATED_SCHEMAS: List[str] = []


@pytest.fixture(scope="class", autouse=True)
def record_created_schema(project):
    """Remember the schema this class created, for the cleanup check below.

    Recorded twice on purpose: in-process for the assertion here, and into the
    manifest named by ``DBT_INTEGRATION_SCHEMA_MANIFEST`` so the runner script -
    a separate process - can verify the same set even if pytest is killed
    halfway through.
    """
    CREATED_SCHEMAS.append(project.test_schema)
    maxcompute_gating.record_schema(project.test_schema)
    yield


@pytest.fixture(scope="module", autouse=True)
def integration_environment_gate():
    """Skip the module when no real run is possible; then verify our own cleanup.

    The check is scoped to the schemas *this run* recorded, not to "every schema
    whose name starts with ``test``".  A MaxCompute project is shared, so a
    concurrent run - or any other ``test*`` schema appearing while we work - is
    nobody's evidence of a leak.  Verified on 2026-09-26: a project-wide diff
    reported the suite as failed (6 passed, 1 error) purely because an unrelated
    ``test*`` schema was created mid-run.
    """
    reason = maxcompute_gating.preflight_reason()
    if reason:
        pytest.skip(f"MaxCompute integration not run: {reason}")

    yield
    still_present = set(maxcompute_gating.existing_schemas())
    leaked = sorted(schema for schema in CREATED_SCHEMAS if schema in still_present)
    assert not leaked, (
        f"schemas this run created were not dropped: {leaked}; "
        "a leaked name means the previous class's teardown failed, and the next "
        "run of this module would then be tested against leftover state"
    )


# ---------------------------------------------------------------------------
# 1. table
# ---------------------------------------------------------------------------

MODEL_TABLE_SQL = """
{{ config(materialized='table') }}

select * from {{ ref('seed_orders') }}
where id <= 2
"""


class TestMinimalTable:
    @pytest.fixture(scope="class")
    def seeds(self):
        return {"seed_orders.csv": SEED_ORDERS_CSV}

    @pytest.fixture(scope="class")
    def models(self):
        return {"my_table.sql": MODEL_TABLE_SQL}

    def test_table_is_materialized_with_expected_rows(self, project):
        run_dbt(["seed"])
        results = run_dbt(["run", "--select", "my_table"])
        assert statuses(results) == ["success"]
        check_relation_types(project.adapter, {"my_table": "table"})

        rows, total = project.run_sql("select count(*), sum(amount) from my_table", fetch="one")
        server_report("table", relation="my_table", kind="table", rows=rows, sum_amount=total)
        assert rows == 2
        assert float(total) == pytest.approx(31.0)

        # A second run must be idempotent, not an append.
        run_dbt(["run", "--select", "my_table"])
        assert project.run_sql("select count(*) from my_table", fetch="one")[0] == 2


# ---------------------------------------------------------------------------
# 2. view
# ---------------------------------------------------------------------------

MODEL_VIEW_SQL = """
{{ config(materialized='view') }}

select id, name from {{ ref('seed_orders') }}
where id > 1
"""


class TestMinimalView:
    @pytest.fixture(scope="class")
    def seeds(self):
        return {"seed_orders.csv": SEED_ORDERS_CSV}

    @pytest.fixture(scope="class")
    def models(self):
        return {"my_view.sql": MODEL_VIEW_SQL}

    def test_view_is_materialized_and_queryable(self, project):
        run_dbt(["seed"])
        results = run_dbt(["run", "--select", "my_view"])
        assert statuses(results) == ["success"]
        check_relation_types(project.adapter, {"my_view": "view"})

        names = project.run_sql("select name from my_view order by name", fetch="all")
        server_report("view", relation="my_view", kind="view", rows=len(names))
        assert [name[0] for name in names] == ["bob", "carol"]


# ---------------------------------------------------------------------------
# 3. incremental (append)
# ---------------------------------------------------------------------------

MODEL_INCREMENTAL_SQL = """
{{ config(materialized='incremental', incremental_strategy='append') }}

select * from {{ ref('seed_orders') }}

{% if is_incremental() %}
where id > (select max(id) from {{ this }})
{% endif %}
"""


class TestMinimalIncremental:
    @pytest.fixture(scope="class")
    def seeds(self):
        return {"seed_orders.csv": SEED_ORDERS_CSV, "seed.yml": SEED_ORDERS_YML}

    @pytest.fixture(scope="class")
    def models(self):
        return {"my_incremental.sql": MODEL_INCREMENTAL_SQL}

    def test_incremental_appends_only_new_rows(self, project):
        run_dbt(["seed"])
        run_dbt(["run", "--select", "my_incremental"])
        first = project.run_sql("select count(*) from my_incremental", fetch="one")[0]

        # The seed is a transactional table, so new source rows can be inserted.
        project.run_sql("insert into seed_orders (id, name, amount) values (4, 'dave', 40.5)")
        run_dbt(["run", "--select", "my_incremental"])
        second = project.run_sql("select count(*) from my_incremental", fetch="one")[0]
        duplicates = project.run_sql(
            "select count(*) from (select id from my_incremental group by id having count(*) > 1) d",
            fetch="one",
        )[0]

        server_report(
            "incremental", rows_first=first, rows_second=second, duplicate_ids=duplicates
        )
        assert first == 3
        assert second == 4
        assert duplicates == 0


# ---------------------------------------------------------------------------
# 4. dbt tests: passing, and one that must fail
# ---------------------------------------------------------------------------

MODEL_CLEAN_SQL = """
{{ config(materialized='table') }}

select 1 as id, 'alice' as name
"""

MODEL_DUPLICATED_SQL = """
{{ config(materialized='table') }}

select 1 as id
union all
select 1 as id
"""

TESTS_SCHEMA_YML = """
version: 2
models:
  - name: clean
    columns:
      - name: id
        data_tests:
          - unique
          - not_null
  - name: duplicated
    columns:
      - name: id
        data_tests:
          - unique
"""

SINGULAR_TEST_SQL = """
select id from {{ ref('clean') }}
where id is null
"""


class TestMinimalTests:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "clean.sql": MODEL_CLEAN_SQL,
            "duplicated.sql": MODEL_DUPLICATED_SQL,
            "schema.yml": TESTS_SCHEMA_YML,
        }

    @pytest.fixture(scope="class")
    def tests(self):
        return {"assert_no_null_id.sql": SINGULAR_TEST_SQL}

    def test_passing_and_failing_tests_are_counted_separately(self, project):
        run_dbt(["run"])
        results = run_dbt(["test"], expect_pass=False)
        outcomes = statuses(results)
        passed = outcomes.count("pass")
        failed = outcomes.count("fail")

        server_report("tests", total=len(outcomes), passed=passed, failed=failed)
        assert len(outcomes) == 4, f"expected 3 passing and 1 failing test, got {outcomes}"
        assert passed == 3
        assert failed == 1, "the unique test on a duplicated id must fail"


# ---------------------------------------------------------------------------
# 5. docs: comments persisted to the server and surfaced by dbt docs
# ---------------------------------------------------------------------------

MODEL_DOCUMENTED_SQL = """
{{ config(materialized='table', persist_docs={'relation': true, 'columns': true}) }}

select 1 as id, 'alice' as name
"""

DOCS_SCHEMA_YML = f"""
version: 2
models:
  - name: documented_model
    description: "{TABLE_COMMENT}"
    columns:
      - name: id
        description: "{COLUMN_COMMENT}"
"""


class TestMinimalDocs:
    @pytest.fixture(scope="class")
    def models(self):
        return {"documented_model.sql": MODEL_DOCUMENTED_SQL, "schema.yml": DOCS_SCHEMA_YML}

    def test_persisted_docs_survive_a_round_trip_to_the_server(self, project):
        run_dbt(["run", "--select", "documented_model"])
        catalog = run_dbt(["docs", "generate"])
        assert catalog is not None

        # catalog.json keys nodes by unique id; the relation name and comments
        # come from the server-side metadata the catalog collected.
        catalog_data = json.loads(Path("target/catalog.json").read_text())
        node = next(
            entry
            for entry in catalog_data["nodes"].values()
            if entry["metadata"]["name"] == "documented_model"
        )

        server_report(
            "docs",
            table_comment=node["metadata"]["comment"],
            column_comment=node["columns"]["id"]["comment"],
        )
        assert node["metadata"]["comment"] == TABLE_COMMENT
        assert node["columns"]["id"]["comment"] == COLUMN_COMMENT


# ---------------------------------------------------------------------------
# 6. invalid SQL must fail
# ---------------------------------------------------------------------------

MODEL_BROKEN_SQL = """
{{ config(materialized='table') }}

select id from this_relation_does_not_exist_in_any_schema
"""


class TestInvalidSqlFails:
    """A deliberate error: the regression entry must report it, not absorb it."""

    @pytest.fixture(scope="class")
    def models(self):
        return {"broken_model.sql": MODEL_BROKEN_SQL}

    def test_invalid_sql_is_reported_as_a_failure(self, project):
        try:
            results = run_dbt(["run", "--select", "broken_model"], expect_pass=None)
            outcomes = statuses(results)
            messages = " ".join(str(getattr(result, "message", "") or "") for result in results)
        except Exception as exc:  # dbt also surfaces some failures as exceptions
            outcomes, messages = ["raised"], str(exc)

        server_report("invalid_sql", statuses=outcomes, error_code_found="ODPS-" in messages)
        assert outcomes and "success" not in outcomes, (
            f"invalid SQL was reported as {outcomes}; the integration entry cannot "
            "tell a real failure from a passing run"
        )
        assert "ODPS-" in messages, f"expected a MaxCompute server error, got: {messages[:400]}"
