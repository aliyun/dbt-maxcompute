import pytest
from dbt.tests.util import (
    run_dbt,
)

models__sql = """
{{ config(
    materialized='raw'
) }}
create table test(c1 bigint) lifecycle 1;
""".lstrip()


class BaseTestRaw:
    @pytest.fixture(scope="class")
    def models(self):
        return {"model.sql": models__sql}

    def test_base(self, project):
        results = run_dbt()
        # run result length
        print(results)


class TestRaw(BaseTestRaw):
    pass
