import pytest
from dbt.tests.adapter.incremental.test_incremental_predicates import BaseIncrementalPredicates


class TestIncrementalPredicatesInsertOverwrite(BaseIncrementalPredicates):
    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {
            "models": {
                "+incremental_predicates": ["id != 2"],
                "+incremental_strategy": "insert_overwrite",
            }
        }

    pass
