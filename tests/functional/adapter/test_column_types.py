import pytest
from dbt.tests.adapter.column_types.test_column_types import BaseColumnTypes

model_sql = """
select
    CAST(1 AS smallint) as smallint_col,
    CAST(2 AS int) as int_col,
    CAST(3 AS bigint) as bigint_col,
    CAST(4.0 AS float) as real_col,
    CAST(5.0 AS double) as double_col,
    CAST(6.0 AS decimal) as numeric_col,
    CAST('7' AS STRING) as text_col,
    CAST('8' AS varchar(20)) as varchar_col
"""

schema_yml = """
version: 2
models:
  - name: model
    data_tests:
      - is_type:
          column_map:
            smallint_col: ['integer', 'number']
            int_col: ['integer', 'number']
            bigint_col: ['integer', 'number']
            real_col: ['float', 'number']
            double_col: ['float', 'number']
            numeric_col: ['numeric', 'number']
            text_col: ['string', 'not number']
            varchar_col: ['string', 'not number']
"""


class BaseMaxComputeColumnTypes(BaseColumnTypes):
    @pytest.fixture(scope="class")
    def models(self):
        return {"model.sql": model_sql, "schema.yml": schema_yml}

    def test_run_and_test(self, project):
        self.run_and_test()


class TestMaxComputeColumnTypes(BaseMaxComputeColumnTypes):
    pass
