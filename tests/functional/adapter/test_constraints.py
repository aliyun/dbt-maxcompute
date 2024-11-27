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


class TestTableConstraintsColumnsEqual(BaseTableConstraintsColumnsEqual):
    @pytest.fixture
    def string_type(self):
        return "string"

    @pytest.fixture
    def int_type(self):
        return "int"

    @pytest.fixture
    def data_types(self, schema_int_type, int_type, string_type):
        # sql_column_value, schema_data_type, error_data_type
        return [
            ["1", schema_int_type, int_type],
            ["'1'", string_type, string_type],
            ["true", "bool", "bool"],
            ["timestamp'2013-11-03 00:00:00.07'", "timestamp", "timestamp"],
            ["1BD", "decimal(1,0)", "decimal(1,0)"],
        ]

    pass


class TestViewConstraintsColumnsEqual(BaseViewConstraintsColumnsEqual):
    @pytest.fixture
    def string_type(self):
        return "string"

    @pytest.fixture
    def int_type(self):
        return "int"

    @pytest.fixture
    def data_types(self, schema_int_type, int_type, string_type):
        # sql_column_value, schema_data_type, error_data_type
        return [
            ["1", schema_int_type, int_type],
            ["'1'", string_type, string_type],
            ["true", "bool", "bool"],
            ["timestamp'2013-11-03 00:00:00.07'", "timestamp", "timestamp"],
            ["1BD", "decimal(1,0)", "decimal(1,0)"],
        ]

    pass


class TestIncrementalConstraintsColumnsEqual(BaseIncrementalConstraintsColumnsEqual):
    @pytest.fixture
    def string_type(self):
        return "string"

    @pytest.fixture
    def int_type(self):
        return "int"

    @pytest.fixture
    def data_types(self, schema_int_type, int_type, string_type):
        # sql_column_value, schema_data_type, error_data_type
        return [
            ["1", schema_int_type, int_type],
            ["'1'", string_type, string_type],
            ["true", "bool", "bool"],
            ["timestamp'2013-11-03 00:00:00.07'", "timestamp", "timestamp"],
            ["1BD", "decimal(1,0)", "decimal(1,0)"],
        ]

    pass


@pytest.mark.skip(reason="See comments.")
class TestTableConstraintsRuntimeDdlEnforcement(BaseConstraintsRuntimeDdlEnforcement):
    """
    This test will pass by modify the expected_sql.

    However, dbt-maxcompute is not-support for all the tested constraints.
    This test is meaningless.
    """

    pass


class TestTableConstraintsRollback(BaseConstraintsRollback):
    @pytest.fixture(scope="class")
    def expected_error_messages(self):
        return ["Can't insert a null value into not-null column"]

    pass


@pytest.mark.skip(reason="Like TestTableConstraintsRuntimeDdlEnforcement")
class TestIncrementalConstraintsRuntimeDdlEnforcement(
    BaseIncrementalConstraintsRuntimeDdlEnforcement
):
    pass


class TestIncrementalConstraintsRollback(BaseIncrementalConstraintsRollback):
    @pytest.fixture(scope="class")
    def expected_error_messages(self):
        return ["Can't insert a null value into not-null column"]

    pass


@pytest.mark.skip(reason="Support, but set timezone is not support.")
class TestTableContractSqlHeader(BaseTableContractSqlHeader):
    pass


@pytest.mark.skip(reason="Support, but set timezone is not support.")
class TestIncrementalContractSqlHeader(BaseIncrementalContractSqlHeader):
    pass


@pytest.mark.skip(reason="Like TestTableConstraintsRuntimeDdlEnforcement")
class TestModelConstraintsRuntimeEnforcement(BaseModelConstraintsRuntimeEnforcement):
    pass


@pytest.mark.skip(reason="Like TestTableConstraintsRuntimeDdlEnforcement")
class TestConstraintQuotedColumn(BaseConstraintQuotedColumn):
    pass


class TestIncrementalForeignKeyConstraint(BaseIncrementalForeignKeyConstraint):
    pass
