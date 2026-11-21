"""dbt source ownership; generic remote operations use the maxc JSON CLI contract."""

import copy
import json
import subprocess

from .files import ValidationError, digest, read_json, safe_path, write_json

WARNINGS = [
    "Restricted exporter: Core 1.12 latest spec, simple/ratio, UTC, single-hop LEFT relationships.",
    "Timezone/minimum grain are descriptive instructions. Windows, SCD, time-spine binding, partition policy and Decimal precision/scale are not mapped.",
    "Primary-key uniqueness and relationship cardinality require independent dbt/data tests.",
    "Review with maxc semantic diff, apply the exact planDigest, validate queries, then publish separately.",
]


class MaxcClient:
    """Only public CLI JSON, no imports from maxc's Python implementation."""

    def __init__(self, executable="maxc", config=None, user_agent=None, namespace=None):
        self.prefix = [executable]
        if config:
            self.prefix += ["--config", str(config)]
        self.suffix = ["--json"]
        if user_agent:
            self.suffix += ["--user-agent", user_agent]
        self.namespace = namespace

    def call(self, args):
        try:
            result = subprocess.run(
                [*self.prefix, *args, *self.suffix],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValidationError(
                "Cannot run maxc; install a compatible CLI and check connectivity."
            ) from exc
        try:
            envelope = json.loads(result.stdout)
            if result.returncode or envelope["status"] != "success":
                raise ValueError("failed")
            return envelope["data"]
        except (ValueError, KeyError, TypeError) as exc:
            # Do not echo arbitrary stderr/transport responses, which may contain credentials.
            raise ValidationError(
                "maxc did not return successful JSON; check CLI version, authentication and target access."
            ) from exc

    def read(self, name):
        args = ["semantic", "get", name]
        if self.namespace:
            args += ["--namespace", self.namespace]
        return self.call(args)

    def describe(self, relation):
        data = self.call(
            [
                "meta",
                "describe",
                relation["alias"],
                "--project",
                relation["database"],
                "--schema",
                relation["schema_name"],
                "--metadata-only",
            ]
        )
        table = data.get("table") if isinstance(data, dict) else None
        if not isinstance(table, dict):
            raise ValidationError("maxc meta describe must return data.table metadata.")
        return table


def export_semantic(
    client, name, *, manifest, output, timezone, join_type, overwrite=False, replace_sections=False
):
    path = safe_path(output)
    if path.exists() and not overwrite:
        raise ValidationError("Output already exists; choose another path or use --overwrite.")
    if not path.parent.is_dir():
        raise ValidationError("Output parent must be an existing directory.")
    if safe_path(manifest) == path:
        raise ValidationError("Output must not overwrite the source manifest.")
    try:
        from .conversion import convert
    except ImportError as exc:
        raise ValidationError(
            'Install converter dependencies with pip install "dbt-maxcompute[semantic]".'
        ) from exc
    source = read_json(manifest)
    models = source.get("semantic_models")
    if not isinstance(models, list) or not 1 <= len(models) <= 20:
        raise ValidationError("Export requires 1 to 20 compiled semantic_models.")
    names = [m.get("name") for m in models if isinstance(m, dict)]
    if (
        len(names) != len(models)
        or not all(isinstance(n, str) and n for n in names)
        or len(set(names)) != len(names)
    ):
        raise ValidationError("Semantic model names must be unique nonempty strings.")
    snapshot = client.read(name)
    if (
        snapshot.get("format") != "maxc.semantic/v1"
        or snapshot.get("source") != "USER_DRAFT"
        or snapshot.get("identity", {}).get("specName") != name
    ):
        raise ValidationError(
            "Expected a revision-bound USER_DRAFT maxc.semantic/v1 snapshot for this package."
        )
    metadata = {}
    for model in models:
        relation = model.get("node_relation") or {}
        if not all(
            isinstance(relation.get(k), str) and relation[k]
            for k in ("database", "schema_name", "alias")
        ):
            raise ValidationError("A complete project/schema/table relation is required.")
        actual = client.describe(relation)
        if (
            actual.get("project") != relation["database"]
            or actual.get("schema_name") != relation["schema_name"]
            or actual.get("table_name") != relation["alias"]
            or actual.get("has_more_columns") is not False
            or not isinstance(actual.get("partition_columns"), list)
        ):
            raise ValidationError("Incomplete or mismatched live table metadata.")
        if actual["partition_columns"]:
            raise ValidationError(
                "Partitioned sources require an explicit partition-policy mapping; unsupported by this exporter."
            )
        metadata[model["name"]] = actual
    try:
        content = convert(source, metadata, timezone=timezone, join_type=join_type)
    except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
        raise ValidationError(
            "Unsupported or unresolved dbt semantics: "
            + (
                str(exc)
                if isinstance(exc, ValueError)
                else "check the Core 1.12 latest-spec manifest"
            )
        ) from exc
    definition_hash = digest(
        {k: source.get(k) for k in ("semantic_models", "metrics", "project_configuration")}
    )
    content["instructions"]["generalGuidance"] += " dbt_definition_sha256=" + definition_hash
    changed = [
        k for k, v in content.items() if k in snapshot["content"] and snapshot["content"][k] != v
    ]
    if changed and not replace_sections:
        raise ValidationError(
            "Export would replace existing sections: "
            + ", ".join(changed)
            + "; review before --replace-sections."
        )
    document = copy.deepcopy(snapshot)
    document["content"].update(content)
    document["object"]["dataScope"] = {"dataReferences": content["dataReferences"]}
    receipt = write_json(output, document, overwrite=overwrite)
    return {
        **receipt,
        "identity": document["identity"],
        "revisionId": document["revisionId"],
        "dbtDefinitionSha256": definition_hash,
        "sourceManifestSha256": digest(source),
        "logicalTables": len(models),
        "metrics": len(content["metricDefinitions"]),
        "relationships": len(content["semanticModel"]["approvedRelationships"]),
        "replacedSections": changed,
        "remoteMutations": False,
        "warnings": WARNINGS,
    }
