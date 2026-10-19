"""Whole-table replacement against a real MaxCompute project."""

import pytest
from dbt.tests.util import relation_from_name, run_dbt

MODEL = """
{{ config(
    materialized='incremental',
    incremental_strategy='truncate_insert',
    full_refresh=var('allow_rebuild', none),
    on_schema_change=var('schema_change', 'ignore')
) }}
{% set mode = var('mode', 'initial') %}
{% if mode == 'invalid' %}
select unknown_column from {{ this }}
{% elif mode == 'reordered' %}
select 'replacement' as value, cast(3 as bigint) as id
{% elif mode == 'empty' %}
select cast(null as bigint) as id, cast(null as string) as value where false
{% elif mode == 'schema_changed' %}
select cast(3 as string) as id, cast(42 as bigint) as value, 'replacement' as extra_value
{% else %}
select cast(1 as bigint) as id, 'original' as value
union all select cast(2 as bigint), 'old'
{% endif %}
"""


@pytest.mark.integration_smoke
class TestTruncateInsert:
    @pytest.fixture(scope="class")
    def models(self):
        return {"whole_table.sql": MODEL}

    def _rows(self, project):
        return sorted(
            tuple(row)
            for row in project.run_sql("select id, value from {schema}.whole_table", fetch="all")
        )

    def _table(self, project):
        return project.adapter.get_odps_table_by_relation(
            relation_from_name(project.adapter, "whole_table")
        )

    def _assert_no_temp(self, project):
        assert (
            project.adapter.get_relation(
                project.database, project.test_schema, "whole_table__dbt_tmp"
            )
            is None
        )

    def test_replacement_empty_repeat_and_table_metadata(self, project):
        run_dbt(["run"])
        assert self._rows(project) == [(1, "original"), (2, "old")]
        project.run_sql("alter table {schema}.whole_table set comment 'keep table object'")
        before = self._table(project)
        identity = (before.creation_time, before.owner, before.comment)

        # on_schema_change returns source columns in the reordered SELECT order;
        # OVERWRITE must still project in the target's order, with no wrong values.
        args = ["run", "--vars", "{mode: reordered, schema_change: fail}"]
        run_dbt(args)
        assert self._rows(project) == [(3, "replacement")]
        run_dbt(args)
        assert self._rows(project) == [(3, "replacement")]
        self._assert_no_temp(project)

        run_dbt(["run", "--vars", "{mode: empty}"])
        assert self._rows(project) == []
        after = self._table(project)
        assert (after.creation_time, after.owner, after.comment) == identity
        self._assert_no_temp(project)

    def test_failed_stage_keeps_old_rows(self, project):
        run_dbt(["run"])
        before = self._rows(project)
        run_dbt(["run", "--vars", "{mode: invalid}"], expect_pass=False)
        assert self._rows(project) == before
        self._assert_no_temp(project)

    def test_full_refresh_false_keeps_table(self, project):
        run_dbt(["run"])
        before = self._table(project).creation_time
        run_dbt(["run", "--full-refresh", "--vars", "{mode: reordered, allow_rebuild: false}"])
        assert self._rows(project) == [(3, "replacement")]
        assert self._table(project).creation_time == before
        self._assert_no_temp(project)

    @pytest.mark.parametrize(
        "refresh_args,refresh_vars",
        [
            (["--full-refresh"], "{mode: schema_changed, schema_change: fail}"),
            ([], "{mode: schema_changed, schema_change: fail, allow_rebuild: true}"),
        ],
    )
    def test_full_refresh_rebuilds_changed_schema(self, project, refresh_args, refresh_vars):
        run_dbt(["run", "--full-refresh"])
        project.run_sql("alter table {schema}.whole_table set comment 'old target metadata'")

        run_dbt(["run", *refresh_args, "--vars", refresh_vars])
        table = self._table(project)
        assert [
            (column.name, str(column.type).lower()) for column in table.table_schema.simple_columns
        ] == [
            ("id", "string"),
            ("value", "bigint"),
            ("extra_value", "string"),
        ]
        assert self._rows(project) == [("3", 42)]
        assert list(
            project.run_sql("select extra_value from {schema}.whole_table", fetch="all")[0]
        ) == ["replacement"]
        assert table.comment != "old target metadata"
        self._assert_no_temp(project)

        # The rebuilt schema can be used by the next ordinary overwrite.
        run_dbt(
            ["run", "--vars", "{mode: schema_changed, schema_change: fail, allow_rebuild: false}"]
        )
        assert self._rows(project) == [("3", 42)]
        self._assert_no_temp(project)
