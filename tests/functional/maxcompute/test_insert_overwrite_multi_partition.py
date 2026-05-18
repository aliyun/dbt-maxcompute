"""Regression test for insert_overwrite with multi-column partition_by.

`mc_generate_incremental_insert_overwrite_build_sql` had a guard
`if partition_by.fields|length != 1: raise_compiler_error('requires the
partition_by config')`. The error text is wrong (it claims partition_by
is missing) and the guard itself is wrong — MaxCompute fully supports
multi-column dynamic partition for INSERT OVERWRITE. This test pins
that compose case end-to-end.
"""
import pytest

from dbt.tests.util import run_dbt


_model_sql = """
{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    partition_by={"fields": "k1,k2", "data_types": "string,string"}
) }}
select id, val, k1, k2
from {{ source('raw', 'src') }}
"""

_seed_csv = """
id,val,k1,k2
1,a,p1,q1
2,b,p1,q2
3,c,p2,q1
""".lstrip()

_schema_yml = """
version: 2
sources:
  - name: raw
    schema: "{{ target.schema }}"
    tables:
      - name: src
        identifier: io_multi_part_src
"""


class TestInsertOverwriteMultiPartition:
    @pytest.fixture(scope="class")
    def seeds(self):
        return {"io_multi_part_src.csv": _seed_csv}

    @pytest.fixture(scope="class")
    def models(self):
        return {"model.sql": _model_sql, "schema.yml": _schema_yml}

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"name": "insert_overwrite_multi_part"}

    def test_insert_overwrite_multi_column_partition(self, project):
        run_dbt(["seed"])
        run_dbt(["run"])
        # Insert a new row landing in a new k1,k2 partition; dynamic
        # insert_overwrite must touch only the partitions present in source.
        project.run_sql(
            "insert into {schema}.io_multi_part_src "
            "values (4,'d','p3','q1')"
        )
        run_dbt(["run"])
        rows = project.run_sql(
            "select id from {schema}.model order by id", fetch="all"
        )
        assert [r[0] for r in rows] == [1, 2, 3, 4]
