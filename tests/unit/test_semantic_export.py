import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbt.adapters.maxcompute.semantic.export import MaxcClient, export_semantic
from dbt.adapters.maxcompute.semantic.files import ValidationError, read_json

FIXTURE = Path(__file__).parent / "fixtures/dbt_semantic_latest.json"


class Client:
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

    def read(self, name):
        self.calls.append("read")
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
