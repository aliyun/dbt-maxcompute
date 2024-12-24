from dataclasses import dataclass, field
from typing import Optional, List

import dbt_common.exceptions
from dbt_common.dataclass_schema import dbtClassMixin


@dataclass
class PartitionConfig(dbtClassMixin):
    granularity: str = "day"
    copy_partitions: bool = False

    fields: List[str] = field(default_factory=list)
    data_types: List[str] = field(default_factory=list)

    def auto_partition(self) -> bool:
        for t in self.data_types:
            if t.lower() in ["timestamp", "date", "datetime", "timestamp_ntz"]:
                return True
        return False

    def render(self, with_type: bool = True) -> str:
        default_value = len(self.data_types) == 0
        res = ""
        for i, field in enumerate(self.fields):
            if with_type:
                if default_value:
                    column = f"{field} string"
                else:
                    column = f"{field} {self.data_types[i]}"
            else:
                column = field
            res += f"{column}, "
        res = res[:-2]  # 去掉最后的逗号和空格
        return res

    @classmethod
    def parse(cls, raw_partition_by) -> Optional["PartitionConfig"]:
        if raw_partition_by is None:
            return None
        try:
            new_dict = {}
            for key, value in raw_partition_by.items():
                if key in ['fields', 'data_types']:
                    new_dict[key] = [item.strip() for item in value.split(',')]
                else:
                    new_dict[key] = value
            res = cls.from_dict(new_dict)
            res.post_validate()
            return res
        except TypeError:
            raise dbt_common.exceptions.CompilationError(
                f"Invalid partition_by config:\n"
                f"  Got: {raw_partition_by}\n"
                f'  Expected a dictionary with "fields" and "data_types" keys'
            )

    def post_validate(self):
        if 0 < len(self.data_types) != len(self.fields):
            raise dbt_common.exceptions.DbtValidationError(
                f"Invalid partition_by config:\n"
                f"  Got: {self.fields}\n"
                f"  Got: {self.data_types}\n"
                f"  Expected the same number of fields and data types"
            )
        if self.auto_partition() and len(self.fields) > 1:
            raise dbt_common.exceptions.DbtValidationError(
                f"Invalid partition_by config:\n"
                f"  Got: {self.fields}\n"
                f"  Expected a single partition column for auto partitioning"
            )
