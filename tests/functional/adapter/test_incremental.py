import pytest
from dbt.tests.adapter.incremental.test_incremental_unique_id import BaseIncrementalUniqueKey
from dbt.tests.adapter.incremental.test_incremental_merge_exclude_columns import (
    BaseMergeExcludeColumns,
)
from dbt.tests.adapter.incremental.test_incremental_predicates import BaseIncrementalPredicates
from dbt.tests.adapter.incremental.test_incremental_on_schema_change import (
    BaseIncrementalOnSchemaChange,
)
from dbt.tests.adapter.incremental.test_incremental_microbatch import BaseMicrobatch


class TestMergeExcludeColumnsMaxCompute(BaseMergeExcludeColumns):
    pass


@pytest.mark.skip(reason="MaxCompute Api not support freeze time.")
class TestMicrobatchMaxCompute(BaseMicrobatch):
    pass


class TestIncrementalOnSchemaChange(BaseIncrementalOnSchemaChange):
    pass


class TestIncrementalPredicatesDeleteInsert(BaseIncrementalPredicates):
    pass


class TestPredicatesDeleteInsert(BaseIncrementalPredicates):
    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"models": {"+predicates": ["id != 2"], "+incremental_strategy": "delete+insert"}}


seeds__add_new_rows_sql = """
-- Insert statement which when applied to seed.csv sees incremental model
--   grow in size while not (necessarily) diverging from the seed itself.

-- insert two new rows, both of which should be in incremental model
--   with any unique columns
insert into {schema}.seed
    (state, county, city, last_visit_date)
values ('WA','King','Seattle',DATE'2022-02-01');

insert into {schema}.seed
    (state, county, city, last_visit_date)
values ('CA','Los Angeles','Los Angeles',DATE'2022-02-01');

"""
seeds__duplicate_insert_sql = """
-- Insert statement which when applied to seed.csv triggers the inplace
--   overwrite strategy of incremental models. Seed and incremental model
--   diverge.

-- insert new row, which should not be in incremental model
--  with primary or first three columns unique
insert into {schema}.seed
    (state, county, city, last_visit_date)
values ('CT','Hartford','Hartford',DATE'2022-02-14');

"""
models__expected__one_str__overwrite_sql = """
{{
    config(
        materialized='table'
    )
}}

select
    'CT' as state,
    'Hartford' as county,
    'Hartford' as city,
    cast('2022-02-14' as date) as last_visit_date
union all
select 'MA','Suffolk','Boston',DATE'2020-02-12'
union all
select 'NJ','Mercer','Trenton',DATE'2022-01-01'
union all
select 'NY','Kings','Brooklyn',DATE'2021-04-02'
union all
select 'NY','New York','Manhattan',DATE'2021-04-01'
union all
select 'PA','Philadelphia','Philadelphia',DATE'2021-05-21'

"""

models__expected__unique_key_list__inplace_overwrite_sql = """
{{
    config(
        materialized='table'
    )
}}

select
    'CT' as state,
    'Hartford' as county,
    'Hartford' as city,
    cast('2022-02-14' as date) as last_visit_date
union all
select 'MA','Suffolk','Boston',DATE'2020-02-12'
union all
select 'NJ','Mercer','Trenton',DATE'2022-01-01'
union all
select 'NY','Kings','Brooklyn',DATE'2021-04-02'
union all
select 'NY','New York','Manhattan',DATE'2021-04-01'
union all
select 'PA','Philadelphia','Philadelphia',DATE'2021-05-21'

"""


@pytest.mark.skip(
    reason="The function is ok, but it cannot run successfully due to some reasons on the server side."
)
class TestIncrementalUniqueKey(BaseIncrementalUniqueKey):

    @pytest.fixture(scope="class")
    def models(self):
        from dbt.tests.adapter.incremental.test_incremental_unique_id import (
            models__empty_str_unique_key_sql,
            models__str_unique_key_sql,
            models__duplicated_unary_unique_key_list_sql,
            models__not_found_unique_key_list_sql,
            models__not_found_unique_key_sql,
            models__empty_unique_key_list_sql,
            models__no_unique_key_sql,
            models__trinary_unique_key_list_sql,
            models__nontyped_trinary_unique_key_list_sql,
            models__unary_unique_key_list_sql,
        )

        return {
            "trinary_unique_key_list.sql": models__trinary_unique_key_list_sql,
            "nontyped_trinary_unique_key_list.sql": models__nontyped_trinary_unique_key_list_sql,
            "unary_unique_key_list.sql": models__unary_unique_key_list_sql,
            "not_found_unique_key.sql": models__not_found_unique_key_sql,
            "empty_unique_key_list.sql": models__empty_unique_key_list_sql,
            "no_unique_key.sql": models__no_unique_key_sql,
            "empty_str_unique_key.sql": models__empty_str_unique_key_sql,
            "str_unique_key.sql": models__str_unique_key_sql,
            "duplicated_unary_unique_key_list.sql": models__duplicated_unary_unique_key_list_sql,
            "not_found_unique_key_list.sql": models__not_found_unique_key_list_sql,
            "expected": {
                "one_str__overwrite.sql": models__expected__one_str__overwrite_sql,
                "unique_key_list__inplace_overwrite.sql": models__expected__unique_key_list__inplace_overwrite_sql,
            },
        }

    @pytest.fixture(scope="class")
    def seeds(self):
        from dbt.tests.adapter.incremental.test_incremental_unique_id import seeds__seed_csv

        return {
            "duplicate_insert.sql": seeds__duplicate_insert_sql,
            "seed.csv": seeds__seed_csv,
            "add_new_rows.sql": seeds__add_new_rows_sql,
        }

    pass
