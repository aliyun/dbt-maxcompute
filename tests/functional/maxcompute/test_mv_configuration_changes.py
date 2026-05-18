"""Regression test for materialized view configuration_changes detection.

`maxcompute__get_materialized_view_configuration_changes` was empty:

    {% macro maxcompute__get_materialized_view_configuration_changes(...) %}
    {% endmacro %}

dbt-core's MV materialization does `if configuration_changes is none`,
but an empty Jinja macro returns `""` (a truthy non-none string), not
`None`. So the materialization skips the refresh branch entirely and
falls into `on_configuration_change == 'apply'` → ALTER, which in
dbt-maxcompute is implemented as `get_replace_sql` (DROP + CREATE).

Net effect: every subsequent `dbt run` of an MV does a full DROP + CREATE
even when nothing in the config changed. The view's persisted metadata
(lifecycle, comment, etc.) is rewritten unnecessarily, and the data path
is the expensive full rebuild instead of the cheaper
`ALTER MATERIALIZED VIEW ... REBUILD` refresh.

The proper behavior:
  - same config → return `none` → REFRESH (REBUILD)
  - changed config → return non-none → REPLACE (DROP + CREATE)

PyODPS exposes `Table.creation_time`; REBUILD leaves it unchanged,
DROP+CREATE bumps it. We use that as the witness: two runs with
identical config must leave `creation_time` untouched.
"""
import os
import time
import pytest
from dbt.adapters.maxcompute.relation import MaxComputeRelation
from dbt.tests.util import run_dbt


_seed_csv = """
id,name
1,Alice
2,Bob
""".lstrip()

_schema_yml = """
version: 2
sources:
  - name: raw
    schema: "{{ target.schema }}"
    tables:
      - name: src
        identifier: mv_cfg_src
"""

_model_v1 = """
{{ config(
    materialized='materialized_view',
    lifecycle=1
) }}
select id, name from {{ source('raw', 'src') }}
"""

_model_v2 = """
{{ config(
    materialized='materialized_view',
    lifecycle=7
) }}
select id, name from {{ source('raw', 'src') }}
"""


def _read_table(project, identifier):
    adapter = project.adapter
    with adapter.connection_named("__test"):
        relation = MaxComputeRelation.create(
            database=project.database,
            schema=project.test_schema,
            identifier=identifier,
        )
        return adapter.get_odps_table_by_relation(relation, 3)


class TestMaterializedViewConfigurationChanges:
    @pytest.fixture(scope="class")
    def seeds(self):
        return {"mv_cfg_src.csv": _seed_csv}

    @pytest.fixture(scope="class")
    def models(self):
        return {"mv_model.sql": _model_v1, "schema.yml": _schema_yml}

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"name": "mv_cfg_changes"}

    def test_unchanged_config_refreshes_does_not_recreate(self, project):
        """Two runs with identical config must not DROP+CREATE the MV."""
        run_dbt(["seed"])
        # First run creates the MV.
        run_dbt(["run"])
        t1 = _read_table(project, "mv_model").creation_time
        # Sleep so a re-create would tick creation_time by ≥1s.
        time.sleep(2)
        # Second run, identical config. Should REFRESH, not REPLACE.
        run_dbt(["run"])
        t2 = _read_table(project, "mv_model").creation_time
        assert t1 == t2, (
            f"MV creation_time changed ({t1} -> {t2}) without a config "
            "change. configuration_changes is returning a truthy empty "
            "string instead of None, so the materialization runs the "
            "DROP+CREATE path every time. Detection must return None "
            "when configs match."
        )

    def test_changed_config_is_applied(self, project):
        """Bumping lifecycle must take effect on next run (no --full-refresh)."""
        model_path = os.path.join(
            project.project_root, "models", "mv_model.sql"
        )
        with open(model_path, "w") as f:
            f.write(_model_v2)
        run_dbt(["run"])
        assert _read_table(project, "mv_model").lifecycle == 7
