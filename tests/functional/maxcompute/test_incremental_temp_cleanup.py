"""Regression test for temp_relation_exists tracking in incremental.sql.

When on_schema_change='ignore' (the default), the outer materialization
sets `temp_relation_exists = false` and the inner
mc_generate_incremental_build_sql creates the temp table via
`{% call statement('create_temp_relation') %}`. Jinja scoping prevents
the inner block from mutating the outer variable, so the post-run
cleanup `if temp_relation_exists -> drop_relation(temp_relation)` never
fires and `<model>__dbt_tmp` leaks in the project schema after every
incremental run.
"""
import pytest

from dbt.tests.util import run_dbt


_model_sql = """
{{ config(materialized='incremental', unique_key='id') }}
select 1 as id, 'a' as v
union all select 2, 'b'
{% if is_incremental() %}
union all select 3, 'c'
{% endif %}
"""


class TestIncrementalTempCleanup:
    @pytest.fixture(scope="class")
    def models(self):
        return {"leak_model.sql": _model_sql}

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"name": "incremental_temp_cleanup"}

    def test_temp_table_dropped_after_incremental_run(self, project):
        # First run: full refresh, no incremental path taken
        run_dbt(["run"])
        # Second run: incremental path creates temp via dbt-origin strategy
        run_dbt(["run"])

        leaked = True
        try:
            project.run_sql(
                "select count(*) from {schema}.leak_model__dbt_tmp", fetch="one"
            )
        except Exception:
            leaked = False
        assert not leaked, "leak_model__dbt_tmp survived incremental run"
