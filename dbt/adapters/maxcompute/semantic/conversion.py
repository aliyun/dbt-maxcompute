"""Restricted dbt Core 1.12 semantic manifest conversion; no cloud writes."""

from __future__ import annotations

import logging
from typing import Any

import sqlglot
from jinja2 import StrictUndefined, nodes
from jinja2.sandbox import SandboxedEnvironment
from sqlglot import exp

logger = logging.getLogger(__name__)
TYPE_MAP = {
    "INT": "INTEGER",
    "BIGINT": "BIGINT",
    "STRING": "STRING",
    "BOOLEAN": "BOOL",
    "DOUBLE": "DOUBLE",
    "FLOAT": "DOUBLE",
    "DATE": "DATE",
    "DATETIME": "DATETIME",
    "TIMESTAMP": "TIMESTAMP",
    "DECIMAL": "DECIMAL",
}
AGG_MAP = {
    "sum": "SUM",
    "average": "AVG",
    "count": "COUNT",
    "count_distinct": "COUNT_DISTINCT",
    "min": "MIN",
    "max": "MAX",
}


def physical_type(value: str) -> str:
    base = value.upper().split("(", 1)[0]
    if base not in TYPE_MAP:
        raise ValueError(f"Unsupported physical type: {value}")
    return TYPE_MAP[base]


def expression(value: str, columns: dict[str, str]) -> str:
    parsed = sqlglot.parse(value, read="hive")
    if len(parsed) != 1 or parsed[0] is None:
        raise ValueError("Expected one scalar SQL expression")
    tree = parsed[0]
    if tree.find(exp.Query) or tree.find(exp.AggFunc):
        raise ValueError("Row expressions must not contain queries or aggregations")
    for column in tree.find_all(exp.Column):
        if column.table or column.name not in columns:
            raise ValueError(f"Unresolved column: {column}")
    return tree.sql(dialect="hive")


def filter_sql(template: str, model: dict[str, Any], columns: dict[str, str]) -> str:
    environment = SandboxedEnvironment(undefined=StrictUndefined)
    tree = environment.parse(template)
    allowed = (
        nodes.Template,
        nodes.Output,
        nodes.TemplateData,
        nodes.Call,
        nodes.Name,
        nodes.Const,
    )
    for node in tree.find_all(nodes.Node):
        if type(node) not in allowed:
            raise ValueError("Unsupported Jinja filter construct")
        if type(node) is nodes.Call and (
            type(node.node) is not nodes.Name
            or node.node.name != "Dimension"
            or len(node.args) != 1
            or node.kwargs
            or node.dyn_args
            or node.dyn_kwargs
        ):
            raise ValueError("Only one-argument Dimension references are supported")
    entities = {
        item["name"] for item in model["entities"] if item["type"] in ("primary", "unique")
    }
    dimensions = {item["name"]: item.get("expr") or item["name"] for item in model["dimensions"]}

    def dimension(reference: str) -> str:
        entity, name = reference.split("__", 1)
        if entity not in entities or name not in dimensions:
            raise ValueError("Cross-model or undeclared filter dimension")
        return expression(dimensions[name], columns)

    return expression(environment.from_string(template).render(Dimension=dimension), columns)


def convert(
    manifest: dict[str, Any], metadata: dict[str, Any], *, timezone: str, join_type: str
) -> dict[str, Any]:
    if timezone != "UTC" or join_type != "LEFT":
        raise ValueError(
            "This importer is qualified only for the reviewed UTC / LEFT join contract"
        )
    models = {item["name"]: item for item in manifest["semantic_models"]}
    if any(item.get("measures") for item in models.values()):
        raise ValueError("This importer requires Core 1.12 latest-spec compiled metrics")
    tables, references, conventions = [], [], []
    primary_targets: dict[str, tuple[str, str]] = {}
    columns_by_model: dict[str, dict[str, str]] = {}
    for name, model in models.items():
        actual = metadata[name]
        relation = model["node_relation"]
        if (
            actual["table_name"] != relation["alias"]
            or actual.get("schema_name") != relation["schema_name"]
        ):
            raise ValueError("Manifest / physical metadata identity mismatch")
        if actual.get("has_more_columns"):
            raise ValueError("Complete physical columns are required")
        columns = {item["name"]: physical_type(item["type"]) for item in actual["columns"]}
        columns_by_model[name] = columns
        references.append(
            {
                "project": relation["database"],
                "schema": relation["schema_name"],
                "table": relation["alias"],
            }
        )
        table: dict[str, Any] = {
            "name": name,
            "baseTable": ".".join(
                [relation["database"], relation["schema_name"], relation["alias"]]
            ),
            "description": model.get("description") or name,
            "dimensions": [],
            "timeDimensions": [],
        }
        primary = [item for item in model["entities"] if item["type"] in ("primary", "unique")]
        if len(primary) != 1:
            raise ValueError("Exactly one declared primary/unique entity is required")
        key = primary[0].get("expr") or primary[0]["name"]
        if key not in columns:
            raise ValueError("Computed primary keys are unsupported")
        if primary[0]["name"] in primary_targets:
            raise ValueError("Ambiguous entity target")
        primary_targets[primary[0]["name"]] = (name, key)
        table["primaryKey"] = {"columns": [key]}
        for dimension in model["dimensions"]:
            value = dimension.get("expr") or dimension["name"]
            if value not in columns:
                raise ValueError("Computed dimension type requires an explicit mapping")
            output = {
                "name": dimension["name"],
                "expr": expression(value, columns),
                "dataType": columns[value],
            }
            if dimension["type"] == "time":
                params = dimension["type_params"]
                if params.get("validity_params"):
                    raise ValueError("SCD validity ranges are unsupported")
                granularity = params["time_granularity"].upper()
                output["usageGuidance"] = (
                    f"Source and session timezone={timezone}; minimum granularity={granularity}."
                )
                table["timeDimensions"].append(output)
                conventions.append(
                    f"{name}.{dimension['name']}: source_timezone={timezone}; session_timezone={timezone}; minimum_granularity={granularity}; default_agg_time={model['defaults'].get('agg_time_dimension') == dimension['name']}."
                )
            elif dimension["type"] == "categorical":
                table["dimensions"].append(output)
            else:
                raise ValueError("Unsupported dimension type")
        table = {key: value for key, value in table.items() if value != []}
        tables.append(table)
    relationships = []
    for name, model in models.items():
        for entity in model["entities"]:
            if entity["type"] != "foreign":
                continue
            if entity["name"] not in primary_targets:
                raise ValueError("Unresolved foreign entity")
            target, right = primary_targets[entity["name"]]
            left = entity.get("expr") or entity["name"]
            if (
                left not in columns_by_model[name]
                or columns_by_model[name][left] != columns_by_model[target][right]
            ):
                raise ValueError("Incompatible or unresolved relationship columns")
            relationships.append(
                {
                    "name": f"{name}_to_{target}",
                    "leftTable": name,
                    "rightTable": target,
                    "relationshipCardinality": "MANY_TO_ONE",
                    "joinType": join_type,
                    "relationshipColumns": [{"leftColumn": left, "rightColumn": right}],
                    "joinCondition": f"{name}.{left} = {target}.{right}",
                    "source": "IMPORTED",
                    "usageGuidance": "Imported declared dbt entities; preserve unmatched fact rows. Validate target key uniqueness and cardinality with dbt/data tests before publication.",
                }
            )
    if any(
        rel["leftTable"] == rel["rightTable"]
        or any(other["leftTable"] == rel["rightTable"] for other in relationships)
        for rel in relationships
    ):
        raise ValueError("Only single-hop foreign-entity relationships are supported")
    filters: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    simple_names = {item["name"] for item in manifest["metrics"] if item["type"] == "simple"}
    for metric in manifest["metrics"]:
        params = metric["type_params"]
        if (
            any(
                params.get(key)
                for key in (
                    "window",
                    "grain_to_date",
                    "join_to_timespine",
                    "fill_nulls_with",
                    "is_private",
                )
            )
            or params.get("fill_nulls_with") is not None
        ):
            raise ValueError(
                "Window, time spine fill, or private metric semantics are unsupported"
            )
        result: dict[str, Any] = {
            "name": metric["name"],
            "description": metric.get("description") or metric["name"],
            "visibility": "PUBLIC",
        }
        if metric["type"] == "simple":
            aggregation = params["metric_aggregation_params"]
            if aggregation.get("non_additive_dimension"):
                raise ValueError("Non-additive dimensions are unsupported")
            name = aggregation["semantic_model"]
            agg = AGG_MAP[aggregation["agg"]]
            value = expression(params["expr"], columns_by_model[name])
            tree = sqlglot.parse_one(value, read="hive")
            if agg not in ("COUNT", "COUNT_DISTINCT"):
                if not isinstance(tree, (exp.Column, exp.Coalesce)) or any(
                    not isinstance(
                        node, (exp.Column, exp.Coalesce, exp.Identifier, exp.Literal, exp.Null)
                    )
                    for node in tree.walk()
                ):
                    raise ValueError(
                        "Only direct-column or COALESCE row expressions have qualified aggregate type inference"
                    )
            expression_columns = list(tree.find_all(exp.Column))
            types = {columns_by_model[name][column.name] for column in expression_columns}
            if len(types) != 1:
                raise ValueError("Expression type inference requires one physical scalar type")
            result.update(
                semanticType="MEASURE",
                baseLogicalTable=name,
                expr=value,
                dataType="BIGINT" if agg in ("COUNT", "COUNT_DISTINCT") else next(iter(types)),
                defaultAggregation=agg,
                timeDimension=aggregation["agg_time_dimension"],
                additivityType="ADDITIVE" if agg in ("SUM", "COUNT") else "NON_ADDITIVE",
            )
            refs = []
            for index, predicate in enumerate(
                (metric.get("filter") or {}).get("where_filters", [])
            ):
                filter_name = f"{metric['name']}_filter_{index}"
                filters.append(
                    {
                        "name": filter_name,
                        "sqlFragment": filter_sql(
                            predicate["where_sql_template"],
                            models[name],
                            columns_by_model[name],
                        ),
                        "applicableTables": [name],
                    }
                )
                refs.append(filter_name)
            if refs:
                result["filterRefs"] = refs
        elif metric["type"] == "ratio":
            if metric.get("filter"):
                raise ValueError("Ratio-level filters are unsupported")
            names = []
            for side in ("numerator", "denominator"):
                component = params[side]
                if component["name"] not in simple_names or any(
                    v for k, v in component.items() if k != "name"
                ):
                    raise ValueError("Ratio inputs must be unmodified simple metrics")
                names.append(component["name"])
            result.update(
                semanticType="CALCULATED_METRIC",
                derivedMetric={
                    "formula": f"{names[0]} / NULLIF({names[1]}, 0)",
                    "refMetrics": names,
                    "resultDataType": "DOUBLE",
                },
                additivityType="NON_ADDITIVE",
                caveats="Divide aggregate numerator by aggregate denominator; zero denominator yields NULL. Do not average precomputed ratios.",
            )
        else:
            raise ValueError(f"Unsupported metric type: {metric['type']}")
        metrics.append(result)
    return {
        "dataReferences": references,
        "semanticModel": {
            "logicalTables": tables,
            "namedFilters": filters,
            "approvedRelationships": relationships,
        },
        "metricDefinitions": metrics,
        "instructions": {
            "generalGuidance": "Imported one-way from dbt definitions; review before publication. Read the exact Published revision; source bindings are recorded in the import command receipt.",
            "timeConventions": conventions,
            "businessRules": [
                {
                    "id": "preserve_unmatched_" + relation["name"],
                    "description": "Keep unmatched "
                    + relation["leftTable"]
                    + " rows when grouping by "
                    + relation["rightTable"]
                    + " dimensions.",
                    "affectedMetrics": sorted(
                        metric["name"]
                        for metric in metrics
                        if metric.get("baseLogicalTable") == relation["leftTable"]
                    ),
                }
                for relation in relationships
            ],
        },
    }
