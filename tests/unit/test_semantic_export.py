import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from dbt.adapters.maxcompute.semantic.export import MaxcClient, export_semantic
from dbt.adapters.maxcompute.semantic.files import ValidationError, read_json
from dbt.adapters.maxcompute.semantic.import_project import import_semantic
from dbt.adapters.maxcompute.semantic.reverse import normalized_joins

FIXTURE = Path(__file__).parent / "fixtures/dbt_semantic_latest.json"


class Client:
    namespace = "123"

    def __init__(self):
        self.calls = []
        self.snapshot = {
            "format": "maxc.semantic/v1",
            "source": "USER_DRAFT",
            "revisionId": "old",
            "identity": {
                "endpoint": "https://example.test",
                "namespace": "123",
                "specName": "sales",
                "specId": "id",
            },
            "object": {"dataScope": {"dataReferences": []}},
            "content": {"glossary": [{"term": "keep"}]},
        }
        self.partitions = []

    def read(self, name, *, revision=None):
        self.calls.append("read" if revision is None else ("published", revision))
        return copy.deepcopy(self.snapshot)

    def describe(self, relation):
        self.calls.append("metadata")
        cols = {
            "orders": [
                ("order_id", "INT"),
                ("customer_id", "INT"),
                ("ordered_at", "TIMESTAMP"),
                ("amount", "DECIMAL(18,2)"),
                ("status", "STRING"),
            ],
            "customers": [("customer_id", "INT"), ("region", "STRING")],
        }
        return {
            "table_name": relation["alias"],
            "project": relation["database"],
            "schema_name": relation["schema_name"],
            "columns": [{"name": n, "type": t} for n, t in cols[relation["alias"]]],
            "has_more_columns": False,
            "partition_columns": self.partitions,
        }


@pytest.fixture
def case(tmp_path):
    source = tmp_path / "manifest.json"
    source.write_bytes(FIXTURE.read_bytes())
    return Client(), source, tmp_path / "proposal.json"


def run(case, **kw):
    c, source, out = case
    return export_semantic(
        c, "sales", manifest=str(source), output=str(out), timezone="UTC", join_type="LEFT", **kw
    )


def test_export_preserves_revision_and_unowned_sections(case):
    c, _, out = case
    result = run(case)
    doc = read_json(str(out))
    assert result["metrics"] == 3 and result["relationships"] == 1
    assert result["remoteMutations"] is False
    assert doc["revisionId"] == "old"
    assert doc["content"]["glossary"] == [{"term": "keep"}]
    assert doc["content"]["semanticModel"]["approvedRelationships"][0]["joinType"] == "LEFT"
    assert "timeConventions" in doc["content"]["instructions"]
    assert c.calls == ["read", "metadata", "metadata"]
    assert out.stat().st_mode & 0o777 == 0o600


def test_existing_section_requires_explicit_replacement(case):
    c, _, out = case
    c.snapshot["content"]["instructions"] = {"generalGuidance": "manual"}
    with pytest.raises(ValidationError, match="replace existing sections"):
        run(case)
    assert not out.exists()
    assert "instructions" in run(case, replace_sections=True)["replacedSections"]


def test_volatile_manifest_metadata_does_not_change_content(case):
    c, source, out = case
    first = run(case)
    doc = read_json(str(out))
    c.snapshot = doc
    manifest = read_json(str(source))
    manifest["metadata"] = {"generated_at": "new"}
    source.write_text(json.dumps(manifest))
    second = run(case, overwrite=True)
    assert first["dbtDefinitionSha256"] == second["dbtDefinitionSha256"]
    assert read_json(str(out)) == doc


@pytest.mark.parametrize("failure", ["window", "partition", "output", "snapshot"])
def test_failures_do_not_emit_proposal(case, failure):
    c, source, out = case
    if failure == "window":
        doc = read_json(str(source))
        doc["metrics"][0]["type_params"]["window"] = {"count": 7}
        source.write_text(json.dumps(doc))
    elif failure == "partition":
        c.partitions = [{"name": "ds", "type": "STRING"}]
    elif failure == "output":
        out.write_text("keep")
    else:
        c.snapshot["source"] = "PUBLISHED"
    with pytest.raises(ValidationError):
        run(case)
    assert not out.exists() or out.read_text() == "keep"
    if failure == "output":
        assert c.calls == []


def test_command_uses_public_json_cli_without_shell(monkeypatch):
    calls = []

    def fake(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0, stdout='{"status":"success","data":{"table":{"columns":[]}}}'
        )

    monkeypatch.setattr("subprocess.run", fake)
    client = MaxcClient("/bin/maxc", "/config with spaces", "ua", "123")
    client.read("sales")
    assert client.describe({"database": "p", "schema_name": "s", "alias": "orders"}) == {
        "columns": []
    }
    assert "--namespace" in calls[0][0]
    assert "--metadata-only" in calls[1][0]
    assert calls[0][0][2] == "/config with spaces"
    assert all("shell" not in kwargs for _, kwargs in calls)


def test_dbt_adapter_operation_uses_project_target_and_wraps_errors(case, monkeypatch):
    from dbt.adapters.maxcompute.impl import MaxComputeAdapter
    from dbt_common.exceptions import DbtRuntimeError

    c, source, output = case
    project = source.parent
    monkeypatch.setattr("dbt.adapters.maxcompute.semantic.export.MaxcClient", lambda *a: c)
    adapter = SimpleNamespace(config=SimpleNamespace(project_root=str(project), target_path="."))
    source.rename(project / "semantic_manifest.json")
    result = MaxComputeAdapter.export_semantic_package(adapter, "sales", "UTC", "LEFT")
    assert Path(result["path"]) == project / "maxagent-semantic-proposal.json"
    with pytest.raises(DbtRuntimeError, match="already exists"):
        MaxComputeAdapter.export_semantic_package(adapter, "sales", "UTC", "LEFT")


@pytest.fixture
def published(case):
    client, _, proposal = case
    run(case)
    client.snapshot = read_json(str(proposal))
    client.snapshot.update(source="PUBLISHED", revisionId="published_1", object={})
    client.calls.clear()
    return client, proposal.parent / "imported"


def reverse(published, **overrides):
    client, output = published
    args = dict(
        revision="published_1",
        output=str(output),
        project_name="imported_sales",
        table_prefix="sales_dev",
        time_spine_start="2026-01-31",
        time_spine_end="2026-02-04",
    )
    args.update(overrides)
    return import_semantic(client, "sales", **args)


def test_import_binds_published_tables_and_generates_reviewable_project(published):
    client, output = published
    result = reverse(published)
    assert client.calls == [("published", "published_1"), "metadata", "metadata"]
    assert result["remoteMutations"] is False
    assert result["metrics"] == 3 and result["relationships"] == 1
    assert any("COUNT on empty inputs" in note for note in result["reviewNotes"])
    assert read_json(str(output / "published-package.json")) == client.snapshot
    assert not (output / "profiles.yml").exists()
    profile = yaml.safe_load((output / "profiles.yml.example").read_text())["imported_sales"]
    assert "env_var" in profile["outputs"]["dev"]["access_key_secret"]
    sources = yaml.safe_load((output / "models/sources.yml").read_text())["sources"]
    assert sources[0]["database"] == "p" and sources[0]["schema"] == "s"
    assert sources[0]["tables"] == [
        {"name": "orders", "identifier": "orders"},
        {"name": "customers", "identifier": "customers"},
    ]
    models = yaml.safe_load((output / "models/semantic.yml").read_text())["models"]
    orders, customers, spine = models
    assert orders["config"]["alias"] == "sales_dev_orders"
    assert orders["agg_time_dimension"] == "ordered_at"
    entities = {
        column["name"]: column["entity"] for column in orders["columns"] if "entity" in column
    }
    assert entities["customer_id"] == customers["columns"][0]["entity"] | {"type": "foreign"}
    assert orders["metrics"][0]["filter"] == "{{ Dimension('orders__status') }} = 'paid'"
    assert orders["metrics"][2]["type"] == "ratio"
    assert orders["metrics"][2]["denominator"] == "paid_orders"
    assert spine["time_spine"]["standard_granularity_column"] == "date_day"


def test_import_preserves_relationships_without_metrics_or_source_primary_key(published):
    client, output = published
    content = client.snapshot["content"]
    content.pop("metricDefinitions")
    semantic = content["semanticModel"]
    orders, customers = semantic["logicalTables"]
    orders.pop("primaryKey")
    dim = customers["dimensions"][0]
    dim.update(name="year", tags=["odps.binding:physical-column"])
    [link] = semantic["approvedRelationships"]
    link.update(
        leftTable="customers",
        rightTable="orders",
        joinType="RIGHT",
        relationshipCardinality="ONE_TO_MANY",
        joinCondition="customers.customer_id = orders.customer_id",
        relationshipId="catalog-id",
        sourceDetail={"kind": "catalog"},
    )
    with pytest.raises(ValidationError, match="relationship targets require"):
        reverse(published, unkeyed_models=["customers"])
    with pytest.raises(ValidationError, match=r"orders.primaryKey: column is missing"):
        reverse(published, primary_keys={"orders": None})
    assert not output.exists()

    result = reverse(published, unkeyed_models=["orders"])
    assert result["metrics"] == 0 and result["relationships"] == 1
    assert result["unkeyedModels"] == ["orders"]
    assert any("Converted 1 RIGHT ONE_TO_MANY" in note for note in result["reviewNotes"])
    orders, customers, _ = yaml.safe_load((output / "models/semantic.yml").read_text())["models"]
    assert orders["primary_entity"] == "orders"
    assert all("data_tests" not in col for col in orders["columns"])
    assert [col["entity"] for col in orders["columns"] if "entity" in col] == [
        {"name": "customers", "type": "foreign"}
    ]
    dimension = next(col["dimension"] for col in customers["columns"] if "dimension" in col)
    assert dimension["name"] == "dim_year"
    assert dimension["config"]["meta"]["semantic_package"] == {
        "source_name": "year",
        "tags": ["odps.binding:physical-column"],
    }


@pytest.mark.parametrize(
    ("pairs", "error"),
    [
        ([("races", "seasons"), ("races", "seasons")], "explicit role mappings"),
        ([("races", "seasons"), ("seasons", "races")], "cyclic or ambiguous path"),
        ([("races", "seasons"), ("results", "seasons")], "cyclic or ambiguous path"),
        ([("races", "seasons"), ("seasons", "series")], "exceeds two hops"),
    ],
)
def test_import_rejects_unrepresentable_join_paths(pairs, error):
    bindings = {name: ("p", "s", name) for name in ("results", "races", "seasons", "series")}
    links = [
        {
            "name": f"rel_{i}",
            "leftTable": source,
            "rightTable": target,
            "joinType": "LEFT",
            "relationshipCardinality": "MANY_TO_ONE",
            "relationshipColumns": [{"leftColumn": "key", "rightColumn": "id"}],
        }
        for i, (source, target) in enumerate([("results", "races"), *pairs])
    ]
    with pytest.raises(ValidationError, match=error):
        normalized_joins(links, bindings)


@pytest.mark.parametrize(
    ("path", "value", "error"),
    [
        (("source",), "USER_DRAFT", "requested Published"),
        (("revisionId",), "stale", "requested Published"),
        (
            ("content", "semanticModel", "approvedRelationships", 0, "joinType"),
            "INNER",
            "LEFT MANY_TO_ONE",
        ),
        (
            ("content", "semanticModel", "approvedRelationships", 0, "joinCondition"),
            "orders.customer_id = customers.customer_id AND customers.region = 'east'",
            "joinCondition",
        ),
        (
            ("content", "semanticModel", "namedFilters", 0, "sqlFragment"),
            "status = '{{ env_var('secret') }}'",
            "SQL without Jinja",
        ),
        (
            ("content", "metricDefinitions", 2, "derivedMetric", "formula"),
            "paid_revenue / paid_orders",
            "derivedMetric.formula",
        ),
        (("content", "metricDefinitions", 0, "window"), "7 days", "window: unsupported field"),
    ],
)
def test_import_rejects_lossy_or_executable_mappings(published, path, value, error):
    client, output = published
    target = client.snapshot
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError, match=error):
        reverse(published)
    assert not output.exists()


def test_import_requires_reviewed_time_mapping_for_free_text(published):
    client, output = published
    client.snapshot["content"]["instructions"]["timeConventions"] = ["Use UTC order days"]
    client.snapshot["content"]["semanticModel"]["namedFilters"][0][
        "sqlFragment"
    ] = "orders.status = 'paid'"
    with pytest.raises(ValidationError, match=r"time_mapping for orders.ordered_at"):
        reverse(published)
    assert not output.exists()
    result = reverse(
        published,
        time_mapping={
            "orders.ordered_at": {"timezone": "UTC", "granularity": "day", "default": True}
        },
        entity_names={"orders": "order", "customers": "customer"},
    )
    assert any("supplied time mapping" in note for note in result["reviewNotes"])
    model = yaml.safe_load((output / "models/semantic.yml").read_text())["models"][0]
    assert model["metrics"][0]["filter"] == "{{ Dimension('order__status') }} = 'paid'"


def test_import_preserves_existing_project_and_rejects_partitioned_sources(published):
    client, output = published
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep")
    with pytest.raises(ValidationError, match="new directory"):
        reverse(published)
    assert list(output.iterdir()) == [sentinel]
    assert client.calls == []
    client.partitions = [{"name": "ds", "type": "STRING"}]
    with pytest.raises(ValidationError, match="partitioned sources"):
        reverse(published, output=str(output.parent / "other"))
    assert not (output.parent / "other").exists()


def test_import_preserves_quoted_dimensions_and_reviewed_totals_only_models(
    published, monkeypatch
):
    client, output = published
    content = client.snapshot["content"]
    model = content["semanticModel"]
    orders = model["logicalTables"][0]
    orders.pop("primaryKey")
    orders.pop("timeDimensions")
    orders["synonyms"] = ["Order records"]
    orders["dimensions"][0].update(
        name="order__state",
        expr="`order status`",
        synonyms=["State"],
        isEnum=True,
        enumValues=[{"value": "paid"}],
    )
    orders["dimensions"].extend(
        [
            {"name": "order_id", "expr": "order_id", "dataType": "INTEGER"},
            {"name": "source_flag", "expr": "`from`", "dataType": "STRING"},
        ]
    )
    for item in model["namedFilters"]:
        item["sqlFragment"] = item["sqlFragment"].replace("status", "`order status`")
    content["instructions"]["timeConventions"] = []
    for metric in content["metricDefinitions"]:
        metric.pop("timeDimension", None)
        metric.pop("visibility", None)
    describe = client.describe

    def quoted_metadata(relation):
        result = describe(relation)
        if relation["alias"] == "orders":
            result["columns"].append({"name": "from", "type": "STRING"})
        for column in result["columns"]:
            if column["name"] == "status":
                column["name"] = "order status"
        return result

    monkeypatch.setattr(client, "describe", quoted_metadata)
    with pytest.raises(ValidationError, match=r"primary_keys.orders"):
        reverse(published)
    with pytest.raises(ValidationError, match=r"opt in to totals_only"):
        reverse(published, primary_keys={"orders": "order_id"})
    result = reverse(
        published,
        primary_keys={"orders": "order_id"},
        totals_only=["orders"],
    )
    assert result["totalsOnlyModels"] == ["orders"]
    docs = yaml.safe_load((output / "models/semantic.yml").read_text())
    orders_yml = docs["models"][0]
    assert orders_yml["semantic_model"]["config"]["meta"]["semantic_package"]["synonyms"] == [
        "Order records"
    ]
    state = next(
        c for c in orders_yml["columns"] if c.get("dimension", {}).get("name") == "order_state"
    )
    assert state["dimension"]["config"]["meta"]["semantic_package"] == {
        "synonyms": ["State"],
        "isEnum": True,
        "enumValues": [{"value": "paid"}],
        "source_name": "order__state",
        "source_column": "order status",
    }
    assert orders_yml["derived_semantics"]["dimensions"][0]["expr"] == "order_id"
    assert orders_yml["metrics"][0]["filter"] == "{{ Dimension('orders__order_state') }} = 'paid'"
    assert orders_yml["metrics"][0]["agg_time_dimension"] == "unresolved_import_time"
    assert (
        orders_yml["metrics"][0]["config"]["meta"]["semantic_package"]["visibility"]
        == "unspecified"
    )
    sql = (output / "models/orders.sql").read_text()
    assert f"`order status` as `{state['name']}`" in sql
    reserved = next(
        c for c in orders_yml["columns"] if c.get("dimension", {}).get("name") == "source_flag"
    )
    assert reserved["name"].startswith("sp_from_")
    assert f"`from` as `{reserved['name']}`" in sql
    assert "cast(null as date) as `unresolved_import_time`" in sql


def test_totals_only_cannot_replace_published_aggregation_time(published):
    with pytest.raises(ValidationError, match=r"totals_only.orders"):
        reverse(published, totals_only=["orders"])


def test_import_rejects_template_code_in_annotation_metadata(published):
    client, output = published
    client.snapshot["content"]["semanticModel"]["logicalTables"][0]["synonyms"] = [
        "{{ env_var('SECRET') }}"
    ]
    with pytest.raises(
        ValidationError, match="metadata must not contain executable template delimiters"
    ):
        reverse(published)
    assert not output.exists()


def test_export_rejects_totals_only_placeholder(case):
    _, manifest, output = case
    data = json.loads(manifest.read_text())
    dimension = next(d for d in data["semantic_models"][0]["dimensions"] if d["type"] == "time")
    dimension["config"] = {"meta": {"semantic_package": {"totals_only": True}}}
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValidationError, match="totals-only technical date"):
        run(case)
    assert not output.exists()
