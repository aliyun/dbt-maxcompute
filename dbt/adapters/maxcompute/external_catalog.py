import csv
import io
import json
from threading import Lock
from typing import Any
from urllib.parse import unquote

from dbt_common.exceptions import DbtRuntimeError
from odps import ODPS
from odps.errors import ODPSError
from odps.models import TableSchema

from dbt.adapters.maxcompute.column import MaxComputeColumn
from dbt.adapters.maxcompute.utils import quote_ref


# MaxCompute documents a 10,000-partition query limit. A result at the limit
# cannot establish that a particular partition is absent.
_PARTITION_LIST_LIMIT = 10_000


class ExternalCatalogMetadata:
    """Read EP v2 metadata through SQL, as the MaxCompute Java SDK does."""

    def __init__(self) -> None:
        self._project_flags: dict[tuple[str, str], bool] = {}
        self._lock = Lock()

    def is_bound(self, client: ODPS, project: str) -> bool:
        key = (client.endpoint, project)
        with self._lock:
            if key not in self._project_flags:
                metadata = client.get_project(project)
                flag = getattr(metadata, "is_external_catalog_bound", None)
                if flag is None:
                    properties = (metadata.properties or {}).get("external_project_properties")
                    if properties:
                        # PyODPS versions without the XML field still expose project properties.
                        flag = json.loads(properties).get("isExternalCatalogBound")
                self._project_flags[key] = str(flag).lower() == "true"
            return self._project_flags[key]

    def list_schemas(self, client: ODPS, project: str) -> list[str]:
        return self._list_names(client, project, "default", "SHOW SCHEMAS", "schema_name")

    def list_tables(self, client: ODPS, project: str, schema: str) -> list[str]:
        try:
            return self._list_names(client, project, schema, "SHOW TABLES", "table_name")
        except ODPSError as exc:
            message = str(exc)
            if (
                exc.code == "ODPS-0110005"
                and "DatabaseNotExistException" in message
                and f"Database {schema} does not exist" in message
            ):
                return []
            raise

    def describe_table(
        self, client: ODPS, project: str, schema: str, table: str
    ) -> dict[str, Any] | None:
        sql = f"DESC EXTENDED {quote_ref(table)}"
        try:
            raw = self._execute(client, project, schema, sql, "json")
        except ODPSError as exc:
            if exc.code == "ODPS-0130131":
                return None
            raise
        try:
            metadata = json.loads(raw)
        except ValueError as exc:
            raise DbtRuntimeError(f"Invalid JSON from {sql} in {project}.{schema}") from exc
        if not isinstance(metadata, dict) or not any(
            key in metadata for key in ("NativeColumns", "PartitionColumns")
        ):
            raise DbtRuntimeError(f"Missing column metadata from {sql} in {project}.{schema}")
        return metadata

    def require_new_partition(
        self,
        client: ODPS,
        project: str,
        schema: str,
        table: str,
        values: dict[str, str],
        fixed_char_fields: set[str],
    ) -> None:
        # EP v2 accepts a PARTITION clause on SHOW PARTITIONS but can ignore it.
        sql = f"SHOW PARTITIONS {quote_ref(table)}"
        raw = self._execute(client, project, schema, sql, "csv")
        relation = f"{project}.{schema}.{table}"
        try:
            rows = (row for row in csv.reader(io.StringIO(raw), strict=True) if row)
            for count, row in enumerate(rows, start=1):
                if len(row) != 1:
                    raise ValueError
                fields = row[0].split("/")
                if len(fields) != len(values):
                    raise ValueError
                parts = dict(field.split("=", 1) for field in fields)
                parts = {unquote(key).lower(): value for key, value in parts.items()}
                if parts.keys() != values.keys():
                    raise ValueError
                matches = []
                for key, value in parts.items():
                    decoded = unquote(value, errors="strict")
                    if key in fixed_char_fields:
                        value, decoded = value.rstrip(" "), decoded.rstrip(" ")
                    matches.append(values[key] in (value, decoded))
                if all(matches):
                    raise DbtRuntimeError(
                        f"EP v2 can only write new partitions. Partition {values} "
                        f"already exists in {relation}."
                    )
                if count >= _PARTITION_LIST_LIMIT:
                    raise DbtRuntimeError(
                        f"Could not verify that partition {values} is absent in {relation}. "
                        "SHOW PARTITIONS reached the partition query limit."
                    )
        except (ValueError, csv.Error) as exc:
            raise DbtRuntimeError(
                f"Could not verify that partition {values} is absent in {relation}. "
                "Invalid SHOW PARTITIONS result."
            ) from exc

    @staticmethod
    def columns(metadata: dict[str, Any]) -> list[MaxComputeColumn]:
        columns = []
        for field, is_partition in (("NativeColumns", False), ("PartitionColumns", True)):
            for entry in metadata.get(field) or []:
                column = TableSchema.TableColumn(
                    name=entry["Name"],
                    type=entry["Type"],
                    comment=entry.get("Comment"),
                    nullable=str(entry.get("Nullable", True)).lower() == "true",
                )
                columns.append(
                    MaxComputeColumn.from_odps_column(column, is_partition=is_partition)
                )
        return columns

    def _list_names(
        self, client: ODPS, project: str, schema: str, sql: str, field: str
    ) -> list[str]:
        raw = self._execute(client, project, schema, sql, "csv")
        rows = csv.DictReader(io.StringIO(raw))
        if field not in (rows.fieldnames or []):
            raise DbtRuntimeError(f"Missing {field} in {sql} results for {project}.{schema}")
        return [row[field] for row in rows]

    @staticmethod
    def _execute(client: ODPS, project: str, schema: str, sql: str, output_format: str) -> str:
        instance = client.execute_sql(
            sql,
            project=project,
            default_schema=schema,
            hints={
                "odps.namespace.schema": "true",
                "odps.sql.allow.namespace.schema": "true",
                "odps.sql.select.output.format": output_format,
            },
        )
        result = instance.get_task_results().get("AnonymousSQLTask")
        if result is None:
            raise DbtRuntimeError(f"Missing {sql} result for {project}.{schema}")
        return result
