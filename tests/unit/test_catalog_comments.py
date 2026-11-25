import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from dbt_common.exceptions import DbtRuntimeError
from odps.errors import InvalidArgument, ODPSError

from dbt.adapters.maxcompute.catalog_comments import persist_table_comments
from dbt.adapters.maxcompute.impl import MaxComputeAdapter
from dbt.adapters.maxcompute.relation import MaxComputeRelation


TABLE = MaxComputeRelation.create(
    database="test_project", schema="target_schema", identifier="model", type="table"
)
URL = (
    "https://catalog.example/api/catalog/v1alpha/projects/test_project"
    "/schemas/target_schema/tables/model"
)


@pytest.fixture
def metadata():
    return {
        "type": "TABLE",
        "projectId": "test_project",
        "schemaName": "target_schema",
        "tableName": "model",
        "etag": "initial",
        "description": "old table",
        "labels": {"keep": "label"},
        "expirationOptions": {"lifecycleDays": 1},
        "tableSchema": {
            "fields": [
                {
                    "fieldName": "id",
                    "description": "old id",
                    "typeCategory": "BIGINT",
                    "mode": "REQUIRED",
                    "policyTags": {"names": ["keep_tag"]},
                },
                {"fieldName": "payload", "description": "keep", "typeCategory": "STRING"},
                {"fieldName": "ds", "description": "partition", "typeCategory": "STRING"},
            ]
        },
        "partitionDefinition": {"partitionedColumns": [{"field": "ds"}]},
    }


def response(table):
    return Mock(**{"json.return_value": table})


@pytest.fixture
def catalog(metadata):
    client = Mock(endpoint="https://catalog.example")
    client.get.return_value = response(metadata)
    client.put.side_effect = lambda url, *, data, headers: response(json.loads(data))
    return client


def persist(catalog, description=None, columns=None):
    return persist_table_comments(
        SimpleNamespace(catalog_rest=catalog), TABLE, description, columns or {}
    )


def test_updates_only_descriptions_in_one_put(catalog, metadata):
    description = '客户的 "表"\nC:\\temp\\'
    assert persist(catalog, description, {"id": {"description": ""}})

    catalog.get.assert_called_once_with(URL)
    catalog.put.assert_called_once()
    write = catalog.put.call_args
    assert write.args == (URL,)
    assert write.kwargs["headers"] == {"Content-Type": "application/json"}
    expected = deepcopy(metadata)
    expected["description"] = description
    expected["tableSchema"]["fields"][0]["description"] = ""
    assert json.loads(write.kwargs["data"]) == expected


def test_unchanged_comments_skip_put(catalog):
    assert persist(catalog, "old table", {"ds": {"description": "partition"}})
    catalog.get.assert_called_once_with(URL)
    catalog.put.assert_not_called()


def test_partition_column_changes_use_sql_before_any_put(catalog):
    assert not persist(
        catalog, "new table", {"id": {"description": "new id"}, "ds": {"description": "new ds"}}
    )
    catalog.put.assert_not_called()


@pytest.mark.parametrize(
    "unsupported",
    [
        {"type": "EXTERNAL"},
        {"maxLakeConfiguration": {"managed": True}},
        {"externalCatalogTableOptions": {"catalog": "external"}},
        {"tableConstraints": {"primaryKey": {"fields": ["id"]}}},
        {"etag": None},
        {"tableSchema": {}},
    ],
)
def test_unsupported_metadata_keeps_sql_path(catalog, metadata, unsupported):
    metadata.update(unsupported)
    assert not persist(catalog, "new table")
    catalog.put.assert_not_called()


def test_missing_documented_column_keeps_existing_lookup(catalog):
    assert not persist(catalog, "new table", {"missing": {"description": "not visible yet"}})
    catalog.put.assert_not_called()


def test_older_pyodps_keeps_sql_path():
    assert not persist_table_comments(SimpleNamespace(), TABLE, "new table", {})


def test_epv2_project_does_not_route_to_catalog_rest():
    adapter = object.__new__(MaxComputeAdapter)
    adapter.is_external_catalog_project = Mock(return_value=True)
    adapter.get_odps_client = Mock()
    assert not adapter.persist_table_comments_via_catalog(TABLE, "new table", {})
    adapter.get_odps_client.assert_not_called()


def test_catalog_get_failure_keeps_sql_path(catalog):
    catalog.get.side_effect = ODPSError("cannot read table metadata", status_code=403)
    assert not persist(catalog, "new table")
    catalog.put.assert_not_called()


@pytest.mark.parametrize("status", [405, 429, 501])
def test_rejected_catalog_put_keeps_sql_path(catalog, status):
    catalog.put.side_effect = ODPSError("update not accepted", status_code=status)
    assert not persist(catalog, "new table")
    catalog.get.assert_called_once_with(URL)
    catalog.put.assert_called_once()


def test_etag_conflict_merges_over_fresh_metadata(catalog, metadata):
    fresh = deepcopy(metadata)
    fresh["etag"] = "changed by another writer"
    fresh["tableSchema"]["fields"][1]["description"] = "new unrelated description"
    catalog.get.side_effect = [response(metadata), response(fresh)]
    conflict = InvalidArgument("Conflict with other alter table operation", status_code=400)

    def update(url, *, data, headers):
        body = json.loads(data)
        if body["etag"] == "initial":
            raise conflict
        return response(body)

    catalog.put.side_effect = update
    assert persist(catalog, "new table", {"id": {"description": "new id"}})
    assert catalog.get.call_count == 2
    assert catalog.put.call_count == 2
    sent = json.loads(catalog.put.call_args.kwargs["data"])
    assert sent["etag"] == fresh["etag"]
    assert sent["tableSchema"]["fields"][1]["description"] == "new unrelated description"


def test_etag_conflict_retries_at_most_once(catalog):
    catalog.put.side_effect = InvalidArgument(
        "Conflict with other alter table operation", status_code=400
    )
    with pytest.raises(InvalidArgument):
        persist(catalog, "new table")
    assert catalog.get.call_count == 2
    assert catalog.put.call_count == 2


@pytest.mark.parametrize("committed", [False, True])
def test_ambiguous_timeout_reads_back_and_never_falls_through_to_sql(catalog, metadata, committed):
    after = deepcopy(metadata)
    if committed:
        after["description"] = "new table"
    catalog.get.side_effect = [response(metadata), response(after)]
    catalog.put.side_effect = TimeoutError("write response timed out")
    if committed:
        assert persist(catalog, "new table")
    else:
        with pytest.raises(TimeoutError):
            persist(catalog, "new table")
    assert catalog.get.call_count == 2
    catalog.put.assert_called_once()


def test_success_that_did_not_apply_update_is_reported(catalog, metadata):
    catalog.put.side_effect = None
    catalog.put.return_value = response(metadata)
    with pytest.raises(DbtRuntimeError, match="did not persist"):
        persist(catalog, "new table")
    assert catalog.get.call_count == 2
    catalog.put.assert_called_once()
