import pytest
from dbt.tests.adapter.incremental.test_incremental_unique_id import BaseIncrementalUniqueKey
from dbt.tests.adapter.incremental.test_incremental_merge_exclude_columns import BaseMergeExcludeColumns
from dbt.tests.adapter.incremental.test_incremental_predicates import BaseIncrementalPredicates
from dbt.tests.adapter.incremental.test_incremental_on_schema_change import BaseIncrementalOnSchemaChange
from dbt.tests.adapter.incremental.test_incremental_microbatch import BaseMicrobatch


@pytest.mark.skip(reason="The incremental strategy 'merge' is not valid for this adapter")
class TestMergeExcludeColumnsMaxCompute(BaseMergeExcludeColumns):
    pass


@pytest.mark.skip(reason="MaxCompute Api not support freeze time.")
class TestMicrobatchMaxCompute(BaseMicrobatch):
    pass


@pytest.mark.skip(
    reason="This test is OK, but need execute 'setproject odps.schema.evolution.enable=true;'")
class TestIncrementalOnSchemaChange(BaseIncrementalOnSchemaChange):
    pass


# dbt/include/global_project/macros/materializations/models/incremental/on_schema_change.sql
# TODO: alter_relation_add_remove_columns
@pytest.mark.skip(reason="The incremental strategy 'delete+insert' is not valid for this adapter")
class TestIncrementalPredicatesDeleteInsert(BaseIncrementalPredicates):
    pass


@pytest.mark.skip(reason="The incremental strategy 'delete+insert' is not valid for this adapter")
class TestPredicatesDeleteInsert(BaseIncrementalPredicates):
    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"models": {"+predicates": ["id != 2"], "+incremental_strategy": "delete+insert"}}


@pytest.mark.skip(reason="Need to modify case")
class TestIncrementalUniqueKey(BaseIncrementalUniqueKey):
    pass
