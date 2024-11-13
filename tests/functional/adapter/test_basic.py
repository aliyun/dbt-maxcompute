import pytest

from dbt.tests.adapter.basic.test_base import BaseSimpleMaterializations
from dbt.tests.adapter.basic.test_singular_tests import BaseSingularTests
from dbt.tests.adapter.basic.test_singular_tests_ephemeral import (
    BaseSingularTestsEphemeral,
)
from dbt.tests.adapter.basic.test_empty import BaseEmpty
from dbt.tests.adapter.basic.test_ephemeral import BaseEphemeral
from dbt.tests.adapter.basic.test_incremental import BaseIncremental
from dbt.tests.adapter.basic.test_generic_tests import BaseGenericTests
from dbt.tests.adapter.basic.test_snapshot_check_cols import BaseSnapshotCheckCols
from dbt.tests.adapter.basic.test_snapshot_timestamp import BaseSnapshotTimestamp
from dbt.tests.adapter.basic.test_adapter_methods import BaseAdapterMethod

# additional basic tests
from dbt.tests.adapter.basic.test_table_materialization import BaseTableMaterialization
from dbt.tests.adapter.basic.test_validate_connection import BaseValidateConnection


class TestSimpleMaterializationsMaxCompute(BaseSimpleMaterializations):
    # passed
    pass


class TestSingularTestsMaxCompute(BaseSingularTests):
    # passed
    pass


class TestSingularTestsEphemeralMaxCompute(BaseSingularTestsEphemeral):
    # passed
    pass


class TestEmptyMaxCompute(BaseEmpty):
    # passed
    pass


class TestEphemeralMaxCompute(BaseEphemeral):
    # passed
    pass


class TestIncrementalMaxCompute(BaseIncremental):
    # passed
    pass


class TestGenericTestsMaxCompute(BaseGenericTests):
    # passed
    pass


schema_seed__yml = """
seeds:
  - name: added
    config:
      transactional: true
"""

"""
Since the update operation on seed 'added' is involved in the test case,
'added' must be constructed into a transaction table.

This does not mean that the 'dbt snapshot' cannot be performed normally on an ordinary table.
It only matters if the user wants to update or delete operation, the target table must be the transaction table.
"""


class TestSnapshotCheckColsMaxCompute(BaseSnapshotCheckCols):
    @pytest.fixture(scope="class")
    def models(self):
        return {"seeds.yml": schema_seed__yml}

    # passed
    pass


class TestSnapshotTimestampMaxCompute(BaseSnapshotTimestamp):
    @pytest.fixture(scope="class")
    def models(self):
        return {"seeds.yml": schema_seed__yml}

    # passed
    pass


@pytest.mark.skip(reason="See below comments.")
class TestBaseAdapterMethodMaxCompute(BaseAdapterMethod):
    # passed
    """
    This UT is sometimes unstable,
    which may be due to potential problems on the MaxCompute server
    (the created View cannot be seen in a short period of time, which is not as expected)
    Therefore, the error reported by this UT does not mean that the function of the adapter is incomplete.
    """


class TestTableMaterializationMaxCompute(BaseTableMaterialization):
    pass


class TestBaseValidateConnectionMaxCompute(BaseValidateConnection):
    pass
