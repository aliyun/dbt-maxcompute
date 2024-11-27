import pytest
from dbt.tests.adapter.utils.data_types.test_type_int import BaseTypeInt
from dbt.tests.adapter.utils.data_types.test_type_bigint import BaseTypeBigInt
from dbt.tests.adapter.utils.data_types.test_type_boolean import BaseTypeBoolean
from dbt.tests.adapter.utils.data_types.test_type_float import BaseTypeFloat
from dbt.tests.adapter.utils.data_types.test_type_numeric import BaseTypeNumeric
from dbt.tests.adapter.utils.data_types.test_type_string import BaseTypeString
from dbt.tests.adapter.utils.data_types.test_type_timestamp import BaseTypeTimestamp


class TestTypeBigIntMaxCompute(BaseTypeBigInt):
    pass


class TestTypeBoolean(BaseTypeBoolean):
    pass


schema_seed_float__yml = """
seeds:
  - name: expected
    config:
      column_types: {
        float_col: 'float'
      }
"""


class TestTypeFloat(BaseTypeFloat):
    @pytest.fixture(scope="class")
    def models(self):
        from dbt.tests.adapter.utils.data_types.test_type_float import models__actual_sql

        return {
            "actual.sql": self.interpolate_macro_namespace(models__actual_sql, "type_float"),
            "schema.yml": schema_seed_float__yml,
        }

    pass


schema_seed_int__yml = """
seeds:
  - name: expected
    config:
      column_types: {
        int_col: 'int'
      }
"""


class TestTypeInt(BaseTypeInt):
    @pytest.fixture(scope="class")
    def models(self):
        from dbt.tests.adapter.utils.data_types.test_type_int import models__actual_sql

        return {
            "actual.sql": self.interpolate_macro_namespace(models__actual_sql, "type_int"),
            "schema.yml": schema_seed_int__yml,
        }

    pass


schema_seed_numeric__yml = """
seeds:
  - name: expected
    config:
      column_types:
        numeric_col: 'decimal(28,6)'
"""


class TestTypeNumeric(BaseTypeNumeric):
    def numeric_fixture_type(self):
        return "decimal(28,6)"

    @pytest.fixture(scope="class")
    def seeds(self):
        from dbt.tests.adapter.utils.data_types.test_type_numeric import seeds__expected_csv

        return {"expected.csv": seeds__expected_csv, "schema.yml": schema_seed_numeric__yml}

    pass


class TestTypeString(BaseTypeString):
    pass


class TestTypeTimestamp(BaseTypeTimestamp):
    pass
