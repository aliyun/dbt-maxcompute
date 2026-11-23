import json
from multiprocessing import get_context
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from dbt.adapters.contracts.relation import RelationType
from dbt_common.exceptions import DbtRuntimeError
from odps.errors import ODPSError

from dbt.adapters.maxcompute import external_catalog
from dbt.adapters.maxcompute.external_catalog import ExternalCatalogMetadata
from dbt.adapters.maxcompute.impl import MaxComputeAdapter
from dbt.adapters.maxcompute.relation_configs._partition import PartitionConfig


PROJECT = "lake_project"
SCHEMA = "warehouse"
TABLE = "orders"


@pytest.fixture
def adapter_and_client():
    client = Mock(endpoint="https://maxcompute.example/api")
    client.get_project.return_value = SimpleNamespace(
        is_external_catalog_bound="true",
    )
    adapter = MaxComputeAdapter(SimpleNamespace(log_cache_events=False), get_context("spawn"))
    adapter.get_odps_client = Mock(return_value=client)
    return adapter, client


def _return_result(client, data):
    client.execute_sql.return_value.get_task_results.return_value = {"AnonymousSQLTask": data}


def _description():
    return {
        "Owner": "owner",
        "TableComment": "lake orders",
        "NativeColumns": [
            {"Name": "amount", "Type": "DECIMAL(12,2)", "Comment": "order amount"},
            {"Name": "memo", "Type": "VARCHAR(32)", "Nullable": "false"},
        ],
        "PartitionColumns": [{"Name": "ds", "Type": "STRING"}],
    }


def test_project_flag_cache_includes_internal_projects_and_is_scoped_by_endpoint():
    cache = ExternalCatalogMetadata()
    client = Mock(endpoint="https://maxcompute.example/api")
    client.get_project.side_effect = [
        SimpleNamespace(is_external_catalog_bound="true"),
        SimpleNamespace(is_external_catalog_bound="false"),
    ]
    for _ in range(2):
        assert cache.is_bound(client, PROJECT)
        assert not cache.is_bound(client, "internal")
    assert client.get_project.call_args_list == [call(PROJECT), call("internal")]

    other_endpoint = Mock(endpoint="https://another.example/api")
    other_endpoint.get_project.return_value = SimpleNamespace(is_external_catalog_bound=False)
    assert not cache.is_bound(other_endpoint, PROJECT)
    other_endpoint.get_project.assert_called_once_with(PROJECT)


def test_project_check_supports_old_pyodps_and_does_not_cache_a_failed_lookup():
    client = Mock(endpoint="https://maxcompute.example/api")
    client.get_project.side_effect = [
        ODPSError("Project metadata unavailable"),
        SimpleNamespace(
            properties={"external_project_properties": '{"isExternalCatalogBound":"True"}'}
        ),
    ]
    cache = ExternalCatalogMetadata()
    with pytest.raises(ODPSError, match="Project metadata unavailable"):
        cache.is_bound(client, PROJECT)
    assert cache.is_bound(client, PROJECT)


def test_lists_schemas_and_relations_through_epv2_sql(adapter_and_client):
    adapter, client = adapter_and_client
    _return_result(client, '"schema_name"\r\n"warehouse"\r\n"default"\r\n')
    assert adapter.list_schemas(PROJECT) == [SCHEMA, "default"]
    assert adapter.check_schema_exists(PROJECT, SCHEMA)
    assert not adapter.check_schema_exists(PROJECT, "gone")
    client.list_schemas.assert_not_called()
    client.exist_schema.assert_not_called()

    _return_result(client, '"user_name","table_name"\r\n"","orders"\r\n')
    schema_relation = adapter.Relation.create(database=PROJECT, schema=SCHEMA)
    relations = adapter.list_relations_without_caching(schema_relation)
    assert [(r.database, r.schema, r.identifier, r.type) for r in relations] == [
        (PROJECT, SCHEMA, TABLE, RelationType.Table)
    ]
    args, kwargs = client.execute_sql.call_args
    assert args == ("SHOW TABLES",)
    assert (kwargs["project"], kwargs["default_schema"]) == (PROJECT, SCHEMA)
    assert kwargs["hints"]["odps.sql.select.output.format"] == "csv"
    client.get_project.assert_called_once_with(PROJECT)
    client.list_tables.assert_not_called()


def test_describe_identifies_existing_relation_and_preserves_column_types(adapter_and_client):
    adapter, client = adapter_and_client
    _return_result(client, json.dumps(_description()))

    relation = adapter.get_relation(PROJECT, SCHEMA, TABLE)
    assert relation.type == RelationType.Table
    amount, memo, ds = adapter.get_columns_in_relation(relation)
    assert amount.data_type == "DECIMAL(12, 2)"
    assert amount.comment == "order amount"
    assert memo.declared_size() == 32
    assert not memo.table_column.nullable
    assert ds.name == "ds" and ds.is_partition
    args, kwargs = client.execute_sql.call_args
    assert args == ("DESC EXTENDED `orders`",)
    assert (kwargs["project"], kwargs["default_schema"]) == (PROJECT, SCHEMA)
    assert kwargs["hints"]["odps.sql.select.output.format"] == "json"
    client.get_project.assert_called_once_with(PROJECT)
    client.get_table.assert_not_called()


def test_describe_distinguishes_absent_table_from_metadata_errors(adapter_and_client):
    adapter, client = adapter_and_client
    client.execute_sql.side_effect = [
        ODPSError("Table not found", code="ODPS-0130131"),
        ODPSError("Permission denied", code="ODPS-0420095"),
    ]
    assert adapter.get_relation(PROJECT, SCHEMA, "missing") is None
    with pytest.raises(ODPSError, match="Permission denied"):
        adapter.get_relation(PROJECT, SCHEMA, TABLE)


def test_catalog_uses_epv2_table_details(adapter_and_client):
    adapter, client = adapter_and_client
    _return_result(client, json.dumps(_description()))
    relation = adapter.Relation.create(database=PROJECT, schema=SCHEMA, identifier=TABLE)
    catalog = adapter._get_one_catalog_by_relations(
        None, [relation], frozenset({(PROJECT, SCHEMA)})
    )
    assert [row["column_name"] for row in catalog.rows] == ["amount", "memo"]
    assert catalog.rows[0]["table_comment"] == "lake orders"
    assert catalog.rows[0]["table_owner"] == "owner"
    client.get_table.assert_not_called()


def test_catalog_resolves_the_source_project_when_it_differs_from_the_connection(
    adapter_and_client,
):
    adapter, client = adapter_and_client
    client.project = "internal"
    client.execute_sql.return_value.get_task_results.side_effect = [
        {"AnonymousSQLTask": '"user_name","table_name"\n"","orders"\n'},
        {"AnonymousSQLTask": json.dumps(_description())},
    ]
    catalog = adapter._get_one_catalog(None, {SCHEMA}, frozenset({(PROJECT, SCHEMA)}))
    assert catalog.rows[0]["table_database"] == PROJECT
    assert catalog.rows[0]["table_name"] == TABLE
    assert all(query.kwargs["project"] == PROJECT for query in client.execute_sql.call_args_list)
    client.list_tables.assert_not_called()


def test_list_tables_distinguishes_missing_schema_from_connector_errors(adapter_and_client):
    adapter, client = adapter_and_client
    schema_relation = adapter.Relation.create(database=PROJECT, schema=SCHEMA)
    _return_result(client, '"user_name","table_name"\n')
    assert adapter.list_relations_without_caching(schema_relation) == []

    client.execute_sql.side_effect = [
        ODPSError(
            f"Catalog.DatabaseNotExistException: Database {SCHEMA} does not exist.",
            code="ODPS-0110005",
        ),
        ODPSError("Connector access denied", code="ODPS-0110005"),
    ]
    assert adapter.list_relations_without_caching(schema_relation) == []
    with pytest.raises(ODPSError, match="Connector access denied"):
        adapter.list_relations_without_caching(schema_relation)


def test_invalid_list_payload_does_not_hide_catalog_objects(adapter_and_client):
    adapter, client = adapter_and_client
    _return_result(client, "unexpected output")
    with pytest.raises(DbtRuntimeError, match="Missing schema_name"):
        adapter.list_schemas(PROJECT)


def test_partition_values_use_column_order(adapter_and_client):
    adapter, _ = adapter_and_client
    config = PartitionConfig(fields=["ds", "region"])
    result = adapter.epv2_partition_values(config, {"region": "cn", "DS": "2026-11-23"})
    assert list(result.items()) == [("ds", "2026-11-23"), ("region", "cn")]


@pytest.mark.parametrize("values", [None, {}, {"ds": None}, {"ds": True}, {"ds": "", "DS": "a"}])
def test_incomplete_or_ambiguous_epv2_partition_values_are_rejected(adapter_and_client, values):
    adapter, _ = adapter_and_client
    with pytest.raises(DbtRuntimeError, match="partition_values"):
        adapter.epv2_partition_values(PartitionConfig(fields=["ds"]), values)


def test_epv2_overwrite_requires_a_fresh_lookup_with_no_matching_partition(adapter_and_client):
    adapter, client = adapter_and_client
    relation = adapter.Relation.create(database=PROJECT, schema=SCHEMA, identifier=TABLE)
    values = {"ds": "2026-11-23", "region": "us"}
    metadata = _description()
    metadata["PartitionColumns"].append({"Name": "region", "Type": "STRING"})
    for result, expected_error in [
        ('"ds=2026-11-23/region=cn"\n\n', False),
        ('"ds=2026-11-23/region=cn"\n"ds=2026-11-23/region=us"\n\n', True),
    ]:
        client.execute_sql.return_value.get_task_results.side_effect = [
            {"AnonymousSQLTask": json.dumps(metadata)},
            {"AnonymousSQLTask": result},
        ]
        if expected_error:
            with pytest.raises(DbtRuntimeError, match="only write new partitions"):
                adapter.epv2_new_partition_write(relation, values)
        else:
            assert adapter.epv2_new_partition_write(relation, values) == (
                ["amount", "memo"],
                ["`ds`='2026-11-23'", "`region`='us'"],
            )
        assert client.execute_sql.call_args.args == ("SHOW PARTITIONS `orders`",)


def test_numeric_partition_values_use_the_canonical_key(adapter_and_client):
    adapter, client = adapter_and_client
    metadata = _description()
    metadata["PartitionColumns"] = [{"Name": "version", "Type": "BIGINT"}]
    relation = adapter.Relation.create(database=PROJECT, schema=SCHEMA, identifier=TABLE)
    for listing in ("\n", '"version=1"\n\n'):
        client.execute_sql.return_value.get_task_results.side_effect = [
            {"AnonymousSQLTask": json.dumps(metadata)},
            {"AnonymousSQLTask": listing},
        ]
        values = {"version": "001"}
        if listing.strip():
            with pytest.raises(DbtRuntimeError, match="already exists"):
                adapter.epv2_new_partition_write(relation, values)
        else:
            assert adapter.epv2_new_partition_write(relation, values)[1] == ["`version`=1"]


@pytest.mark.parametrize(
    "listing,error",
    [("invalid\n", "Invalid SHOW PARTITIONS"), ('"ds=old"\n', "partition query limit")],
)
def test_unverifiable_partition_listing_blocks_writes(
    adapter_and_client, monkeypatch, listing, error
):
    adapter, client = adapter_and_client
    monkeypatch.setattr(external_catalog, "_PARTITION_LIST_LIMIT", 1)
    relation = adapter.Relation.create(database=PROJECT, schema=SCHEMA, identifier=TABLE)
    client.execute_sql.return_value.get_task_results.side_effect = [
        {"AnonymousSQLTask": json.dumps(_description())},
        {"AnonymousSQLTask": listing},
    ]
    with pytest.raises(DbtRuntimeError, match=error):
        adapter.epv2_new_partition_write(relation, {"ds": "new"})


def test_epv2_write_rejects_partition_config_inconsistent_with_target(adapter_and_client):
    adapter, client = adapter_and_client
    relation = adapter.Relation.create(database=PROJECT, schema=SCHEMA, identifier=TABLE)
    _return_result(client, json.dumps(_description()))
    with pytest.raises(DbtRuntimeError, match="must match the partition columns"):
        adapter.epv2_new_partition_write(relation, {"region": "cn"})
    client.execute_sql.assert_called_once()
