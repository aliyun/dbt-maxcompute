"""Regression test for delete+insert strategy with list unique_key.

`maxcompute__get_delete_insert_merge_sql` emitted PostgreSQL-flavored
DELETE...USING when `unique_key` is a list:

    delete from <tgt>
    using <src>
    where ( <src>.<k1> = <tgt>.<k1> and <src>.<k2> = <tgt>.<k2> );

MaxCompute rejects this — its DELETE accepts either a WHERE-condition or
a WHERE...IN (subquery), never USING. Multi-column joins must be written
as `where (k1, k2) in (select k1, k2 from src)`.

No existing test in dbt-maxcompute exercises delete+insert with a list
unique_key, so the bug stayed hidden.
"""
import pytest

from dbt.tests.util import run_dbt


_model_sql = """
{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key=['a', 'b'],
    transactional=true,
    primary_keys=['a', 'b']
) }}
select 1 as a, 1 as b, 'x' as v
union all select 2, 2, 'y'
union all select 3, 3, 'z'
{% if is_incremental() %}
union all select 4, 4, 'w'
{% endif %}
"""


class TestDeleteInsertListUniqueKey:
    @pytest.fixture(scope="class")
    def models(self):
        return {"model.sql": _model_sql}

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"name": "delete_insert_list_key"}

    def test_delete_insert_list_unique_key(self, project):
        run_dbt(["run"])
        # Second run goes through the delete+insert path that emits the
        # multi-column DELETE — this is what fails today.
        run_dbt(["run"])
        rows = project.run_sql(
            "select a from {schema}.model order by a", fetch="all"
        )
        assert [r[0] for r in rows] == [1, 2, 3, 4]
