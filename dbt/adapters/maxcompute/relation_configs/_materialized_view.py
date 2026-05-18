from dataclasses import dataclass
from typing import Any, Dict, Optional, List

from dbt.adapters.contracts.relation import (
    RelationConfig,
    ComponentName,
)
from dbt.adapters.maxcompute.relation_configs._base import MaxComputeBaseRelationConfig
from dbt.adapters.maxcompute.relation_configs._partition import (
    PartitionConfig,
)
from dbt.adapters.maxcompute.utils import quote_string, quote_ref


@dataclass(frozen=True, eq=True, unsafe_hash=True)
class MaxComputeMaterializedViewConfig(MaxComputeBaseRelationConfig):
    name: str
    project: str
    schema: str
    lifecycle: Optional[int] = None
    build_deferred: bool = False
    columns: Optional[List[str]] = None
    column_comment: Optional[Dict[str, str]] = None
    disable_rewrite: bool = False
    table_comment: Optional[str] = None
    partition_by: Optional[PartitionConfig] = None
    tblProperties: Optional[Dict[str, str]] = None

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "MaxComputeMaterializedViewConfig":
        # required
        kwargs_dict: Dict[str, Any] = {
            "name": cls._render_part(ComponentName.Identifier, config_dict["name"]),
            "schema": cls._render_part(ComponentName.Schema, config_dict["schema"]),
            "project": cls._render_part(ComponentName.Database, config_dict["project"]),
        }
        for key, value in config_dict.items():
            if key in ["name", "schema", "project"]:
                pass
            kwargs_dict[key] = value

        if partition := config_dict.get("partition_by"):
            kwargs_dict.update({"partition_by": PartitionConfig.parse(partition)})

        materialized_view: "MaxComputeMaterializedViewConfig" = super().from_dict(kwargs_dict)
        return materialized_view

    @classmethod
    def parse_mc_table(cls, table) -> Dict[str, Any]:
        # Only fields that MaxCompute reliably reports back are populated.
        # Anything we cannot read (tblProperties, build_deferred, column_comment, ...)
        # is left as the dataclass default so it never falsely triggers a change.
        schema_obj = table.get_schema()
        partition_fields = [p.name for p in (table.table_schema.partitions or [])]
        return {
            "name": table.name,
            "project": table.project.name,
            "schema": schema_obj.name if schema_obj else "default",
            "lifecycle": table.lifecycle if table.lifecycle and table.lifecycle > 0 else None,
            "table_comment": table.comment or None,
            "disable_rewrite": not table.is_materialized_view_rewrite_enabled,
            "partition_by": (
                {"fields": ",".join(partition_fields)} if partition_fields else None
            ),
        }

    @classmethod
    def parse_relation_config(cls, relation_config: RelationConfig) -> Dict[str, Any]:
        config_dict = {
            "name": relation_config.identifier,
            "schema": relation_config.schema,
            "project": relation_config.database,
        }
        items = [
            "lifecycle",
            "build_deferred",
            "columns",
            "column_comment",
            "disable_rewrite",
            "table_comment",
            "partition_by",
            "tblProperties",
        ]

        if relation_config:
            for item in items:
                if item in relation_config.config:
                    config_dict.update({item: relation_config.config[item]})
        return config_dict

    def get_coordinate(self) -> str:
        if self.schema is None:
            return f"{self.name}"
        if self.project is None:
            return f"{self.schema}.{self.name}"
        return f"{self.project}.{self.schema}.{self.name}"

    def create_table_sql(self) -> str:
        sql = f"CREATE MATERIALIZED VIEW IF NOT EXISTS {self.get_coordinate()}\n"
        if self.lifecycle and self.lifecycle > 0:
            sql += f"LIFECYCLE {self.lifecycle}\n"
        if self.build_deferred:
            sql += "BUILD DEFERRED\n"
        if self.columns and len(self.columns) > 0:
            sql += "("
            for column in self.columns:
                if self.column_comment and column in self.column_comment:
                    sql += (
                        f"{quote_ref(column)} COMMENT {quote_string(self.column_comment[column])}"
                    )
                else:
                    sql += f"{quote_ref(column)}"
                sql += ", "
            sql = sql[:-2]
            sql += ")\n"
        if self.disable_rewrite:
            sql += " DISABLE REWRITE\n"
        if self.table_comment:
            sql += f"COMMENT {quote_string(self.table_comment)}\n"
        if self.partition_by and len(self.partition_by.fields) > 0:
            sql += f"PARTITIONED BY({self.partition_by.render(False)})\n"
        if self.tblProperties and len(self.tblProperties) > 0:
            sql += "TBLPROPERTIES( "
            for k, v in self.tblProperties.items():
                sql += f'"{k}"="{v}", '
            sql = sql[:-2]
            sql += ")\n"
        return sql
