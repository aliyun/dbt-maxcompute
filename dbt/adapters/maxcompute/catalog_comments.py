import json
from copy import deepcopy
from typing import Any, Optional
from urllib.parse import quote

from dbt.adapters.events.logging import AdapterLogger
from dbt_common.exceptions import DbtRuntimeError
from odps import ODPS
from odps.errors import ODPSError
from odps.rest import RestClient

from dbt.adapters.maxcompute.relation import MaxComputeRelation


logger = AdapterLogger("MaxCompute")
_CATALOG_PATH = "/api/catalog/v1alpha"
_SPECIAL_TABLE_FIELDS = (
    "externalDataConfiguration",
    "externalCatalogTableOptions",
    "maxLakeConfiguration",
    "snapshotDefinition",
    # Catalog rejects a round trip of the existing primary-key constraint on Delta tables.
    "tableConstraints",
)


def _columns(table: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(table, dict):
        return {}
    schema = table.get("tableSchema")
    fields = schema.get("fields") if isinstance(schema, dict) else None
    if not isinstance(fields, list):
        return {}
    columns = {}
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get("fieldName"), str):
            return {}
        name = field["fieldName"]
        if name in columns:
            return {}
        columns[name] = field
    return columns


def _prepare_update(
    table: Any, description: Optional[str], column_descriptions: dict[str, str]
) -> tuple[bool, Optional[dict[str, Any]]]:
    """Return whether CatalogAPI can handle the request and a payload if it needs a write."""
    columns = _columns(table)
    if (
        not columns
        or table.get("type") != "TABLE"
        or not isinstance(table.get("etag"), str)
        or not table["etag"]
        or any(table.get(field) for field in _SPECIAL_TABLE_FIELDS)
        or not column_descriptions.keys() <= columns.keys()
    ):
        return False, None

    changed_columns = {
        name: comment
        for name, comment in column_descriptions.items()
        if (columns[name].get("description") or "") != comment
    }
    if changed_columns and table.get("partitionDefinition") is not None:
        definition = table["partitionDefinition"]
        partitions = definition.get("partitionedColumns") if isinstance(definition, dict) else None
        if not isinstance(partitions, list) or any(
            not isinstance(partition, dict) or not isinstance(partition.get("field"), str)
            for partition in partitions
        ):
            return False, None
        if any(partition["field"] in changed_columns for partition in partitions):
            return False, None

    table_comment_changed = description is not None and table.get("description") != description
    if not table_comment_changed and not changed_columns:
        return True, None

    # UpdateTable requires the table definition as well as the etag, even for a table comment.
    payload = deepcopy(table)
    if table_comment_changed:
        payload["description"] = description
    for field in payload["tableSchema"]["fields"]:
        name = field["fieldName"]
        if name in changed_columns:
            field["description"] = changed_columns[name]
    return True, payload


def _comments_match(
    table: Any, description: Optional[str], column_descriptions: dict[str, str]
) -> bool:
    if not isinstance(table, dict) or table.get("type") != "TABLE":
        return False
    if description is not None and table.get("description") != description:
        return False
    columns = _columns(table)
    return all(
        name in columns and (columns[name].get("description") or "") == comment
        for name, comment in column_descriptions.items()
    )


def _readback_matches(
    catalog: RestClient,
    url: str,
    description: Optional[str],
    column_descriptions: dict[str, str],
) -> bool:
    try:
        table = catalog.get(url).json()
    except (ODPSError, OSError, ValueError):
        return False
    return _comments_match(table, description, column_descriptions)


def _etag_conflict(error: Exception) -> bool:
    return isinstance(error, ODPSError) and (
        getattr(error, "status_code", None) in (409, 412)
        or "conflict with other alter table operation" in str(error).lower()
    )


def persist_table_comments(
    odps: ODPS,
    relation: MaxComputeRelation,
    description: Optional[str],
    column_dict: dict[str, dict[str, Any]],
) -> bool:
    """Persist native table comments, or return False when SQL should handle the request."""
    column_descriptions = {
        name: column["description"]
        for name, column in column_dict.items()
        if isinstance(column.get("description"), str)
    }
    if description is None and not column_descriptions:
        return True

    project = relation.database
    identifier = relation.identifier
    if not project or not identifier:
        return False

    try:
        catalog = getattr(odps, "catalog_rest", None)
        if catalog is None:
            return False
        url = (
            f"{catalog.endpoint.rstrip('/')}{_CATALOG_PATH}"
            f"/projects/{quote(project, safe='')}/schemas/{quote(relation.schema or 'default', safe='')}"
            f"/tables/{quote(identifier, safe='')}"
        )
        table = catalog.get(url).json()
    except (ODPSError, OSError, ValueError):
        logger.debug("Catalog table metadata unavailable before comment update. Using SQL.")
        return False

    retried_conflict = False
    while True:
        supported, payload = _prepare_update(table, description, column_descriptions)
        if not supported:
            if retried_conflict:
                raise DbtRuntimeError(f"Catalog table metadata changed for {relation.render()}")
            return False
        if payload is None:
            return True

        try:
            updated = catalog.put(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            ).json()
        except (ODPSError, OSError, ValueError) as error:
            if not retried_conflict and _etag_conflict(error):
                retried_conflict = True
                table = catalog.get(url).json()
                continue
            status = getattr(error, "status_code", None)
            if status in (405, 429, 501):
                return False
            if not isinstance(status, int) or status == 408 or status >= 500:
                if _readback_matches(catalog, url, description, column_descriptions):
                    return True
            raise

        if _comments_match(updated, description, column_descriptions) or _readback_matches(
            catalog, url, description, column_descriptions
        ):
            return True
        raise DbtRuntimeError(
            f"CatalogAPI did not persist the requested comments for {relation.render()}"
        )
