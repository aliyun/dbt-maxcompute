from dbt.adapters.maxcompute.relation_configs._base import MaxComputeBaseRelationConfig

from dbt.adapters.maxcompute.relation_configs._partition import (
    PartitionConfig,
)

from dbt.adapters.maxcompute.relation_configs._policies import (
    MaxComputeQuotePolicy,
    MaxComputeIncludePolicy,
)
