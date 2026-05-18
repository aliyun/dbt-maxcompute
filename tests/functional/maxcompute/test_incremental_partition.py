"""Regression tests for incremental materialization on partitioned tables.

Covers every incremental_strategy x partition_by combination so that any
adapter-level change to column discovery or merge SQL generation is caught
before release.

Background: the merge strategy used to emit INSERT clauses that omitted
the partition column for non-auto partitioned targets, triggering
ODPS-0123031 "invalid dynamic partition value". These tests pin the
correct behavior end-to-end.
"""
import pytest

from dbt.tests.util import run_dbt


# ----- seed shared across cases -----

seeds_csv = """
id,name,event_time,pt
1,Alice,2024-10-01T00:00:00,p01
2,Bob,2024-10-02T00:00:00,p02
3,Carol,2024-10-03T00:00:00,p03
4,Dave,2024-10-04T00:00:00,p04
5,Eve,2024-10-05T00:00:00,p05
""".lstrip()

schema_yml = """
version: 2
sources:
  - name: raw
    schema: "{{ target.schema }}"
    tables:
      - name: seed
        identifier: "{{ var('seed_name', 'seed_partition') }}"
"""


def _project_config(name):
    return {"name": name}


# =========================================================================
# merge strategy
# =========================================================================

_merge_non_auto_partition_sql = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key='id',
    partition_by={"fields": "pt", "data_types": "string"}
) }}
select id, name, event_time, pt
from {{ source('raw', 'seed') }}
{% if is_incremental() %}
  where id >= 3
{% endif %}
"""


class TestMergeNonAutoPartition:
    """Reproduces the original bug: merge + string partition column."""

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"seed_partition.csv": seeds_csv}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "model.sql": _merge_non_auto_partition_sql,
            "schema.yml": schema_yml,
        }

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return _project_config("merge_non_auto_partition")

    def test_merge_non_auto_partition(self, project):
        run_dbt(["seed"])
        # First run: full refresh creates partitioned table with rows 1-5
        run_dbt(["run"])
        first = project.run_sql(
            "select count(*) from {schema}.model", fetch="one"
        )
        assert first[0] == 5

        # Insert a new row outside the incremental window to verify only the
        # is_incremental() subset enters the merge.
        project.run_sql(
            "insert into {schema}.seed_partition "
            "values (6,'Frank',TIMESTAMP'2024-10-06 00:00:00','p06')"
        )
        # Second run: id>=3 → 3,4,5 update, 6 inserts (this is the path that
        # used to fail with ODPS-0123031)
        run_dbt(["run"])
        rows = project.run_sql(
            "select id from {schema}.model order by id", fetch="all"
        )
        assert [r[0] for r in rows] == [1, 2, 3, 4, 5, 6]


_merge_auto_partition_sql = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key='id',
    partition_by={"fields": "event_time", "data_types": "timestamp"}
) }}
select id, name, event_time
from {{ source('raw', 'seed') }}
{% if is_incremental() %}
  where id >= 3
{% endif %}
"""


class TestMergeAutoPartition:
    """saleaudited-like: timestamp/date partition triggers auto-partition path."""

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"seed_partition.csv": seeds_csv}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "model.sql": _merge_auto_partition_sql,
            "schema.yml": schema_yml,
        }

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return _project_config("merge_auto_partition")

    def test_merge_auto_partition(self, project):
        run_dbt(["seed"])
        run_dbt(["run"])
        project.run_sql(
            "insert into {schema}.seed_partition "
            "values (6,'Frank',TIMESTAMP'2024-10-06 00:00:00','p06')"
        )
        run_dbt(["run"])
        rows = project.run_sql(
            "select id from {schema}.model order by id", fetch="all"
        )
        assert [r[0] for r in rows] == [1, 2, 3, 4, 5, 6]


_merge_auto_partition_generated_sql = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key='id',
    partition_by={
        "fields": "event_time",
        "data_types": "timestamp",
        "granularity": "month",
        "generate_column_name": "pt_month"
    }
) }}
select id, name, event_time
from {{ source('raw', 'seed') }}
{% if is_incremental() %}
  where id >= 3
{% endif %}
"""


class TestMergeAutoPartitionWithGenColName:
    """Auto-partition + named generated column. The synthesized partition column
    must NOT be added to the INSERT list (it's derived by MaxCompute)."""

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"seed_partition.csv": seeds_csv}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "model.sql": _merge_auto_partition_generated_sql,
            "schema.yml": schema_yml,
        }

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return _project_config("merge_auto_partition_gen")

    def test_merge_auto_partition_with_gen_col(self, project):
        run_dbt(["seed"])
        run_dbt(["run"])
        project.run_sql(
            "insert into {schema}.seed_partition "
            "values (6,'Frank',TIMESTAMP'2024-10-06 00:00:00','p06')"
        )
        run_dbt(["run"])
        rows = project.run_sql(
            "select id from {schema}.model order by id", fetch="all"
        )
        assert [r[0] for r in rows] == [1, 2, 3, 4, 5, 6]


_merge_multi_field_partition_sql = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key='id',
    partition_by={"fields": "name,pt", "data_types": "string,string"}
) }}
select id, name, event_time, pt
from {{ source('raw', 'seed') }}
{% if is_incremental() %}
  where id >= 3
{% endif %}
"""


class TestMergeMultiFieldPartition:
    """Non-auto partition with multiple fields — both must reach INSERT."""

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"seed_partition.csv": seeds_csv}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "model.sql": _merge_multi_field_partition_sql,
            "schema.yml": schema_yml,
        }

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return _project_config("merge_multi_field_partition")

    def test_merge_multi_field_partition(self, project):
        run_dbt(["seed"])
        run_dbt(["run"])
        project.run_sql(
            "insert into {schema}.seed_partition "
            "values (6,'Frank',TIMESTAMP'2024-10-06 00:00:00','p06')"
        )
        run_dbt(["run"])
        rows = project.run_sql(
            "select id from {schema}.model order by id", fetch="all"
        )
        assert [r[0] for r in rows] == [1, 2, 3, 4, 5, 6]


_merge_exclude_columns_partition_sql = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key='id',
    merge_exclude_columns=['name'],
    partition_by={"fields": "pt", "data_types": "string"}
) }}
select id,
{% if is_incremental() %}
  case when id = 3 then 'X' else name end as name,
{% else %}
  name,
{% endif %}
  event_time, pt
from {{ source('raw', 'seed') }}
{% if is_incremental() %}
  where id >= 3
{% endif %}
"""


class TestMergeExcludeColumnsWithPartition:
    """User-specified merge_exclude_columns must compose with the implicit
    partition-column exclusion (don't accidentally drop user's column from
    update_columns OR fail to exclude partition column).

    Simulates a source/target name divergence inside the model SQL because
    MaxCompute seed tables are non-transactional and don't accept UPDATE.
    """

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"seed_partition.csv": seeds_csv}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "model.sql": _merge_exclude_columns_partition_sql,
            "schema.yml": schema_yml,
        }

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return _project_config("merge_exclude_partition")

    def test_merge_exclude_columns_partition(self, project):
        run_dbt(["seed"])
        run_dbt(["run"])

        # Add a new row outside the incremental window (won't enter MERGE)
        # and a row that lands in the incremental subset (will MERGE).
        project.run_sql(
            "insert into {schema}.seed_partition "
            "values (6,'Frank',TIMESTAMP'2024-10-06 00:00:00','p06')"
        )
        # On the incremental run, the model rewrites name='X' for id=3.
        # merge_exclude_columns=['name'] must keep the target's original
        # "Carol" intact while still inserting id=6 into a partitioned target.
        run_dbt(["run"])
        rows = project.run_sql(
            "select id, name from {schema}.model order by id", fetch="all"
        )
        names = {r[0]: r[1] for r in rows}
        assert names[3] == "Carol"
        assert names[6] == "Frank"


_merge_update_columns_partition_sql = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key='id',
    merge_update_columns=['name', 'event_time'],
    partition_by={"fields": "pt", "data_types": "string"}
) }}
select id, name, event_time, pt
from {{ source('raw', 'seed') }}
{% if is_incremental() %}
  where id >= 3
{% endif %}
"""


class TestMergeUpdateColumnsWithPartition:
    """When merge_update_columns is explicit, the implicit partition
    exclusion must NOT fight with it."""

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"seed_partition.csv": seeds_csv}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "model.sql": _merge_update_columns_partition_sql,
            "schema.yml": schema_yml,
        }

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return _project_config("merge_update_columns_partition")

    def test_merge_update_columns_partition(self, project):
        run_dbt(["seed"])
        run_dbt(["run"])
        project.run_sql(
            "insert into {schema}.seed_partition "
            "values (6,'Frank',TIMESTAMP'2024-10-06 00:00:00','p06')"
        )
        run_dbt(["run"])
        rows = project.run_sql(
            "select id from {schema}.model order by id", fetch="all"
        )
        assert [r[0] for r in rows] == [1, 2, 3, 4, 5, 6]


# =========================================================================
# delete+insert strategy
# =========================================================================

_delete_insert_non_auto_partition_sql = """
{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key='id',
    partition_by={"fields": "pt", "data_types": "string"}
) }}
select id, name, event_time, pt
from {{ source('raw', 'seed') }}
{% if is_incremental() %}
  where id >= 3
{% endif %}
"""


class TestDeleteInsertNonAutoPartition:
    @pytest.fixture(scope="class")
    def seeds(self):
        return {"seed_partition.csv": seeds_csv}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "model.sql": _delete_insert_non_auto_partition_sql,
            "schema.yml": schema_yml,
        }

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return _project_config("delete_insert_non_auto_partition")

    def test_delete_insert_non_auto_partition(self, project):
        run_dbt(["seed"])
        run_dbt(["run"])
        project.run_sql(
            "insert into {schema}.seed_partition "
            "values (6,'Frank',TIMESTAMP'2024-10-06 00:00:00','p06')"
        )
        run_dbt(["run"])
        rows = project.run_sql(
            "select id from {schema}.model order by id", fetch="all"
        )
        assert [r[0] for r in rows] == [1, 2, 3, 4, 5, 6]


_delete_insert_auto_partition_sql = """
{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key='id',
    partition_by={"fields": "event_time", "data_types": "timestamp"}
) }}
select id, name, event_time
from {{ source('raw', 'seed') }}
{% if is_incremental() %}
  where id >= 3
{% endif %}
"""


class TestDeleteInsertAutoPartition:
    """delete+insert on an auto-partition target: dest_columns excludes the
    server-derived partition col, so the INSERT branch must NOT use the
    explicit PARTITION clause — positional column-list INSERT works because
    MaxCompute back-fills the partition value from the data source column."""

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"seed_partition.csv": seeds_csv}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "model.sql": _delete_insert_auto_partition_sql,
            "schema.yml": schema_yml,
        }

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return _project_config("delete_insert_auto_partition")

    def test_delete_insert_auto_partition(self, project):
        run_dbt(["seed"])
        run_dbt(["run"])
        project.run_sql(
            "insert into {schema}.seed_partition "
            "values (6,'Frank',TIMESTAMP'2024-10-06 00:00:00','p06')"
        )
        run_dbt(["run"])
        rows = project.run_sql(
            "select id from {schema}.model order by id", fetch="all"
        )
        assert [r[0] for r in rows] == [1, 2, 3, 4, 5, 6]


# =========================================================================
# insert_overwrite strategy — guards against regression in the SELECT *
# code path (it doesn't use dest_columns, but partition discovery still
# flows through get_columns_in_relation for schema-change detection).
# =========================================================================

_insert_overwrite_dynamic_sql = """
{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    partition_by={"fields": "pt", "data_types": "string"}
) }}
select id, name, event_time, pt
from {{ source('raw', 'seed') }}
{% if is_incremental() %}
  where id >= 3
{% endif %}
"""


class TestInsertOverwriteDynamicPartition:
    @pytest.fixture(scope="class")
    def seeds(self):
        return {"seed_partition.csv": seeds_csv}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "model.sql": _insert_overwrite_dynamic_sql,
            "schema.yml": schema_yml,
        }

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return _project_config("insert_overwrite_dynamic_partition")

    def test_insert_overwrite_dynamic_partition(self, project):
        run_dbt(["seed"])
        run_dbt(["run"])
        project.run_sql(
            "insert into {schema}.seed_partition "
            "values (6,'Frank',TIMESTAMP'2024-10-06 00:00:00','p06')"
        )
        run_dbt(["run"])
        rows = project.run_sql(
            "select id from {schema}.model order by id", fetch="all"
        )
        # dynamic insert_overwrite replaces partitions present in source;
        # rows 1,2 stay (their partitions weren't touched), 3-5 are replaced
        # in their own partitions, 6 is new
        assert [r[0] for r in rows] == [1, 2, 3, 4, 5, 6]
