import pytest
from dbt.tests.util import (
    run_dbt,
)

seeds_base_csv = """
id,name,some_date
1,Easton,1981-05-20T06:46:51
2,Lillian,1978-09-03T18:10:33
3,Jeremiah,1982-03-11T03:59:51
4,Nolan,1976-05-06T20:21:35
5,Hannah,1982-06-23T05:41:26
6,Eleanor,1991-08-10T23:12:21
7,Lily,1971-03-29T14:58:02
8,Jonathan,1988-02-26T02:55:24
9,Adrian,1994-02-09T13:14:23
10,Nora,1976-03-01T16:51:39
""".lstrip()

models_table__sql = """
{{ config(
    materialized='table',
    sql_header='set a=b;'
) }}
select * from {{ source('raw', 'seed') }}

""".lstrip()

models_view__sql = """
{{ config(
    materialized='view',
    sql_header='set a=b;'
) }}
select * from {{ source('raw', 'seed') }}

""".lstrip()

models_incremental__sql = """
{{ config(
    materialized='incremental',
    sql_header='set a=b;'
) }}
select * from {{ source('raw', 'seed') }}

""".lstrip()

models_materialized_view__sql = """
{{ config(
    materialized='materialized_view',
    sql_header='set a=b;'
) }}
select * from {{ source('raw', 'seed') }}

""".lstrip()

schema_base_yml = """
version: 2
sources:
  - name: raw
    schema: "{{ target.schema }}"
    tables:
      - name: seed
        identifier: "{{ var('seed_name', 'base') }}"
"""


class BaseTestSqlHeader:

    @pytest.fixture(scope="class")
    def seeds(self):
        return {
            "base.csv": seeds_base_csv,
        }

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "table.sql": models_table__sql,
            "view.sql": models_view__sql,
            "incremental.sql": models_incremental__sql,
            "materialized_view.sql": models_materialized_view__sql,
            "schema.yml": schema_base_yml,
        }

    def test_base(self, project):
        # seed command
        results = run_dbt(["seed"])
        # seed result length
        print(results)

        # run command
        results = run_dbt()
        # run result length
        print(results)


class TestSqlHeader(BaseTestSqlHeader):
    pass
