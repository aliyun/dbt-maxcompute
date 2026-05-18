"""Regression test for `incremental_strategy='append'` on a non-auto
partitioned target.

dbt-maxcompute had no `maxcompute__get_incremental_append_sql` override,
so the default emitted

    insert into <tgt> (<cols incl partition>)
    (select <cols incl partition> from <src>)

For a non-auto partitioned MaxCompute table this fails the same way the
delete+insert path used to: the partition column appears in the data
column list without a PARTITION clause, and MaxCompute either errors or
mis-routes rows. The append path needs its own override that emits
`INSERT INTO <tgt> PARTITION(<pt>) SELECT <data_cols>, <pt> FROM <src>`
when the target is non-auto partitioned.
"""
import pytest

from dbt.tests.util import run_dbt


_seed_csv = """
id,name,pt
1,Alice,p01
2,Bob,p02
3,Carol,p03
4,Dave,p04
5,Eve,p05
""".lstrip()

_schema_yml = """
version: 2
sources:
  - name: raw
    schema: "{{ target.schema }}"
    tables:
      - name: src
        identifier: append_part_src
"""

_model_sql = """
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    partition_by={"fields": "pt", "data_types": "string"}
) }}
select id, name, pt
from {{ source('raw', 'src') }}
{% if is_incremental() %}
  where id >= 3
{% endif %}
"""


class TestIncrementalAppendNonAutoPartition:
    @pytest.fixture(scope="class")
    def seeds(self):
        return {"append_part_src.csv": _seed_csv}

    @pytest.fixture(scope="class")
    def models(self):
        return {"model.sql": _model_sql, "schema.yml": _schema_yml}

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"name": "incremental_append_partition"}

    def test_append_non_auto_partition(self, project):
        run_dbt(["seed"])
        # First run: full-refresh inserts rows 1-5
        run_dbt(["run"])
        # Second run: incremental branch appends rows 3,4,5 again
        run_dbt(["run"])
        rows = project.run_sql(
            "select id from {schema}.model order by id", fetch="all"
        )
        # append doesn't dedupe → 1,2,3,3,4,4,5,5
        assert sorted(r[0] for r in rows) == [1, 2, 3, 3, 4, 4, 5, 5]
