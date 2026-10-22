"""Render the real dbt macros without credentials, including their materialization.

The server suite covers actual replacement. These tests pin column order and
the new strategy's validation boundary without changing any legacy strategy.
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from dbt.clients.jinja import MacroGenerator
from dbt.adapters.base.impl import DEFAULT_BASE_BEHAVIOR_FLAGS
from dbt_common.exceptions import CompilationError, MacroReturn
from odps.models import Project, Table
from odps.models.table import TableSchema

from dbt.adapters.maxcompute.impl import MaxComputeAdapter
from dbt.adapters.maxcompute.relation import MaxComputeRelation

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_NAME = yaml.safe_load((ROOT / "dbt/include/maxcompute/dbt_project.yml").read_text())[
    "name"
]
INCREMENTAL = ROOT / "dbt/include/maxcompute/macros/materializations/incremental/incremental.sql"
STRATEGY = INCREMENTAL.parent / "incremental_strategy/truncate_insert.sql"
LEGACY = [None, "merge", "append", "delete+insert", "insert_overwrite", "microbatch"]
TARGET = MaxComputeRelation.create(
    database="project", schema="default", identifier="target", type="table"
)


def _return(value):
    raise MacroReturn(value)


def _error(message):
    raise CompilationError(message)


def _load(path, context, materialization=False):
    source = path.read_text()
    names = re.findall(r"\{%[-+]?\s*macro\s+(\w+)\(", source)
    if materialization:
        names.append("materialization_incremental_maxcompute")
    for name in names:
        node = SimpleNamespace(
            name=name,
            macro_sql=source,
            original_file_path=str(path.relative_to(ROOT)),
            unique_id=f"macro.{PACKAGE_NAME}.{name}",
        )
        context[name] = MacroGenerator(node, context)


def _context(values=None, existing=TARGET, full_refresh=False, partitioned=False, language="sql"):
    values = dict(values or {})
    events = []
    columns = [SimpleNamespace(name="id"), SimpleNamespace(name="value")]
    adapter = SimpleNamespace(
        parse_partition_by=lambda value: value,
        get_odps_table_by_relation=lambda relation: SimpleNamespace(
            type=Table.Type.MANAGED_TABLE,
            table_schema=SimpleNamespace(partitions=["ds"] if partitioned else []),
        ),
        get_columns_in_relation=lambda relation: columns,
        quote=MaxComputeAdapter.quote,
        drop_relation=lambda relation: events.append(("drop", relation.identifier)) or "",
        rename_relation=lambda source, target: events.append(
            ("rename", source.identifier, target.identifier)
        )
        or "",
    )
    context = {
        "return": _return,
        "exceptions": SimpleNamespace(raise_compiler_error=_error),
        "adapter": adapter,
        "config": values,
        "get_quoted_csv": lambda names: ", ".join(adapter.quote(name) for name in names),
    }
    _load(STRATEGY, context)
    _load(ROOT / "dbt/include/maxcompute/macros/adapters.sql", context)
    adapter.dispatch = lambda name, package: context[f"maxcompute__{name}"]

    def statement(name, caller, **kwargs):
        events.append(("statement", name, " ".join(caller().split())))
        return ""

    def load_relation(relation):
        return existing if relation.identifier == TARGET.identifier else None

    context.update(
        model={"language": language},
        this=TARGET,
        sql="select 2 as id, 'new' as value",
        compiled_code="select 2 as id, 'new' as value",
        load_cached_relation=load_relation,
        make_temp_relation=lambda relation, suffix="__dbt_tmp": relation.incorporate(
            path={"identifier": relation.identifier + suffix}
        ),
        make_intermediate_relation=lambda relation: relation.incorporate(
            path={"identifier": relation.identifier + "__dbt_int"}
        ),
        make_backup_relation=lambda relation, kind: relation.incorporate(
            path={"identifier": relation.identifier + "__dbt_bak"}
        ),
        should_full_refresh=lambda: (
            full_refresh if values.get("full_refresh") is None else values["full_refresh"]
        ),
        incremental_validate_on_schema_change=lambda value, default: value or default,
        merge_sql_hints_and_header=lambda hints, header: header,
        pre_hooks=[],
        post_hooks=[],
        run_hooks=lambda *args, **kwargs: events.append(("hooks",)) or "",
        drop_relation_if_exists=lambda relation: (
            adapter.drop_relation(relation) if relation is not None else ""
        ),
        statement=statement,
        create_table_as_internal=lambda temporary, relation, sql, *args, **kwargs: (
            f"create table {relation} as {sql}"
        ),
        should_revoke=lambda relation, mode: mode,
        apply_grants=lambda *args, **kwargs: "",
        persist_docs=lambda *args: "",
        mc_expand_target_column_types=lambda *args: None,
        mc_validate_microbatch_config=lambda *args: None,
        log=lambda message: "",
    )
    if language == "python":
        context.update(
            invocation_id="abcdef123456",
            load_relation=lambda relation: None,
            maxframe_write_table=lambda *args, **kwargs: "python job",
            maxframe_select_without_sentinel=lambda relation: "select id, value from stage",
            process_schema_changes=lambda *args: None,
        )
    _load(INCREMENTAL, context, materialization=True)
    adapter.get_incremental_strategy_macro = lambda ctx, strategy: (
        context["get_incremental_truncate_insert_sql"]
        if strategy == "truncate_insert"
        else lambda args: f"legacy {strategy}"
    )
    context["context"] = context
    return context, events


def _run(context):
    return context["materialization_incremental_maxcompute"]()


def test_dispatch_and_projection_use_actual_target_order_not_source_columns():
    context, _ = _context()
    context["adapter"].get_columns_in_relation = lambda relation: [
        SimpleNamespace(name="value"),
        SimpleNamespace(name="odd`name"),
        SimpleNamespace(name="id"),
    ]
    sql = context["get_incremental_truncate_insert_sql"](
        {
            "target_relation": TARGET,
            "temp_relation": "staged",
            "dest_columns": [SimpleNamespace(name="id"), SimpleNamespace(name="value")],
        }
    )
    assert " ".join(sql.split()) == (
        f"insert overwrite table {TARGET} " "select `value`, `odd``name`, `id` from staged"
    )


def test_normal_run_stages_before_one_overwrite_and_cleans_only_temp():
    context, events = _context({"incremental_strategy": "truncate_insert"})
    assert _run(context) == {"relations": [TARGET.incorporate(type="table")]}
    statements = [event for event in events if event[0] == "statement"]
    assert [event[1] for event in statements] == ["create_temp_relation", "main"]
    assert "insert overwrite table" in statements[-1][2]
    assert "select `id`, `value` from" in statements[-1][2]
    assert [event for event in events if event[0] == "drop"] == [("drop", "target__dbt_tmp")]


def test_first_run_creates_target():
    context, events = _context({"incremental_strategy": "truncate_insert"}, existing=None)
    _run(context)
    assert [event[1] for event in events if event[0] == "statement"] == ["main"]
    assert "create table" in next(event[2] for event in events if event[0] == "statement")
    assert not [event for event in events if event[0] == "drop"]


def test_python_staging_uses_same_overwrite_and_cleans_its_relations():
    context, events = _context({"incremental_strategy": "truncate_insert"}, language="python")
    _run(context)
    statements = [event for event in events if event[0] == "statement"]
    assert [event[1] for event in statements] == [
        "create_temp_relation_maxframe_stage",
        "create_temp_relation",
        "create_temp_relation",
        "main",
    ]
    assert "insert overwrite table" in statements[-1][2]
    assert [event for event in events if event[0] == "drop"] == [
        ("drop", "target__dbt_mf_abcdef12"),
        ("drop", "target__dbt_tmp"),
    ]


@pytest.mark.parametrize(
    "values,kwargs,message",
    [
        ({"partition_by": {"field": "ds"}}, {}, "non-partitioned"),
        ({"partitions": ["2026-01-01"]}, {}, "non-partitioned"),
        ({"predicates": ["id > 1"]}, {}, "entire table"),
        ({"incremental_predicates": ["id > 1"]}, {}, "entire table"),
    ],
)
@pytest.mark.parametrize("full_refresh", [False, True])
def test_invalid_new_strategy_fails_before_hooks_or_writes(values, kwargs, message, full_refresh):
    context, events = _context(
        {"incremental_strategy": "truncate_insert", **values}, full_refresh=full_refresh, **kwargs
    )
    with pytest.raises(CompilationError, match=message):
        _run(context)
    assert events == []


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"partitioned": True}, "target is partitioned"),
        ({"existing": TARGET.incorporate(type="external")}, "existing table"),
    ],
)
@pytest.mark.parametrize("full_refresh", [False, True])
def test_invalid_existing_target_is_rejected_when_full_refresh_is_disabled(
    kwargs, message, full_refresh
):
    context, events = _context(
        {"incremental_strategy": "truncate_insert", "full_refresh": False},
        full_refresh=full_refresh,
        **kwargs,
    )
    with pytest.raises(CompilationError, match=message):
        _run(context)
    assert events == []


@pytest.mark.parametrize(
    "existing,partitioned,full_refresh",
    [
        (TARGET.incorporate(type="view"), False, False),
        (TARGET.incorporate(type="view"), False, True),
        (TARGET, True, True),
        (TARGET.incorporate(type="external"), False, True),
        (TARGET, False, True),
    ],
)
@pytest.mark.parametrize("language", ["sql", "python"])
def test_rebuild_skips_old_target_metadata(existing, partitioned, full_refresh, language):
    context, events = _context(
        {"incremental_strategy": "truncate_insert"},
        existing=existing,
        partitioned=partitioned,
        full_refresh=full_refresh,
        language=language,
    )
    context["adapter"].get_odps_table_by_relation = lambda relation: pytest.fail(
        "rebuilding must not inspect metadata of the old target"
    )
    _run(context)
    statements = [event for event in events if event[0] == "statement"]
    assert any("create table" in event[2] for event in statements)
    assert not any("insert overwrite" in event[2] for event in statements)
    if language == "sql":
        assert events.index(("drop", "target")) < events.index(statements[0])
    else:
        assert ("rename", "target", "target__dbt_bak") in events
        assert ("rename", "target__dbt_int", "target") in events
        assert ("drop", "target__dbt_bak") in events


def test_unreadable_target_metadata_does_not_allow_an_unchecked_overwrite():
    context, events = _context({"incremental_strategy": "truncate_insert"})
    context["adapter"].get_odps_table_by_relation = lambda relation: None
    with pytest.raises(CompilationError, match="Cannot read"):
        _run(context)
    assert events == []


@pytest.mark.parametrize("language", ["sql", "python"])
@pytest.mark.parametrize("full_refresh", [False, True])
def test_full_refresh_false_keeps_new_strategy_on_the_overwrite_path(language, full_refresh):
    context, events = _context(
        {"incremental_strategy": "truncate_insert", "full_refresh": False},
        full_refresh=full_refresh,
        language=language,
    )
    _run(context)
    assert not any(event[:2] == ("drop", "target") for event in events)
    assert not any(event[0] == "rename" for event in events)
    assert any("insert overwrite" in event[-1] for event in events if event[0] == "statement")


@pytest.mark.parametrize(
    "values,full_refresh",
    [({}, True), ({"full_refresh": None}, True), ({"full_refresh": True}, False)],
)
def test_full_refresh_rebuilds_sql_target_from_new_schema(values, full_refresh):
    context, events = _context(
        {"incremental_strategy": "truncate_insert", "on_schema_change": "fail", **values},
        full_refresh=full_refresh,
    )
    new_sql = "select cast(3 as string) as id, 42 as replacement"
    context["sql"] = new_sql
    context["process_schema_changes"] = lambda *args: pytest.fail(
        "full-refresh must rebuild without reconciling the old schema"
    )
    context["adapter"].get_incremental_strategy_macro = lambda *args: pytest.fail(
        "full-refresh must use the shared rebuild path"
    )
    _run(context)
    statements = [event for event in events if event[0] == "statement"]
    assert statements == [("statement", "main", f"create table {TARGET} as {new_sql}")]
    assert events.index(("drop", "target")) < events.index(statements[0])
    assert not any(event[0] == "rename" for event in events)


@pytest.mark.parametrize(
    "values,full_refresh",
    [({}, True), ({"full_refresh": None}, True), ({"full_refresh": True}, False)],
)
def test_full_refresh_rebuilds_python_target_through_intermediate(values, full_refresh):
    context, events = _context(
        {"incremental_strategy": "truncate_insert", **values},
        full_refresh=full_refresh,
        language="python",
    )
    context["adapter"].get_incremental_strategy_macro = lambda *args: pytest.fail(
        "full-refresh must use the shared rebuild path"
    )
    _run(context)
    statements = [event for event in events if event[0] == "statement"]
    assert [event[1] for event in statements] == ["main", "create_maxframe_relation"]
    assert "target__dbt_int" in statements[-1][2]
    renames = [event for event in events if event[0] == "rename"]
    assert renames == [
        ("rename", "target", "target__dbt_bak"),
        ("rename", "target__dbt_int", "target"),
    ]
    assert events.index(statements[-1]) < events.index(renames[0])
    assert events.index(renames[-1]) < events.index(("drop", "target__dbt_bak"))
    assert ("drop", "target") not in events
    assert not any("insert overwrite" in event[-1] for event in statements)


@pytest.mark.parametrize("strategy", LEGACY)
def test_legacy_full_refresh_still_rebuilds_without_new_validation(strategy):
    context, events = _context({"incremental_strategy": strategy}, full_refresh=True)
    context["mc_validate_truncate_insert_config"] = lambda *args: pytest.fail(
        "legacy strategy called the new validation"
    )
    _run(context)
    assert ("drop", "target") in events
    assert any("create table" in event[-1] for event in events if event[0] == "statement")


@pytest.mark.parametrize("strategy", LEGACY)
def test_legacy_incremental_dispatch_is_unchanged(strategy):
    context, events = _context({"incremental_strategy": strategy})
    context["mc_validate_truncate_insert_config"] = lambda *args: pytest.fail(
        "legacy strategy called the new validation"
    )
    for name in (
        "mc_generate_incremental_insert_overwrite_build_sql",
        "mc_generate_microbatch_build_sql",
    ):
        context[name] = lambda *args: "legacy special"
    _run(context)
    main = next(event[2] for event in events if event[:2] == ("statement", "main"))
    assert "insert overwrite table" not in main
    assert "legacy" in main
    assert ("drop", "target") not in events


def test_advertised_strategies_keep_all_existing_names():
    strategies = MaxComputeAdapter.__new__(MaxComputeAdapter).valid_incremental_strategies()
    assert set(LEGACY[1:]) | {"truncate_insert"} == set(strategies)


@pytest.mark.parametrize("override", ["entry", "dispatch"])
@pytest.mark.parametrize("full_refresh", [False, True])
def test_existing_project_strategy_overrides_keep_partition_and_refresh_behavior(
    override, full_refresh
):
    context, events = _context(
        {"incremental_strategy": "truncate_insert", "partition_by": {"field": "ds"}},
        full_refresh=full_refresh,
        partitioned=True,
    )
    name = (
        "get_incremental_truncate_insert_sql"
        if override == "entry"
        else "maxcompute__get_incremental_truncate_insert_sql"
    )
    custom = MacroGenerator(
        SimpleNamespace(
            name=name,
            macro_sql=(
                "{% macro " + name + "(args) %}"
                "{{ return('CUSTOM EXISTING STRATEGY') }}{% endmacro %}"
            ),
            unique_id=f"macro.existing_project.{name}",
            original_file_path="macros/custom_incremental.sql",
        ),
        context,
    )
    context[name] = custom
    if override == "entry":
        context["adapter"].dispatch = lambda *args: pytest.fail(
            "custom entry must not resolve the built-in dispatch"
        )
    # Exercise dbt's actual custom-name resolution instead of the fixture stub.
    resolver = MaxComputeAdapter.__new__(MaxComputeAdapter)
    resolver.config = SimpleNamespace(project_name="existing_project")
    resolver.behavior = list(DEFAULT_BASE_BEHAVIOR_FLAGS)
    context["adapter"].get_incremental_strategy_macro = resolver.get_incremental_strategy_macro
    _run(context)
    if full_refresh:
        assert ("drop", "target") in events
        assert any("create table" in event[-1] for event in events if event[0] == "statement")
    else:
        assert next(event[2] for event in events if event[:2] == ("statement", "main")) == (
            "CUSTOM EXISTING STRATEGY"
        )
        assert ("drop", "target") not in events


@pytest.mark.parametrize("full_refresh", [False, True])
def test_real_external_table_metadata_is_checked_only_for_incremental_runs(full_refresh):
    table = Table(
        name="target",
        parent=Project(name="project"),
        _loaded=True,
        type=Table.Type.EXTERNAL_TABLE,
        table_schema=TableSchema.from_lists(["id", "value"], ["bigint", "string"]),
        is_virtual_view=False,
        is_materialized_view=False,
    )
    relation = MaxComputeRelation.from_odps_table(table)
    assert relation.is_table  # External ODPS tables share this dbt relation type.
    context, events = _context(
        {"incremental_strategy": "truncate_insert"}, existing=relation, full_refresh=full_refresh
    )
    context["adapter"].get_odps_table_by_relation = lambda relation: table
    if full_refresh:
        _run(context)
        assert ("drop", "target") in events
        assert any("create table" in event[2] for event in events if event[0] == "statement")
        return
    with pytest.raises(CompilationError, match="external"):
        _run(context)
    assert events == []
