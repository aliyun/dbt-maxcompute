"""Validate the parsed manifest with the installed MetricFlow semantic rules."""

from pathlib import Path

from metricflow_semantic_interfaces.implementations.semantic_manifest import (
    PydanticSemanticManifest,
)
from metricflow_semantic_interfaces.validations.semantic_manifest_validator import (
    SemanticManifestValidator,
)


def main():
    path = Path(__file__).resolve().parents[1] / "target" / "semantic_manifest.json"
    manifest = PydanticSemanticManifest.parse_raw(path.read_text())
    result = SemanticManifestValidator().validate_semantic_manifest(manifest)
    print(result.json(indent=2))
    if result.has_blocking_issues:
        raise SystemExit(1)
    print("PASS semantic manifest validation")


if __name__ == "__main__":
    main()
