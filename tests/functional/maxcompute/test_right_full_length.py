"""Regression test for the right() macro when length == len(string).

Original bug: the third argument to substr was `length(string_text) - 1`
instead of the requested `length_expression`. For every shorter slice the
substring happened to be bounded by string end and the bug was invisible;
only when caller asks for the full string does the off-by-one surface.

Concretely, right('abc', 3) used to return 'ab'.

dbt-core's BaseRight fixture (abcdef/3, fishtown/4, december/5, december/0)
never exercises this boundary, so the regression slipped through.
"""
import pytest

from dbt.tests.util import run_dbt


_model_sql = """
{{ config(materialized='table') }}
with src as (
    select 'abc' as s,   3 as n,  'abc'   as expected union all
    select 'hello',      5,       'hello'           union all
    select 'a',          1,       'a'               union all
    select 'december',   8,       'december'
)
select s, n, expected, {{ dbt.right('s', 'n') }} as actual
from src
"""


class TestRightFullLength:
    @pytest.fixture(scope="class")
    def models(self):
        return {"model.sql": _model_sql}

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"name": "right_full_length"}

    def test_right_returns_full_string_when_length_matches(self, project):
        run_dbt(["run"])
        rows = project.run_sql(
            "select s, n, expected, actual from {schema}.model order by s",
            fetch="all",
        )
        for s, n, expected, actual in rows:
            assert actual == expected, (
                f"right({s!r}, {n}) == {actual!r}, expected {expected!r}"
            )
