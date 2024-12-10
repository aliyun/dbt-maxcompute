from dataclasses import dataclass

from dbt.adapters.base.relation import Policy


class MaxComputeIncludePolicy(Policy):
    database: bool = True
    schema: bool = True
    identifier: bool = True


@dataclass
class MaxComputeQuotePolicy(Policy):
    database: bool = True
    schema: bool = True
    identifier: bool = True
