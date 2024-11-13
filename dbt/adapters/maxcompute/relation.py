from dataclasses import dataclass, field
from typing import FrozenSet, Optional, TypeVar

from dbt.adapters.base.relation import BaseRelation, InformationSchema
from dbt.adapters.contracts.relation import RelationType, Path, Policy
from odps.models import Table

Self = TypeVar("Self", bound="MaxComputeRelation")


@dataclass
class OdpsIncludePolicy(Policy):
    database: bool = True
    schema: bool = True
    identifier: bool = True


@dataclass(frozen=True, eq=False, repr=False)
class MaxComputeRelation(BaseRelation):
    quote_character: str = "`"
    # subquery alias name is not required in MaxCompute
    require_alias: bool = False

    def without_quote(self):
        return self.quote(False, False, False)

    include_policy: Policy = field(default_factory=lambda: OdpsIncludePolicy())

    renameable_relations: FrozenSet[RelationType] = field(
        default_factory=lambda: frozenset(
            {
                RelationType.View,
                RelationType.Table,
            }
        )
    )

    replaceable_relations: FrozenSet[RelationType] = field(
        default_factory=lambda: frozenset(
            {
                RelationType.View,
                RelationType.Table,
            }
        )
    )

    @property
    def project(self):
        return self.database

    @property
    def is_transactional(self):
        return self.get("transactional", False)

    def information_schema(
        self, identifier: Optional[str] = None
    ) -> "MaxComputeInformationSchema":
        return MaxComputeInformationSchema.from_relation(self, identifier)

    @classmethod
    def from_odps_table(cls, table: Table):
        schema = table.get_schema()
        schema = schema.name if schema else "default"

        is_view = table.is_virtual_view or table.is_materialized_view

        return cls.create(
            database=table.project.name,
            schema=schema,
            identifier=table.name,
            type=RelationType.View if is_view else RelationType.Table,
        )


@dataclass(frozen=True, eq=False, repr=False)
class MaxComputeInformationSchema(InformationSchema):
    quote_character: str = "`"

    @classmethod
    def get_path(cls, relation: BaseRelation, information_schema_view: Optional[str]) -> Path:
        return Path(
            database="SYSTEM_CATALOG",
            schema="INFORMATION_SCHEMA",
            identifier=information_schema_view,
        )

    @classmethod
    def get_include_policy(cls, relation, information_schema_view):
        return relation.include_policy.replace(database=True, schema=True, identifier=True)

    @classmethod
    def get_quote_policy(
        cls,
        relation,
        information_schema_view: Optional[str],
    ) -> Policy:
        return relation.quote_policy.replace(database=False, schema=False, identifier=False)
