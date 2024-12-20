from dbt.tests.adapter.dbt_show.test_dbt_show import (
    BaseShowSqlHeader,
    BaseShowLimit,
    BaseShowDoesNotHandleDoubleLimit,
)


class TestPostgresShowSqlHeader(BaseShowSqlHeader):
    pass


class TestPostgresShowLimit(BaseShowLimit):
    pass


class TestShowDoesNotHandleDoubleLimit(BaseShowDoesNotHandleDoubleLimit):
    DATABASE_ERROR_MESSAGE = "ODPS-0130161"
    pass
