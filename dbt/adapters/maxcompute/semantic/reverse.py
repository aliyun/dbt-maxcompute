"""Map the supported semantic package contract to dbt Core 1.12 model properties."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from .conversion import AGG_MAP, physical_type
from .files import ValidationError

LOGICAL_NAME = re.compile(r"(?!.*__)[a-z][a-z0-9_]{0,62}")
PHYSICAL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{0,127}")
TOKEN = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,254}")
CONVENTION = re.compile(
    r"([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*): source_timezone=([^;]+); "
    r"session_timezone=([^;]+); minimum_granularity=([A-Z]+); default_agg_time=(True|False)\."
)
AGGREGATIONS = {value: key for key, value in AGG_MAP.items()}
TEXT_SECTIONS = {"instructions", "glossary", "verifiedQueries"}
JINJA_STARTS = ("{{", "{%", "{#")

Binding = tuple[str, str, str]


def token(value: Any, path: str, pattern: re.Pattern = TOKEN) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValidationError(f"{path}: expected a supported identifier")
    return value


def record(value: Any, path: str, fields: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{path}: expected an object")
    if fields is not None:
        extra = value.keys() - set(fields.split())
        if extra:
            raise ValidationError(f"{path}.{sorted(extra)[0]}: unsupported field")
    return value


def items(value: Any, path: str) -> list:
    if not isinstance(value, list):
        raise ValidationError(f"{path}: expected a list")
    return value


def named(value: Any, path: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items(value, path):
        obj = record(item, path)
        name = token(obj.get("name"), f"{path}.name", LOGICAL_NAME)
        if name in result:
            raise ValidationError(f"{path}.{name}: duplicate name")
        result[name] = obj
    return result


def relation(value: Any, path: str) -> Binding:
    ref = record(value, path, "project schema table")
    return (
        token(ref.get("project"), f"{path}.project", PHYSICAL_NAME),
        token(ref.get("schema"), f"{path}.schema", PHYSICAL_NAME),
        token(ref.get("table"), f"{path}.table", PHYSICAL_NAME),
    )


def table_bindings(document: dict) -> dict[str, Binding]:
    content = record(document.get("content"), "content")
    extra = content.keys() - (
        {"dataReferences", "semanticModel", "metricDefinitions"} | TEXT_SECTIONS
    )
    if extra:
        raise ValidationError(f"content.{sorted(extra)[0]}: unsupported section")
    semantic = record(
        content.get("semanticModel"),
        "semanticModel",
        "logicalTables namedFilters approvedRelationships",
    )
    refs = [
        relation(ref, "dataReferences")
        for ref in items(content.get("dataReferences"), "dataReferences")
    ]
    if len({tuple(part.lower() for part in ref) for ref in refs}) != len(refs):
        raise ValidationError("dataReferences: duplicate physical references")
    obj = record(document.get("object", {}), "object")
    if "dataScope" in obj:
        scope = record(obj["dataScope"], "object.dataScope", "dataReferences")
        scoped = {
            relation(ref, "object.dataScope.dataReferences")
            for ref in items(scope.get("dataReferences"), "object.dataScope.dataReferences")
        }
        if scoped != set(refs):
            raise ValidationError(
                "object.dataScope: references do not match content.dataReferences"
            )
    tables = named(semantic.get("logicalTables"), "logicalTables")
    if not 1 <= len(tables) <= 20:
        raise ValidationError("logicalTables: expected 1 to 20 logical tables")
    qualified = {".".join(ref): ref for ref in refs}
    bindings = {}
    for name, table in tables.items():
        base = table.get("baseTable")
        if not isinstance(base, str) or base not in qualified:
            raise ValidationError(
                f"logicalTables.{name}.baseTable: expected a declared project.schema.table"
            )
        bindings[name] = qualified[base]
    if set(bindings.values()) != set(refs) or len(bindings) != len(refs):
        raise ValidationError(
            "dataReferences: each referenced relation must have one logical table"
        )
    return bindings


def catalog_columns(actual: dict, binding: Binding, path: str) -> dict[str, str]:
    if (
        (actual.get("project"), actual.get("schema_name"), actual.get("table_name")) != binding
        or actual.get("has_more_columns") is not False
        or not isinstance(actual.get("partition_columns"), list)
    ):
        raise ValidationError(f"{path}: incomplete or mismatched live table metadata")
    if actual["partition_columns"]:
        raise ValidationError(f"{path}: partitioned sources require a partition-policy mapping")
    columns = {}
    seen = set()
    for entry in items(actual.get("columns"), f"{path}.columns"):
        column = record(entry, f"{path}.columns")
        name, kind = column.get("name"), column.get("type")
        if not isinstance(name, str) or name.lower() in seen or not isinstance(kind, str):
            raise ValidationError(f"{path}.columns: incomplete or ambiguous column metadata")
        seen.add(name.lower())
        columns[name] = kind
    return columns


def scalar(value: Any, path: str) -> exp.Expression:
    if not isinstance(value, str) or not value.strip() or any(s in value for s in JINJA_STARTS):
        raise ValidationError(f"{path}: expected SQL without Jinja")
    try:
        parsed = sqlglot.parse(value, read="hive")
    except (sqlglot.errors.SqlglotError, RecursionError) as exc:
        raise ValidationError(f"{path}: cannot parse SQL") from exc
    if len(parsed) != 1 or parsed[0] is None or any(node.comments for node in parsed[0].walk()):
        raise ValidationError(f"{path}: expected one expression without SQL comments")
    if any(marker in parsed[0].sql(dialect="hive") for marker in JINJA_STARTS):
        raise ValidationError(f"{path}: expected SQL without Jinja")
    return parsed[0]


def description(value: Any, fallback: str) -> str:
    # Keep template-shaped text in the source snapshot instead of rendering it through dbt.
    if isinstance(value, str) and value.strip() and not any(s in value for s in JINJA_STARTS):
        return value
    return fallback


@dataclass
class Table:
    name: str
    entity: str
    key: str
    physical: dict[str, str]
    model: dict[str, Any]
    columns: dict[str, dict[str, Any]] = field(default_factory=dict)
    categorical: dict[str, str] = field(default_factory=dict)
    times: set[str] = field(default_factory=set)

    def column(self, value: Any, path: str) -> str:
        name = token(value, path, PHYSICAL_NAME)
        if name not in self.physical:
            raise ValidationError(f"{path}: column is missing from live metadata")
        return name

    def annotate(self, column: str, path: str, **properties: Any) -> None:
        if column in self.columns:
            raise ValidationError(f"{path}: multiple semantic roles for one physical column")
        self.columns[column] = {"name": column, **properties}

    def type_of(self, column: str, path: str) -> str:
        try:
            return physical_type(self.physical[column])
        except ValueError as exc:
            raise ValidationError(f"{path}: unsupported physical column type") from exc


def time_mappings(instructions: dict, overrides: dict) -> tuple[dict[str, dict], list[str]]:
    parsed: dict[str, dict] = {}
    notes = []
    for line in items(instructions.get("timeConventions", []), "instructions.timeConventions"):
        match = CONVENTION.fullmatch(line) if isinstance(line, str) else None
        if not match:
            notes.append("Review the free-text time conventions in published-package.json.")
            continue
        key, source, session, grain, default = match.groups()
        if key in parsed:
            raise ValidationError(f"instructions.timeConventions.{key}: duplicate convention")
        parsed[key] = {
            "timezone": source if source == session else None,
            "granularity": grain.lower(),
            "default": default == "True",
        }
    for key, value in overrides.items():
        mapping = record(value, f"time_mapping.{key}", "timezone granularity default")
        if key in parsed and parsed[key] != mapping:
            raise ValidationError(f"time_mapping.{key}: conflicts with the published convention")
        parsed[key] = mapping
        notes.append(
            f"Confirm the supplied time mapping for {key} against the original business definition."
        )
    return parsed, notes


def add_dimensions(table: Table, obj: dict, path: str, mappings: dict[str, dict]) -> None:
    names: set[str] = set()
    for section in ("dimensions", "timeDimensions"):
        for name, dim in named(obj.get(section, []), f"{path}.{section}").items():
            location = f"{path}.{section}.{name}"
            record(dim, location, "name expr dataType description usageGuidance")
            if name in names:
                raise ValidationError(f"{location}: duplicate dimension name")
            names.add(name)
            column = table.column(dim.get("expr"), f"{location}.expr")
            kind = table.type_of(column, location)
            if dim.get("dataType") != kind:
                raise ValidationError(f"{location}.dataType: does not match live column type")
            dimension = {
                "type": "time" if section == "timeDimensions" else "categorical",
                "name": name,
            }
            properties: dict[str, Any] = {"dimension": dimension}
            if "description" in dim:
                properties["description"] = description(dim["description"], name)
            if section == "timeDimensions":
                mapping = mappings.pop(f"{table.name}.{name}", None)
                if mapping is None:
                    raise ValidationError(
                        f"{location}: supply a reviewed time_mapping for {table.name}.{name}"
                    )
                if (
                    kind not in ("DATE", "DATETIME", "TIMESTAMP")
                    or mapping.get("timezone") != "UTC"
                    or mapping.get("granularity") != "day"
                    or not isinstance(mapping.get("default"), bool)
                ):
                    raise ValidationError(
                        f"{location}: expected a DATE/TIMESTAMP-like column and a UTC day mapping"
                    )
                properties["granularity"] = "day"
                table.times.add(name)
                if mapping["default"]:
                    if "agg_time_dimension" in table.model:
                        raise ValidationError(f"{location}: multiple default aggregation times")
                    table.model["agg_time_dimension"] = name
            else:
                table.categorical[column] = name
            table.annotate(column, location, **properties)


def add_relationships(tables: dict[str, Table], value: Any) -> int:
    links = named(value, "approvedRelationships")
    sources: set[str] = set()
    targets: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for name, obj in links.items():
        path = f"approvedRelationships.{name}"
        record(
            obj,
            path,
            "name leftTable rightTable relationshipCardinality joinType relationshipColumns "
            "joinCondition source description usageGuidance",
        )
        left_name = token(obj.get("leftTable"), f"{path}.leftTable", LOGICAL_NAME)
        right_name = token(obj.get("rightTable"), f"{path}.rightTable", LOGICAL_NAME)
        left, right = tables.get(left_name), tables.get(right_name)
        if (
            left is None
            or right is None
            or left is right
            or (left_name, right_name) in pairs
            or obj.get("relationshipCardinality") != "MANY_TO_ONE"
            or obj.get("joinType") != "LEFT"
        ):
            raise ValidationError(
                f"{path}: expected one LEFT MANY_TO_ONE edge between distinct tables"
            )
        keys = items(obj.get("relationshipColumns"), f"{path}.relationshipColumns")
        if len(keys) != 1:
            raise ValidationError(f"{path}.relationshipColumns: expected one join key pair")
        key = record(keys[0], f"{path}.relationshipColumns", "leftColumn rightColumn")
        lhs = left.column(key.get("leftColumn"), f"{path}.leftColumn")
        rhs = right.column(key.get("rightColumn"), f"{path}.rightColumn")
        if rhs != right.key or left.physical[lhs].upper() != right.physical[rhs].upper():
            raise ValidationError(
                f"{path}: join keys must have the same live type and target the primary key"
            )
        if "joinCondition" in obj:
            condition = scalar(obj["joinCondition"], f"{path}.joinCondition")
            expected = exp.EQ(
                this=exp.column(lhs, table=left_name), expression=exp.column(rhs, table=right_name)
            )
            reversed_eq = exp.EQ(this=expected.expression, expression=expected.this)
            if condition not in (expected, reversed_eq):
                raise ValidationError(
                    f"{path}.joinCondition: expected exactly the declared key equality"
                )
        left.annotate(lhs, path, entity={"type": "foreign", "name": right.entity})
        pairs.add((left_name, right_name))
        sources.add(left_name)
        targets.add(right_name)
    if sources & targets:
        raise ValidationError("approvedRelationships: multi-hop paths and cycles are unsupported")
    return len(links)


PREDICATES = (
    exp.And,
    exp.Or,
    exp.Not,
    exp.Paren,
    exp.EQ,
    exp.NEQ,
    exp.LT,
    exp.LTE,
    exp.GT,
    exp.GTE,
    exp.In,
    exp.Between,
    exp.Is,
)


def render_filter(table: Table, obj: dict, path: str) -> str:
    record(obj, path, "name sqlFragment applicableTables description usageGuidance")
    if obj.get("applicableTables") != [table.name]:
        raise ValidationError(f"{path}.applicableTables: expected the metric's base table only")
    tree = scalar(obj.get("sqlFragment"), f"{path}.sqlFragment")
    if not isinstance(tree, PREDICATES) or any(
        not isinstance(
            node, PREDICATES + (exp.Column, exp.Identifier, exp.Literal, exp.Null, exp.Boolean)
        )
        for node in tree.walk()
    ):
        raise ValidationError(f"{path}.sqlFragment: unsupported filter predicate")

    def dimension(node: exp.Expression) -> exp.Expression:
        if not isinstance(node, exp.Column):
            return node
        if (
            node.table not in ("", table.name)
            or node.db
            or node.catalog
            or node.name not in table.categorical
        ):
            raise ValidationError(
                f"{path}.sqlFragment: filter must use local categorical dimensions"
            )
        return exp.Var(
            this="{{ Dimension('" + table.entity + "__" + table.categorical[node.name] + "') }}"
        )

    return tree.transform(dimension).sql(dialect="hive")


def add_measure(
    name: str,
    obj: dict,
    tables: dict[str, Table],
    filters: dict[str, dict],
    used_filters: set[str],
) -> tuple[str, str]:
    path = f"metricDefinitions.{name}"
    record(
        obj,
        path,
        "name description visibility semanticType baseLogicalTable expr dataType "
        "defaultAggregation timeDimension additivityType filterRefs caveats",
    )
    table = tables.get(
        token(obj.get("baseLogicalTable"), f"{path}.baseLogicalTable", LOGICAL_NAME)
    )
    agg = token(obj.get("defaultAggregation"), f"{path}.defaultAggregation")
    if table is None or agg not in AGGREGATIONS:
        raise ValidationError(f"{path}: unsupported base table or aggregation")
    time = token(obj.get("timeDimension"), f"{path}.timeDimension", LOGICAL_NAME)
    if time not in table.times or "agg_time_dimension" not in table.model:
        raise ValidationError(
            f"{path}.timeDimension: requires a declared time dimension and model default"
        )
    tree = scalar(obj.get("expr"), f"{path}.expr")
    if not isinstance(tree, (exp.Column, exp.Coalesce)) or any(
        not isinstance(node, (exp.Column, exp.Coalesce, exp.Identifier, exp.Literal, exp.Null))
        for node in tree.walk()
    ):
        raise ValidationError(f"{path}.expr: expected a direct column or COALESCE expression")
    types = set()
    for ref in tree.find_all(exp.Column):
        if ref.table or ref.db or ref.catalog:
            raise ValidationError(f"{path}.expr: expected unqualified columns")
        column = table.column(ref.name, f"{path}.expr")
        types.add(table.type_of(column, f"{path}.expr"))
        table.columns.setdefault(column, {"name": column})
    if len(types) != 1:
        raise ValidationError(f"{path}.expr: expected columns of a single physical type")
    result_type = "BIGINT" if agg in ("COUNT", "COUNT_DISTINCT") else next(iter(types))
    if obj.get("dataType") != result_type:
        raise ValidationError(f"{path}.dataType: does not match expression and aggregation")
    additivity = "ADDITIVE" if agg in ("SUM", "COUNT") else "NON_ADDITIVE"
    if obj.get("additivityType", additivity) != additivity:
        raise ValidationError(f"{path}.additivityType: conflicts with the supported aggregation")
    metric = {
        "name": name,
        "label": name,
        "description": description(obj.get("description"), name),
        "type": "simple",
        "agg": AGGREGATIONS[agg],
        "expr": tree.sql(dialect="hive"),
        "agg_time_dimension": time,
    }
    predicates = []
    for value in items(obj.get("filterRefs", []), f"{path}.filterRefs"):
        ref_name = token(value, f"{path}.filterRefs", LOGICAL_NAME)
        if ref_name not in filters:
            raise ValidationError(f"{path}.filterRefs: filter {ref_name} is missing")
        predicates.append(render_filter(table, filters[ref_name], f"namedFilters.{ref_name}"))
        used_filters.add(ref_name)
    if predicates:
        metric["filter"] = (
            predicates[0] if len(predicates) == 1 else " AND ".join(f"({p})" for p in predicates)
        )
    table.model.setdefault("metrics", []).append(metric)
    return table.name, time


def add_ratio(
    name: str, obj: dict, tables: dict[str, Table], measures: dict[str, tuple[str, str]]
) -> None:
    path = f"metricDefinitions.{name}"
    record(
        obj, path, "name description visibility semanticType derivedMetric additivityType caveats"
    )
    derived = record(
        obj.get("derivedMetric"), f"{path}.derivedMetric", "formula refMetrics resultDataType"
    )
    refs = [
        token(ref, f"{path}.derivedMetric.refMetrics", LOGICAL_NAME)
        for ref in items(derived.get("refMetrics"), f"{path}.derivedMetric.refMetrics")
    ]
    formula = scalar(derived.get("formula"), f"{path}.derivedMetric.formula")
    if (
        len(refs) != 2
        or any(ref not in measures for ref in refs)
        or measures[refs[0]] != measures[refs[1]]
        or derived.get("resultDataType") != "DOUBLE"
        or obj.get("additivityType", "NON_ADDITIVE") != "NON_ADDITIVE"
    ):
        raise ValidationError(
            f"{path}: expected a DOUBLE ratio of two simple metrics at the same grain"
        )
    numerator, denominator = refs
    for first, second in ((numerator, denominator), (denominator, numerator)):
        expected = exp.Div(
            this=exp.column(first),
            expression=exp.Nullif(this=exp.column(second), expression=exp.Literal.number(0)),
            typed=False,
            safe=False,
        )
        if formula.sql(dialect="hive") == expected.sql(dialect="hive"):
            numerator, denominator = first, second
            break
    else:
        raise ValidationError(
            f"{path}.derivedMetric.formula: expected numerator / NULLIF(denominator, 0)"
        )
    tables[measures[numerator][0]].model["metrics"].append(
        {
            "name": name,
            "label": name,
            "description": description(obj.get("description"), name),
            "type": "ratio",
            "numerator": numerator,
            "denominator": denominator,
        }
    )


def convert(
    document: dict,
    bindings: dict[str, Binding],
    metadata: dict[str, dict],
    *,
    entity_names: dict,
    time_mapping: dict,
) -> tuple[dict[str, Table], list[str], int, int]:
    content = document["content"]
    semantic = content["semanticModel"]
    instructions = record(content.get("instructions", {}), "instructions")
    mappings, notes = time_mappings(instructions, time_mapping)
    if entity_names.keys() - bindings.keys():
        raise ValidationError("entity_names: contains a name outside logicalTables")
    entities = {
        name: token(entity_names.get(name, name), f"entity_names.{name}", LOGICAL_NAME)
        for name in bindings
    }
    if len(set(entities.values())) != len(entities):
        raise ValidationError("entity_names: primary entity names must be unique")
    tables: dict[str, Table] = {}
    for table_doc in semantic["logicalTables"]:
        name = table_doc["name"]
        path = f"logicalTables.{name}"
        obj = record(
            table_doc,
            path,
            "name baseTable description primaryKey dimensions timeDimensions usageGuidance",
        )
        pk = record(obj.get("primaryKey"), f"{path}.primaryKey", "columns")
        keys = items(pk.get("columns"), f"{path}.primaryKey.columns")
        if len(keys) != 1:
            raise ValidationError(f"{path}.primaryKey: expected one declared key column")
        key = token(keys[0], f"{path}.primaryKey.columns", PHYSICAL_NAME)
        table = Table(
            name,
            entities[name],
            key,
            catalog_columns(metadata[name], bindings[name], path),
            {
                "name": name,
                "description": description(obj.get("description"), name),
                "semantic_model": {"enabled": True},
            },
        )
        table.column(key, f"{path}.primaryKey")
        table.annotate(
            key,
            path,
            entity={"type": "primary", "name": table.entity},
            data_tests=["unique", "not_null"],
        )
        add_dimensions(table, obj, path, mappings)
        tables[name] = table
    if mappings:
        raise ValidationError(
            "time_mapping/timeConventions: references an undeclared time dimension"
        )
    relationships = add_relationships(tables, semantic.get("approvedRelationships", []))
    filters = named(semantic.get("namedFilters", []), "namedFilters")
    metrics = named(content.get("metricDefinitions"), "metricDefinitions")
    if not metrics:
        raise ValidationError("metricDefinitions: at least one metric is required")
    used_filters: set[str] = set()
    measures = {}
    for name, obj in metrics.items():
        if obj.get("visibility") != "PUBLIC":
            raise ValidationError(
                f"metricDefinitions.{name}.visibility: only explicit PUBLIC metrics are supported"
            )
        if obj.get("semanticType") == "MEASURE":
            measures[name] = add_measure(name, obj, tables, filters, used_filters)
        elif obj.get("semanticType") != "CALCULATED_METRIC":
            raise ValidationError(
                f"metricDefinitions.{name}.semanticType: unsupported metric kind"
            )
    for name, obj in metrics.items():
        if obj["semanticType"] == "CALCULATED_METRIC":
            add_ratio(name, obj, tables, measures)
    if any(metric.get("defaultAggregation") == "COUNT" for metric in metrics.values()):
        notes.append(
            "Check COUNT on empty inputs. The tested MetricFlow build returns NULL where SQL COUNT returns 0."
        )
    if filters.keys() - used_filters:
        notes.append("Named filters unused by metrics remain in published-package.json.")
    if TEXT_SECTIONS & content.keys():
        notes.append(
            "Review narrative business rules, guidance and examples in published-package.json before use."
        )
    for table in tables.values():
        table.model["columns"] = list(table.columns.values())
    return tables, sorted(set(notes)), len(metrics), relationships
