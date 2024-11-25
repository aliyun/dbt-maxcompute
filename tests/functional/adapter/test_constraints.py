import re

import pytest
from dbt.tests.adapter.constraints.test_constraints import (
    BaseTableConstraintsColumnsEqual,
    BaseViewConstraintsColumnsEqual,
    BaseIncrementalConstraintsColumnsEqual,
    BaseConstraintsRuntimeDdlEnforcement,
    BaseConstraintsRollback,
    BaseIncrementalConstraintsRuntimeDdlEnforcement,
    BaseIncrementalConstraintsRollback,
    BaseTableContractSqlHeader,
    BaseIncrementalContractSqlHeader,
    BaseModelConstraintsRuntimeEnforcement,
    BaseConstraintQuotedColumn,
    BaseIncrementalForeignKeyConstraint,
)


@pytest.mark.skip(reason="Pass 2 of 4 tests, Need to modify case properties")
class TestTableConstraintsColumnsEqual(BaseTableConstraintsColumnsEqual):
    @pytest.fixture
    def string_type(self):
        return "STRING"

    @pytest.fixture
    def int_type(self):
        return "INT"

    @pytest.fixture
    def data_types(self, schema_int_type, int_type, string_type):
        # sql_column_value, schema_data_type, error_data_type
        return [
            ["1", schema_int_type, int_type],
            ["'1'", string_type, string_type],
            ["true", "bool", "BOOL"],
            ["timestamp'2013-11-03 00:00:00-07'", "timestamp", "timestamp"],
            ["timestamp'2013-11-03 00:00:00-07'", "timestamp", "timestamp"],
            ["ARRAY['a','b','c']", "text[]", "STRINGARRAY"],
            ["ARRAY[1,2,3]", "int[]", "INTEGERARRAY"],
            ["'1'::numeric", "numeric", "DECIMAL"],
            ["""'{"bar": "baz", "balance": 7.77, "active": false}'::json""", "json", "JSON"],
        ]

    pass


@pytest.mark.skip(reason="Pass 2 of 4 tests, Need to modify case properties")
class TestViewConstraintsColumnsEqual(BaseViewConstraintsColumnsEqual):
    pass


class TestIncrementalConstraintsColumnsEqual(BaseIncrementalConstraintsColumnsEqual):
    pass


class TestTableConstraintsRuntimeDdlEnforcement(BaseConstraintsRuntimeDdlEnforcement):
    pass


class TestTableConstraintsRollback(BaseConstraintsRollback):
    pass


class TestIncrementalConstraintsRuntimeDdlEnforcement(
    BaseIncrementalConstraintsRuntimeDdlEnforcement
):
    pass


class TestIncrementalConstraintsRollback(BaseIncrementalConstraintsRollback):
    pass


class TestTableContractSqlHeader(BaseTableContractSqlHeader):
    pass


class TestIncrementalContractSqlHeader(BaseIncrementalContractSqlHeader):
    pass


class TestModelConstraintsRuntimeEnforcement(BaseModelConstraintsRuntimeEnforcement):
    pass


class TestConstraintQuotedColumn(BaseConstraintQuotedColumn):
    pass


class TestIncrementalForeignKeyConstraint(BaseIncrementalForeignKeyConstraint):
    pass
