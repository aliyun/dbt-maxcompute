import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from dbt.clients.jinja import MacroGenerator
from dbt_common.exceptions import DbtRuntimeError, MacroReturn

from dbt.adapters.maxcompute.impl import MaxComputeAdapter
from dbt.adapters.maxcompute.relation import MaxComputeRelation
from dbt.adapters.maxcompute.utils import quote_ref, quote_string


MACROS = Path(__file__).resolve().parents[2] / "dbt/include/maxcompute/macros"
PERSIST_DOCS = "adapters/persist_docs.sql"
CREATE_VIEW = "relations/view/create.sql"
CREATE_TABLE = "relations/table/create.sql"
SQL = "select cast(1 as bigint) as id, 'active' as status, 'Alice' as display_name"
VIEW = MaxComputeRelation.create(
    database="test_project", schema="default", identifier="model", type="view"
)
TABLE = VIEW.incorporate(type="table")
VIEW_DOCS = {
    "description": "Customer list",
    "columns": {
        "display_name": {"description": "Customer's name"},
        "id": {"description": "Customer ID"},
    },
}
CONTRACT_DOCS = {
    "language": "sql",
    "columns": {
        "id": {
            "name": "id",
            "data_type": "bigint",
            "constraints": [{"type": "not_null"}],
            "description": "Customer ID",
        },
        "status": {
            "name": "status",
            "data_type": "string",
            "constraints": [],
            "description": "",
        },
    },
    "constraints": [],
}


class MacroMock(Mock):
    unsafe_callable = False
    alters_data = False


def _return(value):
    raise MacroReturn(value)


def macro_from(package):
    return SimpleNamespace(macro=SimpleNamespace(package_name=package))


def render_macros(file, **context):
    for name in ("persist_docs", "alter_column_comment", "alter_relation_comment"):
        context.setdefault(name, macro_from("dbt"))
    context.update(
        {
            "return": _return,
            "merge_sql_hints_and_header": lambda hints, header: header,
        }
    )
    for name in dict.fromkeys((PERSIST_DOCS, file)):
        source = (MACROS / name).read_text()
        for macro in re.findall(r"\{%[-+]?\s*macro\s+(\w+)\(", source):
            node = SimpleNamespace(
                name=macro,
                macro_sql=source,
                original_file_path=name,
                unique_id=f"macro.dbt_maxcompute.{macro}",
                package_name="dbt_maxcompute",
            )
            context[macro] = MacroGenerator(node, context)
    return SimpleNamespace(**context)


def model_config(materialized, *, columns=True, relation=False, enforced=False, sql_header=None):
    values = {
        "materialized": materialized,
        "contract": SimpleNamespace(enforced=enforced),
        "sql_header": sql_header,
    }
    return SimpleNamespace(
        get=values.get,
        persist_column_docs=lambda: columns,
        persist_relation_docs=lambda: relation,
    )


def adapter_for_docs(external=False):
    adapter = object.__new__(MaxComputeAdapter)
    adapter.is_external_catalog_project = MacroMock(return_value=external)
    adapter.dispatch = MacroMock(return_value=macro_from("dbt_maxcompute"))
    return adapter


def test_view_uses_query_order_and_inlines_both_kinds_of_comment():
    adapter = adapter_for_docs()
    adapter.get_column_schema_from_query = MacroMock(
        return_value=[SimpleNamespace(name=name) for name in ("id", "status", "display_name")]
    )
    empty_query = MacroMock(return_value="select zero rows")
    macros = render_macros(
        CREATE_VIEW,
        adapter=adapter,
        config=model_config("view", relation=True),
        model=VIEW_DOCS,
        get_empty_subquery_sql=empty_query,
    )

    ddl = macros.maxcompute__create_view_as(VIEW, SQL)

    empty_query.assert_called_once_with(SQL, "set dbt.retry=false;")
    adapter.get_column_schema_from_query.assert_called_once_with("select zero rows")
    assert "dbt.retry" not in ddl
    name_comment = quote_string("Customer's name")
    assert (
        f"({quote_ref('id')} COMMENT 'Customer ID', {quote_ref('status')}, "
        f"{quote_ref('display_name')} COMMENT {name_comment})"
    ) in ddl
    assert "COMMENT 'Customer list'" in ddl


@pytest.mark.parametrize(
    ("columns", "external", "replace", "header"),
    [
        (False, False, True, None),
        (True, True, True, None),
        (True, True, False, None),
        (True, False, True, "set example=true;"),
    ],
)
def test_views_outside_scope_keep_the_original_create_sql(columns, external, replace, header):
    adapter = adapter_for_docs(external=external)
    schema_probe = MacroMock()
    macros = render_macros(
        CREATE_VIEW,
        adapter=adapter,
        config=model_config("view", columns=columns, relation=True, sql_header=header),
        model=VIEW_DOCS,
        get_empty_subquery_sql=schema_probe,
    )

    ddl = macros.maxcompute__create_view_as(VIEW, SQL, replace=replace)

    schema_probe.assert_not_called()
    assert "COMMENT" not in ddl
    assert SQL in ddl
    if header:
        assert ddl.count(header) == 1


@pytest.mark.parametrize(
    "name", ["persist_docs", "alter_column_comment", "alter_relation_comment"]
)
def test_view_does_not_inline_when_a_project_overrides_a_docs_macro(name):
    adapter = adapter_for_docs()
    adapter.dispatch.side_effect = lambda macro_name, package: macro_from(
        "custom_project" if macro_name == name else "dbt_maxcompute"
    )
    schema_probe = MacroMock()
    macros = render_macros(
        CREATE_VIEW,
        adapter=adapter,
        config=model_config("view", relation=True),
        model=VIEW_DOCS,
        get_empty_subquery_sql=schema_probe,
    )

    ddl = macros.maxcompute__create_view_as(VIEW, SQL)

    schema_probe.assert_not_called()
    assert "COMMENT" not in ddl


def test_contract_table_does_not_inline_when_a_project_overrides_the_docs_wrapper():
    macros = render_macros(
        CREATE_TABLE,
        adapter=adapter_for_docs(),
        config=model_config("table", enforced=True),
        model=CONTRACT_DOCS,
        persist_docs=macro_from("custom_project"),
        get_assert_columns_equivalent=MacroMock(return_value=""),
    )

    ddl = macros.mc_create_table_ddl(TABLE, SQL, None, None, None, None)

    assert "COMMENT" not in ddl


@pytest.mark.parametrize(
    "probe_error", [DbtRuntimeError("query not supported"), OSError("timeout")]
)
def test_failed_view_probe_leaves_create_and_post_create_docs_available(probe_error):
    adapter = adapter_for_docs()
    adapter.get_column_schema_from_query = MacroMock(side_effect=probe_error)
    adapter.get_odps_table_by_relation = MacroMock(
        return_value=SimpleNamespace(comment=None, view_text=SQL)
    )
    adapter.get_columns_in_relation = MacroMock(
        return_value=[SimpleNamespace(name="id", comment=None)]
    )
    adapter.run_raw_sql = MacroMock()
    config = model_config("view", relation=True)
    macros = render_macros(
        CREATE_VIEW,
        adapter=adapter,
        config=config,
        model=VIEW_DOCS,
        get_empty_subquery_sql=lambda sql, header: sql,
    )

    ddl = macros.maxcompute__create_view_as(VIEW, SQL)

    assert "COMMENT" not in ddl
    assert SQL in ddl
    assert "COMMENT 'Customer list'" in macros.maxcompute__alter_relation_comment(
        VIEW, VIEW_DOCS["description"]
    )
    macros.maxcompute__alter_column_comment(VIEW, VIEW_DOCS["columns"])
    adapter.run_raw_sql.assert_called_once_with(
        f"ALTER VIEW test_project.default.model CHANGE COLUMN {quote_ref('id')} COMMENT 'Customer ID';",
        None,
    )


def test_ambiguous_pyodps_column_schema_uses_post_create_docs():
    adapter = adapter_for_docs()
    adapter.get_column_schema_from_query = MacroMock(
        return_value=[SimpleNamespace(name="_c0", dtype="string")]
    )

    assert not adapter.get_view_column_definitions_for_docs(
        SQL, {"_c0": {"description": "First column"}}
    )


@pytest.mark.parametrize(
    ("materialized", "external", "persist", "inline"),
    [
        ("table", False, True, True),
        ("table", False, False, False),
        ("table", True, True, False),
        ("incremental", False, True, False),
        ("snapshot", False, True, False),
    ],
)
def test_contract_ddl_scopes_comments_and_keeps_constraints(
    materialized, external, persist, inline
):
    macros = render_macros(
        CREATE_TABLE,
        adapter=adapter_for_docs(external=external),
        config=model_config(materialized, columns=persist, enforced=True),
        model=CONTRACT_DOCS,
        this=TABLE,
        get_assert_columns_equivalent=MacroMock(return_value=""),
    )

    ddl = macros.mc_create_table_ddl(TABLE, SQL, None, None, None, None)
    columns = " ".join(ddl.split()).casefold()

    assert "id bigint not null" in columns
    assert "status string" in columns
    assert ("comment 'customer id'" in columns) is inline
    assert "comment ''" not in columns


def test_non_contract_table_keeps_legacy_comments_when_persist_docs_is_off():
    macros = render_macros(
        CREATE_TABLE,
        config=model_config("table", columns=False),
        model={"columns": {"id": {"constraints": [], "description": "Customer ID"}}},
        get_column_schema_from_query=MacroMock(
            return_value=[SimpleNamespace(name="id", dtype="bigint")]
        ),
    )

    ddl = macros.mc_create_table_ddl(TABLE, SQL, None, None, None, None)

    assert "id bigint COMMENT 'Customer ID'" in " ".join(ddl.split())


def test_native_contract_table_reads_column_metadata_once():
    adapter = adapter_for_docs()
    adapter.get_columns_in_relation = MacroMock(
        return_value=[
            SimpleNamespace(name="id", comment="Customer ID"),
            SimpleNamespace(name="status", comment="Old status"),
            SimpleNamespace(name="display_name", comment=None),
        ]
    )
    adapter.get_odps_table_by_relation = MacroMock()
    adapter.run_raw_sql = MacroMock()
    macros = render_macros(
        PERSIST_DOCS,
        adapter=adapter,
        config=model_config("table", enforced=True),
        model=CONTRACT_DOCS,
    )

    macros.maxcompute__alter_column_comment(
        TABLE,
        {
            "id": {"description": "Customer ID"},
            "status": {"description": "New status"},
            "display_name": {"description": ""},
            "not_in_query": {"description": "Ignore"},
        },
    )

    adapter.get_columns_in_relation.assert_called_once_with(TABLE)
    adapter.get_odps_table_by_relation.assert_not_called()
    adapter.run_raw_sql.assert_called_once_with(
        f"ALTER TABLE test_project.default.model CHANGE COLUMN {quote_ref('status')} COMMENT 'New status';",
        None,
    )


@pytest.mark.parametrize(
    ("materialized", "language", "enforced", "external", "relation"),
    [
        ("seed", "sql", False, False, TABLE),
        ("snapshot", "sql", True, False, TABLE),
        ("incremental", "sql", True, False, TABLE),
        ("table", "sql", False, False, TABLE),
        ("table", "python", True, False, TABLE),
        ("table", "sql", True, True, TABLE),
        ("view", "sql", False, True, VIEW),
        ("materialized_view", "sql", False, False, VIEW.incorporate(type="materialized_view")),
    ],
)
def test_existing_materializations_keep_per_column_dispatch(
    materialized, language, enforced, external, relation
):
    adapter = adapter_for_docs(external=external)
    adapter.get_columns_in_relation = MacroMock(return_value=[SimpleNamespace(name="id")])
    adapter.add_comment_to_column = MacroMock(return_value="")
    adapter.add_comments_to_columns = MacroMock()
    model = {**CONTRACT_DOCS, "language": language}
    macros = render_macros(
        PERSIST_DOCS,
        adapter=adapter,
        config=model_config(materialized, enforced=enforced),
        model=model,
    )

    macros.maxcompute__alter_column_comment(relation, model["columns"])

    adapter.add_comment_to_column.assert_called_once_with(relation, "id", "Customer ID")
    adapter.add_comments_to_columns.assert_not_called()


def test_relation_only_view_keeps_existing_comment_ddl():
    adapter = adapter_for_docs()
    adapter.view_comment_matches = MacroMock()
    adapter.add_comment = MacroMock(return_value="legacy view DDL")
    macros = render_macros(
        PERSIST_DOCS,
        adapter=adapter,
        config=model_config("view", columns=False, relation=True),
        model=VIEW_DOCS,
    )
    assert (
        macros.maxcompute__alter_relation_comment(VIEW, "Customer list").strip()
        == "legacy view DDL"
    )
    adapter.view_comment_matches.assert_not_called()


def test_matching_view_comment_does_not_replace_the_view():
    adapter = adapter_for_docs()
    description = 'Customer\'s "list"\nC:\\temp\\'
    view = SimpleNamespace(comment=r"""Customer's \"list\"\nC:\\temp\\""", view_text=SQL)
    adapter.get_odps_table_by_relation = MacroMock(return_value=view)
    macros = render_macros(
        PERSIST_DOCS,
        adapter=adapter,
        config=model_config("view", relation=True),
        model=VIEW_DOCS,
    )

    assert not macros.maxcompute__alter_relation_comment(VIEW, description).strip()
    assert not adapter.view_comment_matches(VIEW, r"""Customer's "list"\nC:\temp\\""")


def catalog_persist_context(*, result, custom_columns=False, sql_header=None):
    adapter = adapter_for_docs()
    adapter.persist_table_comments_via_catalog = MacroMock(return_value=result)
    relation_sql = MacroMock(return_value="legacy relation comment")
    relation_sql.macro = SimpleNamespace(package_name="dbt")
    column_sql = MacroMock(return_value="")
    column_sql.macro = SimpleNamespace(package_name="custom_project" if custom_columns else "dbt")
    macros = render_macros(
        PERSIST_DOCS,
        adapter=adapter,
        config=model_config("table", relation=True, sql_header=sql_header),
        model=VIEW_DOCS,
        alter_relation_comment=relation_sql,
        alter_column_comment=column_sql,
        run_query=MacroMock(),
    )
    return macros


@pytest.mark.parametrize(
    ("for_relation", "for_columns"),
    [(True, True), (True, False), (False, True)],
)
def test_default_persist_docs_combines_enabled_comments(for_relation, for_columns):
    macros = catalog_persist_context(result=True)

    macros.maxcompute__persist_docs(TABLE, VIEW_DOCS, for_relation, for_columns)

    macros.adapter.persist_table_comments_via_catalog.assert_called_once_with(
        TABLE,
        VIEW_DOCS["description"] if for_relation else None,
        VIEW_DOCS["columns"] if for_columns else {},
    )
    macros.alter_relation_comment.assert_not_called()
    macros.alter_column_comment.assert_not_called()
    macros.run_query.assert_not_called()


@pytest.mark.parametrize(
    ("relation", "custom_columns", "sql_header", "catalog_attempted"),
    [
        (TABLE, False, None, True),
        (VIEW, False, None, False),
        (TABLE, True, None, False),
        (TABLE, False, "set odps.test=true;", False),
    ],
)
def test_persist_docs_keeps_the_existing_fallback(
    relation, custom_columns, sql_header, catalog_attempted
):
    macros = catalog_persist_context(
        result=False, custom_columns=custom_columns, sql_header=sql_header
    )

    macros.maxcompute__persist_docs(relation, VIEW_DOCS, True, True)

    assert macros.adapter.persist_table_comments_via_catalog.called == catalog_attempted
    macros.alter_relation_comment.assert_called_once_with(relation, VIEW_DOCS["description"])
    macros.alter_column_comment.assert_called_once_with(relation, VIEW_DOCS["columns"])
    macros.run_query.assert_called_once_with("legacy relation comment")
