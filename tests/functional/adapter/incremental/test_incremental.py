import pytest
from dbt.artifacts.schemas.results import RunStatus
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

        # no unique_key test

    def test__no_unique_keys(self, project):
        """with no unique keys, seed and model should match"""

        expected_fields = self.get_expected_fields(relation="seed", seed_rows=8)
        test_case_fields = self.get_test_fields(
            project, seed="seed", incremental_model="no_unique_key", update_sql_file="add_new_rows"
        )
        self.check_scenario_correctness(expected_fields, test_case_fields, project)

    # unique_key as str tests
    def test__empty_str_unique_key(self, project):
        """with empty string for unique key, seed and model should match"""

        expected_fields = self.get_expected_fields(relation="seed", seed_rows=8)
        test_case_fields = self.get_test_fields(
            project,
            seed="seed",
            incremental_model="empty_str_unique_key",
            update_sql_file="add_new_rows",
        )
        self.check_scenario_correctness(expected_fields, test_case_fields, project)

    def test__one_unique_key(self, project):
        """with one unique key, model will overwrite existing row"""

        expected_fields = self.get_expected_fields(
            relation="one_str__overwrite", seed_rows=7, opt_model_count=1
        )
        test_case_fields = self.get_test_fields(
            project,
            seed="seed",
            incremental_model="str_unique_key",
            update_sql_file="duplicate_insert",
            opt_model_count=self.update_incremental_model("one_str__overwrite"),
        )
        self.check_scenario_correctness(expected_fields, test_case_fields, project)

    def test__bad_unique_key(self, project):
        """expect compilation error from unique key not being a column"""

        (status, exc) = self.fail_to_build_inc_missing_unique_key_column(
            incremental_model_name="not_found_unique_key"
        )

        assert status == RunStatus.Error
        assert "thisisnotacolumn" in exc.lower()

    # test unique_key as list
    def test__empty_unique_key_list(self, project):
        """with no unique keys, seed and model should match"""

        expected_fields = self.get_expected_fields(relation="seed", seed_rows=8)
        test_case_fields = self.get_test_fields(
            project,
            seed="seed",
            incremental_model="empty_unique_key_list",
            update_sql_file="add_new_rows",
        )
        self.check_scenario_correctness(expected_fields, test_case_fields, project)

    def test__unary_unique_key_list(self, project):
        """with one unique key, model will overwrite existing row"""

        expected_fields = self.get_expected_fields(
            relation="unique_key_list__inplace_overwrite", seed_rows=7, opt_model_count=1
        )
        test_case_fields = self.get_test_fields(
            project,
            seed="seed",
            incremental_model="unary_unique_key_list",
            update_sql_file="duplicate_insert",
            opt_model_count=self.update_incremental_model("unique_key_list__inplace_overwrite"),
        )
        self.check_scenario_correctness(expected_fields, test_case_fields, project)

    def test__duplicated_unary_unique_key_list(self, project):
        """with two of the same unique key, model will overwrite existing row"""

        expected_fields = self.get_expected_fields(
            relation="unique_key_list__inplace_overwrite", seed_rows=7, opt_model_count=1
        )
        test_case_fields = self.get_test_fields(
            project,
            seed="seed",
            incremental_model="duplicated_unary_unique_key_list",
            update_sql_file="duplicate_insert",
            opt_model_count=self.update_incremental_model("unique_key_list__inplace_overwrite"),
        )
        self.check_scenario_correctness(expected_fields, test_case_fields, project)

    def test__trinary_unique_key_list(self, project):
        """with three unique keys, model will overwrite existing row"""

        expected_fields = self.get_expected_fields(
            relation="unique_key_list__inplace_overwrite", seed_rows=7, opt_model_count=1
        )
        test_case_fields = self.get_test_fields(
            project,
            seed="seed",
            incremental_model="trinary_unique_key_list",
            update_sql_file="duplicate_insert",
            opt_model_count=self.update_incremental_model("unique_key_list__inplace_overwrite"),
        )
        self.check_scenario_correctness(expected_fields, test_case_fields, project)

    def test__trinary_unique_key_list_no_update(self, project):
        """even with three unique keys, adding distinct rows to seed does not
        cause seed and model to diverge"""

        expected_fields = self.get_expected_fields(relation="seed", seed_rows=8)
        test_case_fields = self.get_test_fields(
            project,
            seed="seed",
            incremental_model="nontyped_trinary_unique_key_list",
            update_sql_file="add_new_rows",
        )
        self.check_scenario_correctness(expected_fields, test_case_fields, project)

    def test__bad_unique_key_list(self, project):
        """expect compilation error from unique key not being a column"""

        (status, exc) = self.fail_to_build_inc_missing_unique_key_column(
            incremental_model_name="not_found_unique_key_list"
        )

        assert status == RunStatus.Error
        assert "thisisnotacolumn" in exc.lower()

    pass
