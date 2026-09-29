"""Declared column widths on the incremental temp path, measured on a real server.

dbt-core's incremental materialization calls
``adapter.expand_target_column_types(from_relation=temp, to_relation=target)`` before it
merges a temp relation into an existing model. This file first recorded that this adapter
never made that call, and that its ``maxcompute__alter_column_type`` only rendered the
statement instead of submitting it -- so a model whose target column is ``varchar(10)``
silently kept the first 10 characters of a 16-character value. Both halves are fixed now;
the tests below measure the fixed behaviour.

What a real server says about the DDL involved (5 table shapes x both directions, each
column read back afterwards rather than trusting that the statement was accepted):

* widening is accepted on plain, partitioned, transactional, Append Delta and PK Delta
  tables, and the rows already stored keep their content;
* a change that could lose characters -- ``string`` to ``varchar(n)``, ``varchar(10)`` to
  ``char(5)``, ``char(5)`` to ``char(4)`` -- is rejected by the server rather than
  applied, so an automatic widening cannot corrupt a table;
* a longer value written into a narrower column *succeeds* and truncates. The loss
  happens on the insert, which is why the guard runs before it and not after.
"""

import pytest
from dbt.tests.util import relation_from_name, run_dbt

import dbt.adapters.maxcompute.impl as maxcompute_impl
from dbt.adapters.maxcompute.impl import MaxComputeAdapter

# The value a narrow target cannot hold. Spelled once, with its length asserted below,
# because a test that hard-codes both the literal and the expected length will eventually
# disagree with itself: the first draft of this file wrote "16" for a 17-character literal
# and only the widened case -- where nothing truncates it -- noticed.
LONG_VALUE = "x" * 15 + "y"
NARROW_VALUE = "abcdefghij"


def test_the_fixtures_are_what_the_tests_claim():
    assert len(LONG_VALUE) == 16
    assert len(NARROW_VALUE) == 10

# First run creates the target with a declared varchar(10); the incremental run selects an
# unbounded 16-character string -- what an upstream column that became a plain `string`
# produces. The incoming width is unknowable, so the default leaves the declared bound
# alone and says so.
UNBOUNDED_MODEL = """
{{ config(materialized='incremental', incremental_strategy='append') }}
{%- if is_incremental() %}
select 'xxxxxxxxxxxxxxxy' as c
{%- else %}
select cast('abcdefghij' as varchar(10)) as c
{%- endif %}
"""

# The same shape, but the incoming column declares how wide it is: 16 characters.
BOUNDED_MODEL = """
{{ config(materialized='incremental', incremental_strategy='append') }}
{%- if is_incremental() %}
select cast('xxxxxxxxxxxxxxxy' as varchar(16)) as c
{%- else %}
select cast('abcdefghij' as varchar(10)) as c
{%- endif %}
"""

# `expand_column_types='widen'` buys the value at the price of the declared bound.
GIVE_UP_WIDTH_MODEL = UNBOUNDED_MODEL.replace(
    "incremental_strategy='append')",
    "incremental_strategy='append', expand_column_types='widen')",
)

# Both sides are unbounded `string`: there is nothing to widen and no DDL to send.
PLAIN_MODEL = """
{{ config(materialized='incremental', incremental_strategy='append') }}
select 'abcdefghij' as c
"""

# `off` restores the pre-fix behaviour: no DDL, no notice, silent truncation.
LEGACY_MODEL = UNBOUNDED_MODEL.replace(
    "incremental_strategy='append')",
    "incremental_strategy='append', expand_column_types='off')",
)


def column_shapes(adapter, name):
    """Server read-back of (dtype, char_size) for every column of a table."""
    return [
        (col.dtype, col.char_size)
        for col in adapter.get_columns_in_relation(relation_from_name(adapter, name))
    ]


def lengths(project, name):
    rows = project.run_sql(
        f"select length(c) from {project.test_schema}.{name} order by 1", fetch="all"
    )
    return sorted(int(row[0]) for row in rows)


@pytest.fixture
def widen_notes(monkeypatch):
    """The notices the widening pass leaves behind, without intercepting its DDL."""
    notes = []

    class Recorder:
        def info(self, message):
            notes.append(("info", str(message)))

        def warning(self, message):
            notes.append(("warning", str(message)))

        def debug(self, message):
            pass

        def error(self, message):
            notes.append(("error", str(message)))

    monkeypatch.setattr(maxcompute_impl, "logger", Recorder())
    return {"calls": [], "notes": notes}


@pytest.fixture
def widen_calls(monkeypatch):
    """Every DDL the widening pass *tries* to submit, with nothing actually sent.

    For the cases whose point is that no statement goes out: intercepting makes that
    observable instead of inferred from a schema that would look identical either way.
    """
    calls = []

    def record_alter(self, relation, column_name, new_column_type):
        calls.append((relation.identifier, column_name, new_column_type))

    monkeypatch.setattr(MaxComputeAdapter, "alter_column_type", record_alter)
    return {"calls": calls, "notes": []}


class TestIncomingWidthIsKnown:
    """The common shape: an upstream column that grew from varchar(10) to varchar(16)."""

    @pytest.fixture(scope="class")
    def models(self):
        return {"bounded_target.sql": BOUNDED_MODEL}

    def test_append_widens_to_the_incoming_width(self, project):
        run_dbt(["run"])
        assert column_shapes(project.adapter, "bounded_target") == [("varchar(10)", 10)]

        result = run_dbt(["run"])
        assert result[0].status == "success"
        assert column_shapes(project.adapter, "bounded_target") == [("varchar(16)", 16)]
        # Both rows are intact: the 10-character original and the 16-character newcomer.
        assert lengths(project, "bounded_target") == [10, 16]


class TestIncomingWidthIsUnknown:
    """A `string` source against a declared width: the bound is kept, and the cost is named."""

    @pytest.fixture(scope="class")
    def models(self):
        return {"narrow_target.sql": UNBOUNDED_MODEL}

    def test_declared_width_survives_and_the_truncation_is_reported(self, project, widen_notes):
        run_dbt(["run"])
        result = run_dbt(["run"])
        assert result[0].status == "success"
        # MaxCompute cannot go back from `string` to `varchar(n)`, so the default does not
        # spend a declared bound it cannot restore.
        assert column_shapes(project.adapter, "narrow_target") == [("varchar(10)", 10)]
        assert lengths(project, "narrow_target") == [10, 10]
        reported = [
            note
            for level, note in widen_notes["notes"]
            if level == "warning" and "narrow_target" in note
        ]
        assert reported, f"truncated with nothing said: {widen_notes['notes']}"
        assert "unbounded string" in reported[0]


class TestWidenModeBuysTheValue:
    """`expand_column_types='widen'` gives up the bound instead of the characters."""

    @pytest.fixture(scope="class")
    def models(self):
        return {"give_up_width.sql": GIVE_UP_WIDTH_MODEL}

    def test_column_becomes_unbounded_and_keeps_both_values(self, project):
        run_dbt(["run"])
        assert column_shapes(project.adapter, "give_up_width") == [("varchar(10)", 10)]

        run_dbt(["run"])
        assert column_shapes(project.adapter, "give_up_width") == [("string", None)]
        assert lengths(project, "give_up_width") == [10, 16]


class TestNothingToSendSendsNothing:
    """Two unbounded `string` columns: no DDL, and no invented reason to send one."""

    @pytest.fixture(scope="class")
    def models(self):
        return {"plain_target.sql": PLAIN_MODEL}

    def test_string_to_string_emits_no_alter_statement(self, project, widen_calls):
        run_dbt(["run"])
        run_dbt(["run"])
        assert widen_calls["calls"] == [], widen_calls["calls"]
        assert column_shapes(project.adapter, "plain_target") == [("string", None)]


class TestOffModeIsTheOldBehaviour:
    """`expand_column_types='off'` leaves the target schema exactly as it found it."""

    @pytest.fixture(scope="class")
    def models(self):
        return {"legacy.sql": LEGACY_MODEL}

    def test_no_ddl_and_the_value_is_still_cut(self, project, widen_calls):
        run_dbt(["run"])
        run_dbt(["run"])
        assert column_shapes(project.adapter, "legacy") == [("varchar(10)", 10)]
        assert lengths(project, "legacy") == [10, 10]
        assert widen_calls["calls"] == [], widen_calls["calls"]


class TestColumnsTheServerWillNotReType:
    """Primary key and partition columns are skipped per column -- and said out loud.

    ``alter table ... change column`` is refused for them
    (`column id cannot be changed except for its comment because it is a primary key
    column` / `invalid alter operation: partition keys can not be changed`), so offering
    them a DDL would fail runs that are green today. This adapter's
    ``get_columns_in_relation`` returns non-generated partition columns too, which is what
    makes the skip something that has to be deliberate rather than incidental.
    """

    TARGET = "widen_guard_target"
    STAGE = "widen_guard_stage"

    @pytest.fixture(scope="class")
    def relations(self, project):
        project.run_sql(
            f"create table {project.test_schema}.{self.TARGET} "
            f"(id varchar(10) not null, c varchar(10), primary key(id)) "
            'tblproperties("transactional"="true","write.bucket.num"="16")'
        )
        project.run_sql(
            f"create table {project.test_schema}.{self.STAGE} as select "
            f"cast('abcdefghijklmnop' as varchar(16)) as id, "
            f"cast('abcdefghijklmnop' as varchar(16)) as c"
        )
        return (
            relation_from_name(adapter=project.adapter, name=self.TARGET),
            relation_from_name(adapter=project.adapter, name=self.STAGE),
        )

    def test_primary_key_skipped_while_the_ordinary_column_is_widened(
        self, project, relations, widen_notes
    ):
        target, stage = relations
        project.adapter.expand_target_column_types(from_relation=stage, to_relation=target)

        shapes = {col.column: col.dtype for col in project.adapter.get_columns_in_relation(target)}
        assert shapes["c"] == "varchar(16)", shapes  # widened for real
        assert shapes["id"] == "varchar(10)", shapes  # primary key untouched
        assert [
            note
            for level, note in widen_notes["notes"]
            if level == "warning" and "primary key" in note
        ], widen_notes["notes"]

    def test_partition_column_skipped_while_the_data_column_is_widened(self, project, widen_notes):
        name, stage = "widen_guard_partitioned", "widen_guard_partitioned_stage"
        project.run_sql(
            f"create table {project.test_schema}.{name} "
            f"(id bigint, c varchar(10)) partitioned by (ds varchar(10))"
        )
        project.run_sql(
            f"create table {project.test_schema}.{stage} as select "
            f"cast(1 as bigint) as id, cast('abcdefghijklmnop' as varchar(16)) as c, "
            f"cast('p1' as string) as ds"
        )
        target = relation_from_name(adapter=project.adapter, name=name)
        source = relation_from_name(adapter=project.adapter, name=stage)

        project.adapter.expand_target_column_types(from_relation=source, to_relation=target)

        shapes = {col.column: col.dtype for col in project.adapter.get_columns_in_relation(target)}
        assert shapes["c"] == "varchar(16)", shapes
        assert shapes["ds"] == "varchar(10)", shapes  # partition key untouched
        assert [
            note
            for level, note in widen_notes["notes"]
            if level == "warning" and "partition column" in note
        ], widen_notes["notes"]
        project.run_sql(f"drop table if exists {project.test_schema}.{name}")
        project.run_sql(f"drop table if exists {project.test_schema}.{stage}")


class TestTheUnderlyingServerFacts:
    """The measurements that made this a bug rather than a dialect limitation."""

    def test_a_longer_value_still_truncates_on_insert(self, project):
        """Why the guard has to run before the merge, not instead of it."""
        name = "truncate_probe"
        project.run_sql(f"create table {project.test_schema}.{name} (c varchar(10))")
        project.run_sql(
            f"insert into table {project.test_schema}.{name} values ('abcdefghijklmnop')"
        )
        assert lengths(project, name) == [10]  # green run, no complaint, 6 characters gone
        project.run_sql(f"drop table if exists {project.test_schema}.{name}")

    def test_shrinking_a_column_is_the_server_s_job_not_ours(self, project):
        name = "narrow_probe"
        project.run_sql(f"create table {project.test_schema}.{name} (id bigint, c string)")
        project.run_sql(
            f"insert into table {project.test_schema}.{name} "
            f"select cast(1 as bigint), 'abcdefghijklmnop'"
        )
        with pytest.raises(Exception) as excinfo:
            project.run_sql(
                f"alter table {project.test_schema}.{name} change column c c varchar(10)"
            )
        assert "is not supported" in str(excinfo.value)
        project.run_sql(f"drop table if exists {project.test_schema}.{name}")
