import time
from dataclasses import dataclass
from functools import lru_cache
from multiprocessing.context import SpawnContext
from typing import Optional, List, Dict, Any, Set, FrozenSet, Tuple

import agate
from agate import Table
from dbt.adapters.base import ConstraintSupport, available
from dbt.adapters.base.relation import InformationSchema
from dbt.adapters.capability import (
    CapabilityDict,
    Capability,
    CapabilitySupport,
    Support,
)
from dbt.adapters.contracts.macros import MacroResolverProtocol
from dbt.adapters.contracts.relation import RelationType
from dbt.adapters.protocol import AdapterConfig
from dbt.adapters.sql import SQLAdapter
from dbt_common.contracts.constraints import ConstraintType
from dbt_common.utils import AttrDict
from odps.errors import ODPSError

from dbt.adapters.maxcompute import MaxComputeConnectionManager
from dbt.adapters.maxcompute.column import MaxComputeColumn
from dbt.adapters.maxcompute.context import GLOBAL_SQL_HINTS
from dbt.adapters.maxcompute.relation import MaxComputeRelation
from dbt.adapters.events.logging import AdapterLogger

logger = AdapterLogger("MaxCompute")


@dataclass
class MaxComputeConfig(AdapterConfig):
    partitionColumns: Optional[List[Dict[str, str]]] = None
    sqlHints: Optional[Dict[str, str]] = None


class MaxComputeAdapter(SQLAdapter):
    RELATION_TYPES = {
        "TABLE": RelationType.Table,
        "VIEW": RelationType.View,
        "MATERIALIZED_VIEW": RelationType.MaterializedView,
        "EXTERNAL": RelationType.External,
    }

    ConnectionManager = MaxComputeConnectionManager
    Relation = MaxComputeRelation
    Column = MaxComputeColumn
    AdapterSpecificConfigs = MaxComputeConfig

    CONSTRAINT_SUPPORT = {
        ConstraintType.check: ConstraintSupport.NOT_SUPPORTED,
        ConstraintType.not_null: ConstraintSupport.ENFORCED,
        ConstraintType.unique: ConstraintSupport.NOT_ENFORCED,
        ConstraintType.primary_key: ConstraintSupport.NOT_ENFORCED,
        ConstraintType.foreign_key: ConstraintSupport.NOT_SUPPORTED,
    }

    _capabilities: CapabilityDict = CapabilityDict(
        {
            Capability.TableLastModifiedMetadata: CapabilitySupport(
                support=Support.Full
            ),
            Capability.SchemaMetadataByRelations: CapabilitySupport(
                support=Support.Full
            ),
        }
    )

    def __init__(self, config, mp_context: SpawnContext) -> None:
        super().__init__(config, mp_context)
        self.connections: MaxComputeConnectionManager = self.connections

    def get_odps_client(self):
        conn = self.connections.get_thread_connection()
        return conn.handle.odps

    def get_odps_table_by_relation(self, relation: MaxComputeRelation):
        if self.get_odps_client().exist_table(
            relation.identifier, relation.project, relation.schema
        ):
            return self.get_odps_client().get_table(
                relation.identifier, relation.project, relation.schema
            )
        return None

    @lru_cache(maxsize=100)  # Cache results with no limit on size
    def support_namespace_schema(self, project: str):
        return (
            self.get_odps_client()
            .get_project(project)
            .get_property("odps.schema.model.enabled", "false")
            == "true"
        )

    ###
    # Implementations of abstract methods
    ###
    @classmethod
    def date_function(cls) -> str:
        return "current_timestamp()"

    @classmethod
    def is_cancelable(cls) -> bool:
        return True

    def drop_relation(self, relation: MaxComputeRelation) -> None:
        is_cached = self._schema_is_cached(relation.database, relation.schema)
        if is_cached:
            self.cache_dropped(relation)
        conn = self.connections.get_thread_connection()
        conn.handle.odps.delete_table(
            relation.identifier, relation.project, True, relation.schema
        )

    def truncate_relation(self, relation: MaxComputeRelation) -> None:
        # use macro to truncate
        sql = super().truncate_relation(relation)
        logger.debug(f"execute sql: {sql}")
        self.get_odps_client().execute_sql(sql)

    def rename_relation(
        self, from_relation: MaxComputeRelation, to_relation: MaxComputeRelation
    ) -> None:
        # from_relation type maybe wrong, here is a workaround.
        from_table = self.get_odps_table_by_relation(from_relation)
        from_relation = MaxComputeRelation.from_odps_table(from_table)

        # use macro to rename
        super().rename_relation(from_relation, to_relation)

    def get_columns_in_relation(self, relation: MaxComputeRelation):
        logger.debug(f"get_columns_in_relation: {relation.render()}")
        odps_table = self.get_odps_table_by_relation(relation)
        return (
            [
                MaxComputeColumn.from_odps_column(column)
                for column in odps_table.table_schema.simple_columns
            ]
            if odps_table
            else []
        )

    def execute_macro(
        self,
        macro_name: str,
        macro_resolver: Optional[MacroResolverProtocol] = None,
        project: Optional[str] = None,
        context_override: Optional[Dict[str, Any]] = None,
        kwargs: Optional[Dict[str, Any]] = None,
        needs_conn: bool = False,
    ) -> AttrDict:
        sql = super().execute_macro(
            macro_name,
            macro_resolver=macro_resolver,
            project=project,
            context_override=context_override,
            kwargs=kwargs,
            needs_conn=needs_conn,
        )
        inst = self.get_odps_client().run_sql(sql=sql, hints=GLOBAL_SQL_HINTS)
        logger.debug(f"create instance id '{inst.id}', execute_sql: '{sql}'")
        inst.wait_for_success()
        return sql

    def create_schema(self, relation: MaxComputeRelation) -> None:
        logger.debug(f"create_schema: '{relation.project}.{relation.schema}'")

        # Although the odps client has a check schema exist method, it will have a considerable delay,
        # so that it is impossible to judge how many seconds it should wait.
        # The same purpose is achieved by directly deleting and capturing the schema does not exist exception.

        try:
            self.get_odps_client().create_schema(relation.schema, relation.database)
        except ODPSError as e:
            if e.code == "ODPS-0110061":
                return
            else:
                raise e
        # self.checkSchemaDeeply(relation.schema, relation.database, True)

    def drop_schema(self, relation: MaxComputeRelation) -> None:
        logger.debug(f"drop_schema: '{relation.project}.{relation.schema}'")

        # Although the odps client has a check schema exist method, it will have a considerable delay,
        # so that it is impossible to judge how many seconds it should wait.
        # The same purpose is achieved by directly deleting and capturing the schema does not exist exception.

        try:
            self.get_odps_client().delete_schema(relation.schema, relation.database)
        except ODPSError as e:
            if e.code == "ODPS-0110061":
                return
            else:
                raise e

    def list_relations_without_caching(
        self,
        schema_relation: MaxComputeRelation,
    ) -> List[MaxComputeRelation]:
        logger.debug(f"list_relations_without_caching: {schema_relation}")
        if not self.check_schema_exists(
            schema_relation.database, schema_relation.schema
        ):
            return []
        results = self.get_odps_client().list_tables(
            project=schema_relation.database, schema=schema_relation.schema
        )
        relations = []
        for table in results:
            relations.append(MaxComputeRelation.from_odps_table(table))
        return relations

    @classmethod
    def quote(cls, identifier):
        return "`{}`".format(identifier)

    def list_schemas(self, database: str) -> List[str]:
        database = database.split(".")[0]
        database = database.strip("`")
        if not self.support_namespace_schema(database):
            return ["default"]
        res = [schema.name for schema in self.get_odps_client().list_schemas(database)]

        logger.debug(f"list_schemas: {res}")
        return res

    def check_schema_exists(self, database: str, schema: str) -> bool:
        database = database.strip("`")
        if not self.support_namespace_schema(database):
            return False
        schema = schema.strip("`")
        time.sleep(10)
        schema_exist = self.get_odps_client().exist_schema(schema, database)
        logger.debug(
            f"check_schema_exists: {database}.{schema}, answer is {schema_exist}"
        )
        return schema_exist

    def _get_one_catalog(
        self,
        information_schema: InformationSchema,
        schemas: Set[str],
        used_schemas: FrozenSet[Tuple[str, str]],
    ) -> "agate.Table":
        relations = []
        for schema in schemas:
            results = self.get_odps_client().list_tables(schema=schema)
            for odps_table in results:
                relation = MaxComputeRelation.from_odps_table(odps_table)
                relations.append(relation)
        return self._get_one_catalog_by_relations(
            information_schema, relations, used_schemas
        )

    def _get_one_catalog_by_relations(
        self,
        information_schema: InformationSchema,
        relations: List[MaxComputeRelation],
        used_schemas: FrozenSet[Tuple[str, str]],
    ) -> "agate.Table":
        sql_column_names = [
            "table_database",
            "table_schema",
            "table_name",
            "table_type",
            "table_comment",
            "column_name",
            "column_type",
            "column_index",
            "column_comment",
            "table_owner",
        ]

        sql_rows = []

        for relation in relations:
            odps_table = self.get_odps_table_by_relation(relation)
            table_database = relation.project
            table_schema = relation.schema
            table_name = relation.table

            if odps_table or odps_table.is_materialized_view:
                table_type = "view"
            else:
                table_type = "table"
            table_comment = "'" + odps_table.comment + "'"
            table_owner = odps_table.owner
            column_index = 0
            for column in odps_table.table_schema.simple_columns:
                column_name = column.name
                column_type = column.type.name
                column_comment = "'" + column.comment + "'"
                sql_rows.append(
                    (
                        table_database,
                        table_schema,
                        table_name,
                        table_type,
                        table_comment,
                        column_name,
                        column_type,
                        column_index,
                        column_comment,
                        table_owner,
                    )
                )
                column_index += 1

        table_instance = Table(sql_rows, column_names=sql_column_names)
        results = self._catalog_filter_table(table_instance, used_schemas)  # type: ignore[arg-type]
        return results

    # MaxCompute does not support transactions
    def clear_transaction(self) -> None:
        pass

    @classmethod
    def convert_text_type(cls, agate_table: "agate.Table", col_idx: int) -> str:
        return "string"

    @classmethod
    def convert_number_type(cls, agate_table: "agate.Table", col_idx: int) -> str:
        return "decimal"

    @classmethod
    def convert_integer_type(cls, agate_table: "agate.Table", col_idx: int) -> str:
        return "bigint"

    @classmethod
    def convert_datetime_type(cls, agate_table: "agate.Table", col_idx: int) -> str:
        # use timestamp but not timestamp_ntz because there is a problem with HashJoin for TIMESTAMP_NTZ type.
        return "timestamp"

    @classmethod
    def convert_time_type(cls, agate_table: "agate.Table", col_idx: int) -> str:
        # use timestamp but not timestamp_ntz because there is a problem with HashJoin for TIMESTAMP_NTZ type.
        return "timestamp"

    # TODO: standardize_grants_dict method may also be overridden

    @available.parse(lambda *a, **k: [])
    def get_column_schema_from_query(self, sql: str) -> List[MaxComputeColumn]:
        """Get a list of the Columns with names and data types from the given sql."""
        _, cursor = self.connections.add_select_query(sql)
        columns = [
            self.Column.create(column_name, column_type_code)
            # https://peps.python.org/pep-0249/#description
            for column_name, column_type_code, *_ in cursor.description
        ]
        return columns
