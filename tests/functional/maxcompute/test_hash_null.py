"""Regression test for the hash() macro on NULL input.

Original bug: `case when {{ expression }} = NULL then md5('') else md5(...) end`
— `x = NULL` is always NULL under three-valued logic, so the THEN branch is
never taken. NULL inputs fell to ELSE and returned md5(NULL) = NULL instead
of the md5('') that the macro intends.

dbt-core's BaseHash fixture only tests non-NULL inputs, so this case has to
live in our own suite.
"""
import hashlib

import pytest

from dbt.tests.util import run_dbt


_model_sql = """
{{ config(materialized='table') }}
with src as (
    select 1 as id, cast('hello' as string) as val
    union all
    select 2 as id, cast(null as string) as val
)
select id, {{ dbt.hash('val') }} as hashed
from src
"""


class TestHashNullInput:
    @pytest.fixture(scope="class")
    def models(self):
        return {"model.sql": _model_sql}

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"name": "hash_null"}

    def test_hash_handles_null(self, project):
        run_dbt(["run"])
        rows = project.run_sql(
            "select id, hashed from {schema}.model order by id", fetch="all"
        )
        by_id = {r[0]: r[1] for r in rows}

        assert by_id[1] == hashlib.md5(b"hello").hexdigest()
        # The macro's stated contract: NULL hashes to md5('').
        # Pre-fix, `null = NULL` is NULL, so this row came back as NULL.
        assert by_id[2] == hashlib.md5(b"").hexdigest()
