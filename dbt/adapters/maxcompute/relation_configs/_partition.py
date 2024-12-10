from dataclasses import dataclass
from typing import Any, Dict, Optional

import dbt_common.exceptions
from dbt.adapters.contracts.relation import RelationConfig
from dbt_common.dataclass_schema import dbtClassMixin, ValidationError
from odps.models.table import Table as MaxComputeTable


@dataclass
class PartitionConfig(dbtClassMixin):
    field: str
    data_type: str = "string"
    granularity: str = "day"
    copy_partitions: bool = False

    @classmethod
    def auto_partition(self) -> bool:
        return self.data_type in ["timestamp", "date", "datetime", "timestamp_ntz"]

    def render(self, alias: Optional[str] = None):
        column: str = self.field
        if alias:
            column = f"{alias}.{column}"
        return column

    def render_wrapped(self, alias: Optional[str] = None):
        """Wrap the partitioning column when time involved to ensure it is properly cast to matching time."""
        # if data type is going to be truncated, no need to wrap
        return self.render(alias)

    @classmethod
    def parse(cls, raw_partition_by) -> Optional["PartitionConfig"]:
        if raw_partition_by is None:
            return None
        try:
            cls.validate(raw_partition_by)
            return cls.from_dict(
                {
                    key: (value.lower() if isinstance(value, str) else value)
                    for key, value in raw_partition_by.items()
                }
            )
        except ValidationError as exc:
            raise dbt_common.exceptions.base.DbtValidationError(
                "Could not parse partition config"
            ) from exc
        except TypeError:
            raise dbt_common.exceptions.CompilationError(
                f"Invalid partition_by config:\n"
                f"  Got: {raw_partition_by}\n"
                f'  Expected a dictionary with "field" and "data_type" keys'
            )

    @classmethod
    def parse_model_node(cls, relation_config: RelationConfig) -> Dict[str, Any]:
        """
        Parse model node into a raw config for `PartitionConfig.parse`
        """
        config_dict: Dict[str, Any] = relation_config.config.extra.get("partition_by")
        return config_dict

    @classmethod
    def parse_mc_table(cls, table: MaxComputeTable) -> Dict[str, Any]:
        """
        Parse the MC Table object into a raw config for `PartitionConfig.parse`
        """
        return {}
