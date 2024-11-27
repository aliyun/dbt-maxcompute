from dbt.tests.adapter.dbt_debug.test_dbt_debug import (
    BaseDebugPostgres,
    BaseDebugInvalidProjectPostgres,
)


class TestDebugMaxCompute(BaseDebugPostgres):
    pass


class TestDebugInvalidProjectPostgres(BaseDebugInvalidProjectPostgres):
    pass
