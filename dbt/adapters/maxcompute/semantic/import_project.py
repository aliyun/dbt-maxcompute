"""Read a pinned Published revision and write a reviewable dbt project."""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from dbt.adapters.maxcompute.utils import quote_ref

from .files import MAX_FILE_BYTES, ValidationError, digest, safe_path
from .reverse import (
    LOGICAL_NAME,
    PHYSICAL_NAME,
    UNRESOLVED_TIME,
    Table,
    convert,
    items,
    record,
    table_bindings,
    token,
)

TIME_SPINE = "import_time_spine"


def date_bounds(start: str, end: str) -> tuple[str, str]:
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if (first.isoformat(), last.isoformat()) != (start, end) or not 1 <= (
            last - first
        ).days <= 36600:
            raise ValueError("invalid date range")
    except (ValueError, TypeError) as exc:
        raise ValidationError(
            "time_spine_start/end: use ISO YYYY-MM-DD dates with an exclusive end, 1 to 36600 days apart"
        ) from exc
    return start, end


def write_project(root: Path, files: dict[str, str]) -> None:
    try:
        root.mkdir(mode=0o700)
    except OSError as exc:
        raise ValidationError(
            "Output must be a new directory under an existing directory."
        ) from exc
    try:
        # Write the dbt project marker last, so it is only usable after the other files exist.
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
    except (OSError, UnicodeError) as exc:
        shutil.rmtree(root)
        raise ValidationError("Could not write the generated project.") from exc


def yaml_text(obj: dict) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True)


def profile(project_name: str) -> str:
    return yaml_text(
        {
            project_name: {
                "target": "dev",
                "outputs": {
                    "dev": {
                        "type": "maxcompute",
                        "threads": 1,
                        "project": "{{ env_var('MAXCOMPUTE_PROJECT') }}",
                        "schema": "{{ env_var('MAXCOMPUTE_SCHEMA') }}",
                        "endpoint": "{{ env_var('MAXCOMPUTE_ENDPOINT') }}",
                        "auth_type": "access_key",
                        "access_key_id": "{{ env_var('ALIBABA_CLOUD_ACCESS_KEY_ID') }}",
                        "access_key_secret": "{{ env_var('ALIBABA_CLOUD_ACCESS_KEY_SECRET') }}",
                        "timezone": "UTC",
                    }
                },
            }
        }
    )


def model_files(
    tables: dict[str, Table], bindings: dict, aliases: dict[str, str], start: str, end: str
) -> dict[str, str]:
    files = {}
    sources: dict[tuple[str, str], dict[str, Any]] = {}
    models = []
    for name, table in tables.items():
        project, schema, identifier = bindings[name]
        scope = (project, schema)
        if scope not in sources:
            sources[scope] = {
                "name": f"semantic_source_{len(sources) + 1}",
                "database": project,
                "schema": schema,
                "quoting": {"database": True, "schema": True, "identifier": True},
                "tables": [],
            }
        source = sources[scope]
        source["tables"].append({"name": name, "identifier": identifier})
        projections = [
            quote_ref(column) if column == alias else f"{quote_ref(column)} as {quote_ref(alias)}"
            for column, alias in table.projection.items()
        ]
        if table.totals_only:
            projections.append(f"cast(null as date) as {quote_ref(UNRESOLVED_TIME)}")
        projection = ",\n       ".join(projections)
        files[f"models/{name}.sql"] = (
            f"select {projection}\nfrom {{{{ source('{source['name']}', '{name}') }}}}\n"
        )
        models.append({**table.model, "config": {"alias": aliases[name]}})
    files[f"models/{TIME_SPINE}.sql"] = (
        f"""{{{{ dbt.date_spine('day', "date '{start}'", "date '{end}'") }}}}\n"""
    )
    models.append(
        {
            "name": TIME_SPINE,
            "config": {"alias": aliases[TIME_SPINE]},
            "time_spine": {"standard_granularity_column": "date_day"},
            "columns": [
                {"name": "date_day", "granularity": "day", "data_tests": ["unique", "not_null"]}
            ],
        }
    )
    files["models/sources.yml"] = yaml_text({"version": 2, "sources": list(sources.values())})
    files["models/semantic.yml"] = yaml_text({"version": 2, "models": models})
    return files


def report(
    identity: dict,
    revision: str,
    snapshot_hash: str,
    bindings: dict,
    tables: dict[str, Table],
    aliases: dict[str, str],
    start: str,
    end: str,
    notes: list[str],
) -> str:
    lines = [
        "# Semantic package conversion",
        "",
        f"Published package {identity['specName']} at revision {revision}.",
        f"Canonical JSON SHA-256 of the source snapshot is {snapshot_hash}.",
        "",
        "| Logical model | Existing source | dbt view alias | Primary entity |",
        "| --- | --- | --- | --- |",
    ]
    for name, table in tables.items():
        lines.append(f"| {name} | {'.'.join(bindings[name])} | {aliases[name]} | {table.entity} |")
    lines.extend(
        [
            "",
            f"The daily calendar uses the half-open range {start} through {end}.",
            "It supports dbt validation and is separate from the source package's time definitions.",
            "The SQL models select the referenced columns from existing tables. Upstream transformations require their original dbt project.",
            "",
            "## Review and validate",
            "",
            "The importer checked the live table definitions. Run data tests and compare metric results before adopting the project.",
            "Confirm that the target schema and generated aliases are reserved for this project before building its views.",
            "The profile template requires your MaxCompute data endpoint, target project/schema and credentials via environment variables.",
            "Primary entity names use the logical table names unless you supplied entity_names.",
            "Use a MetricFlow build that supports MaxCompute to verify LEFT behavior and metric results.",
            "",
            *[f"- {note}" for note in notes],
            "",
            "```bash",
            "cp profiles.yml.example profiles.yml",
            "dbt parse --profiles-dir .",
            "dbt build --profiles-dir .",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def import_semantic(
    client,
    name: str,
    *,
    revision: str,
    output: str,
    project_name: str,
    table_prefix: str,
    time_spine_start: str,
    time_spine_end: str,
    entity_names: dict | None = None,
    time_mapping: dict | None = None,
    primary_keys: dict | None = None,
    totals_only: list | None = None,
) -> dict:
    token(name, "name")
    token(revision, "revision")
    token(project_name, "project_name", LOGICAL_NAME)
    token(table_prefix, "table_prefix", LOGICAL_NAME)
    start, end = date_bounds(time_spine_start, time_spine_end)
    root = safe_path(output)
    if root.exists() or not root.parent.is_dir():
        raise ValidationError("Output must be a new directory under an existing directory.")
    document = record(client.read(name, revision=revision), "Published snapshot")
    identity = record(document.get("identity"), "identity")
    if (
        document.get("format") != "maxc.semantic/v1"
        or document.get("source") != "PUBLISHED"
        or document.get("revisionId") != revision
        or identity.get("specName") != name
        or (client.namespace is not None and identity.get("namespace") != client.namespace)
    ):
        raise ValidationError(
            "Expected the requested Published maxc.semantic/v1 identity and revision."
        )
    token(identity.get("specId"), "identity.specId")
    token(identity.get("namespace"), "identity.namespace")
    try:
        raw = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        size = len(raw.encode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise ValidationError(
            "Published snapshot cannot be represented as portable JSON."
        ) from exc
    if size > MAX_FILE_BYTES:
        raise ValidationError("Published snapshot exceeds the 16 MiB portable file limit.")
    bindings = table_bindings(document)
    if TIME_SPINE in bindings:
        raise ValidationError(f"logicalTables.{TIME_SPINE}: reserved for the generated time spine")
    aliases = {
        model: token(f"{table_prefix}_{model}", f"alias.{model}", PHYSICAL_NAME)
        for model in (*bindings, TIME_SPINE)
    }
    if {ref[2].lower() for ref in bindings.values()} & set(aliases.values()):
        raise ValidationError("table_prefix: a generated view would share a source table name")
    metadata = {}
    for model, (project, schema, identifier) in bindings.items():
        try:
            metadata[model] = client.describe(
                {"database": project, "schema_name": schema, "alias": identifier}
            )
        except ValidationError as exc:
            raise ValidationError(
                f"logicalTables.{model}: cannot read live metadata for {project}.{schema}.{identifier}: {exc}"
            ) from exc
    tables, notes, metrics, relationships = convert(
        document,
        bindings,
        metadata,
        entity_names=record({} if entity_names is None else entity_names, "entity_names"),
        time_mapping=record({} if time_mapping is None else time_mapping, "time_mapping"),
        primary_keys=record({} if primary_keys is None else primary_keys, "primary_keys"),
        totals_only=items([] if totals_only is None else totals_only, "totals_only"),
    )
    snapshot_hash = digest(document)
    files = model_files(tables, bindings, aliases, start, end)
    files.update(
        {
            "profiles.yml.example": profile(project_name),
            ".gitignore": "profiles.yml\npublished-package.json\ntarget/\nlogs/\n.user.yml\n",
            "published-package.json": raw,
            "conversion-report.md": report(
                identity, revision, snapshot_hash, bindings, tables, aliases, start, end, notes
            ),
            "dbt_project.yml": yaml_text(
                {
                    "name": project_name,
                    "version": "1.0",
                    "config-version": 2,
                    "profile": project_name,
                    "require-dbt-version": [">=1.12.0", "<2.0.0"],
                    "model-paths": ["models"],
                    "models": {project_name: {"+materialized": "view"}},
                }
            ),
        }
    )
    write_project(root, files)
    return {
        "path": str(root),
        "identity": identity,
        "revisionId": revision,
        "publishedSnapshotSha256": snapshot_hash,
        "logicalTables": len(tables),
        "metrics": metrics,
        "relationships": relationships,
        "entityNames": {model: table.entity for model, table in tables.items()},
        "totalsOnlyModels": [name for name, table in tables.items() if table.totals_only],
        "reviewNotes": notes,
        "remoteMutations": False,
    }
