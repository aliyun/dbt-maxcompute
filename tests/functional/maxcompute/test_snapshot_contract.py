"""Snapshot support contract, measured against a live MaxCompute project.

Two questions, one file:

1. Which ``strategy`` x target-table-type combinations actually work?  Each
   case reads the snapshot table back from the server after every ``dbt
   snapshot``, so assertions are about stored rows - is ``dbt_valid_to`` closed
   out, is there a new current version - and not about what dbt printed.
2. When a combination is not supported, does the run stop with a message a user
   can act on, or does it fail inside ``merge into`` with an opaque server
   error, or quietly ignore what was asked for?

Assertions are written as *deltas* (before -> after) rather than absolute row
counts, so a case does not depend on which case ran before it.  The
``SERVER[...]`` lines each case prints are the measurements behind
``docs/snapshot-support.md``.
"""

import os

import pytest
from dbt.tests.util import run_dbt


# Five records: ``updated_at`` advances so the timestamp strategy can tell
# versions apart; ``name``/``amount`` change so the check strategy can too.
SEED_CSV = """id,name,amount,updated_at
1,Alice,100,2024-01-01 00:00:00
2,Bob,200,2024-01-02 00:00:00
3,Charlie,300,2024-01-03 00:00:00
4,Dan,400,2024-01-04 00:00:00
5,Eve,500,2024-01-05 00:00:00
""".lstrip()

# UPDATE/DELETE on the *source* needs a transactional table;
# TestSnapshotPlainSource covers a source that cannot be mutated.
SCHEMA_YML = """version: 2
seeds:
  - name: base
    config:
      column_types:
        updated_at: "timestamp"
models:
  - name: fact
    config:
      transactional: true
""".lstrip()

MODEL_FACT_SQL = """{{ config(materialized="table") }}
select * from {{ ref('base') }}
""".lstrip()

SNAPSHOT_TIMESTAMP_SQL = """{% snapshot snap_ts %}
{{ config(target_schema=schema, unique_key="id", strategy="timestamp",
          updated_at="updated_at") }}
select * from {{ ref('fact') }}
{% endsnapshot %}
""".lstrip()

SNAPSHOT_CHECK_SQL = """{% snapshot snap_check %}
{{ config(target_schema=schema, unique_key="id", strategy="check",
          check_cols=["name", "amount"]) }}
select * from {{ ref('fact') }}
{% endsnapshot %}
""".lstrip()

META_COLUMNS = """
       cast(null as string) as dbt_scd_id,
       cast(null as timestamp) as dbt_updated_at,
       cast(null as timestamp) as dbt_valid_from,
       cast(null as timestamp) as dbt_valid_to"""


# --------------------------------------------------------------------------- #
# reading the server back
# --------------------------------------------------------------------------- #
def _counts(project, snapshot_name, valid_to="dbt_valid_to", current_expr=None):
    """(total, current, closed_out) versions as the server sees them.

    `current_expr` is what makes a row the live version: `is null` by default, or
    a sentinel comparison when the snapshot sets `dbt_valid_to_current`.
    """
    is_current = f"{valid_to} {current_expr or 'is null'}"
    row = project.run_sql(
        f"""
        select count(*),
               sum(case when {is_current} then 1 else 0 end),
               sum(case when not ({is_current}) then 1 else 0 end)
        from {snapshot_name}
        """,
        fetch="one",
    )
    return int(row[0]), int(row[1]), int(row[2])


def _delta(before, after):
    return tuple(a - b for a, b in zip(after, before))


def _ids(project, snapshot_name, current=True, valid_to="dbt_valid_to", current_expr=None):
    is_current = f"{valid_to} {current_expr or 'is null'}"
    predicate = is_current if current else f"not ({is_current})"
    rows = project.run_sql(
        f"select id from {snapshot_name} where {predicate} order by id",
        fetch="all",
    )
    return sorted(int(r[0]) for r in rows)


def _table(project, table_name):
    odps = project.adapter.get_odps_client()
    tbl = odps.get_table(table_name, schema=project.test_schema)
    tbl.reload()
    return tbl


def _table_shape(project, table_name):
    """What the created table actually is, read back from the server."""
    tbl = _table(project, table_name)
    return {
        "transactional": bool(tbl.is_transactional),
        "partitioned": bool(tbl.table_schema.partitions),
        "lifecycle": getattr(tbl, "lifecycle", None),
        "columns": [c.name for c in tbl.table_schema.columns],
    }


def _typed_columns(project, table_name):
    return [(c.name, str(c.type)) for c in _table(project, table_name).table_schema.columns]


def _node_status(node):
    status = getattr(node, "status", None)
    return str(getattr(status, "value", status)).lower()


def _try_snapshot(name):
    """(succeeded, message, events) for one ``dbt snapshot --select <name>``.

    ``expect_pass=None`` keeps dbt's per-node result: a rejected snapshot is a
    node with status ``error`` carrying the adapter's message, while an
    invocation that aborts before any node runs surfaces as an exception.
    """
    events = []
    try:
        results = run_dbt(
            ["snapshot", "--select", name],
            expect_pass=None,
            callbacks=[events.append],
        )
    except BaseException as exc:  # noqa: BLE001 - the message is the measurement
        return False, str(exc), events
    nodes = list(getattr(results, "results", None) or [])
    bad = [n for n in nodes if _node_status(n) != "success"]
    if not bad:
        return True, "", events
    return False, " | ".join(str(getattr(n, "message", "") or "") for n in bad), events


def _snapshot(project, name):
    ok, detail, _ = _try_snapshot(name)
    assert ok, f"dbt snapshot {name} failed: {detail}"


def _warned_text(events):
    return " ".join(str(getattr(e, "info", e)) for e in events).lower()


def _write_snapshot_file(project, name, strategy_config=""):
    """Write ``snapshots/<name>.sql``, snapshotting ``fact`` with extra config.

    For the cases that need a snapshot the base fixtures do not carry.  Built by
    concatenation on purpose: the Jinja braces are not f-string material, and an
    f-string that eats one of them costs a whole live-project run to notice.
    """
    directory = os.path.join(project.project_root, "snapshots")
    os.makedirs(directory, exist_ok=True)
    config = (
        "{{ config(target_schema=schema, unique_key='id', strategy='timestamp', "
        "updated_at='updated_at'" + (", " + strategy_config if strategy_config else "") + ") }}"
    )
    body = "\n".join(
        [
            "{% snapshot " + name + " %}",
            config,
            "select * from {{ ref('fact') }}",
            "{% endsnapshot %}",
            "",
        ]
    )
    path = os.path.join(directory, name + ".sql")
    with open(path, "w") as handle:
        handle.write(body)
    return path


class BaseSnapshotCase:
    @pytest.fixture(scope="class")
    def seeds(self):
        return {"base.csv": SEED_CSV, "schema.yml": SCHEMA_YML}

    @pytest.fixture(scope="class")
    def models(self):
        return {"fact.sql": MODEL_FACT_SQL}

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {"snap_ts.sql": SNAPSHOT_TIMESTAMP_SQL, "snap_check.sql": SNAPSHOT_CHECK_SQL}

    @pytest.fixture(scope="class", autouse=True)
    def _prepare(self, project):
        run_dbt(["seed"])
        run_dbt(["run"])
        yield


class TestSnapshotTimestampStrategy(BaseSnapshotCase):
    """strategy=timestamp, target created by the adapter."""

    def test_first_run_creates_a_transactional_unpartitioned_target(self, project):
        _snapshot(project, "snap_ts")
        total, current, closed = _counts(project, "snap_ts")
        shape = _table_shape(project, "snap_ts")
        print(
            f"SERVER[timestamp.first] total={total} current={current} "
            f"closed={closed} target={shape}"
        )
        assert (total, current, closed) == (5, 5, 0)
        assert _ids(project, "snap_ts") == [1, 2, 3, 4, 5]
        # Closing out an expired version is a merge, and a merge only runs on a
        # transactional table; a partitioned target would not be what the merge
        # macro writes.
        assert shape["transactional"] is True
        assert shape["partitioned"] is False

    def test_rerun_without_changes_adds_nothing(self, project):
        _snapshot(project, "snap_ts")
        before = _counts(project, "snap_ts")
        _snapshot(project, "snap_ts")
        after = _counts(project, "snap_ts")
        print(f"SERVER[timestamp.noop] before={before} after={after}")
        assert _delta(before, after) == (0, 0, 0)

    def test_update_expires_the_old_version_and_keeps_the_row_current(self, project):
        _snapshot(project, "snap_ts")
        before = _counts(project, "snap_ts")
        project.run_sql(
            "update fact set amount = 999, "
            "updated_at = CAST('2024-02-01 00:00:00' AS TIMESTAMP) "
            "where id in (1, 2)"
        )
        _snapshot(project, "snap_ts")
        after = _counts(project, "snap_ts")
        print(
            f"SERVER[timestamp.update] delta={_delta(before, after)} "
            f"closed_ids={_ids(project, 'snap_ts', False)}"
        )
        # Two expired versions, the same five ids still current.
        assert _delta(before, after) == (2, 0, 2)
        old_amount = project.run_sql(
            "select amount from snap_ts where id = 1 and dbt_valid_to is not null",
            fetch="one",
        )[0]
        assert float(old_amount) == 100

    def test_hard_delete_needs_the_dbt_option_and_then_expires_the_record(self, project):
        """Measured: without ``hard_deletes`` a vanished row stays current.

        ``SERVER[timestamp.delete] delta=(0, 0, 0)`` - dbt-core does not look for
        deleted rows unless ``hard_deletes`` says what to do.  With
        ``hard_deletes="invalidate"`` MaxCompute expires the version it has and
        adds none, which is what the rest of this case asserts.
        """
        name = "snap_ts_hd"
        directory = os.path.join(project.project_root, "snapshots")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, f"{name}.sql"), "w") as handle:
            handle.write(
                "{% snapshot " + name + " %}\n"
                "{{ config(target_schema=schema, unique_key='id', "
                "strategy='timestamp', updated_at='updated_at', "
                "hard_deletes='invalidate') }}\n"
                "select * from {{ ref('fact') }}\n"
                "{% endsnapshot %}\n"
            )
        _snapshot(project, name)
        before = _counts(project, name)
        project.run_sql("delete from fact where id in (3, 4)")
        _snapshot(project, name)
        after = _counts(project, name)
        print(
            f"SERVER[timestamp.hard_delete_invalidate] delta={_delta(before, after)} "
            f"current_ids={_ids(project, name)}"
        )
        assert _delta(before, after) == (0, -2, 2)
        assert _ids(project, name) == [1, 2, 5]
        # Nothing to add back: the expired version is the whole story.
        assert _counts(project, name)[0] == before[0]

    def test_reinserting_a_key_with_the_same_updated_at_reopens_nothing(self, project):
        """Measured: the version identity is ``md5(unique_key, updated_at)``.

        Putting id 3 back into the source with its original ``updated_at`` gives the
        same ``dbt_scd_id`` as the version that was just expired.  The merge matches
        that expired row, its ``dbt_valid_to is null`` guard fails, and the
        not-matched insert branch never fires - nothing is written
        (``SERVER[timestamp.revive_same_updated_at] delta=(0, 0, 0)``).

        Upstream's own revive case cannot catch this: its ``_assert_results`` only
        checks that actual records are members of the expected set, so "dbt wrote
        nothing" passes there vacuously.
        """
        name = "snap_ts_hd"
        _snapshot(project, name)
        before = _counts(project, name)
        project.run_sql(
            "insert into fact select * from base where id = 3 "
            "and not exists (select 1 from fact where fact.id = 3)"
        )
        _snapshot(project, name)
        after = _counts(project, name)
        print(
            f"SERVER[timestamp.revive_same_updated_at] delta={_delta(before, after)} "
            f"current_ids={_ids(project, name)}"
        )
        assert _delta(before, after) == (0, 0, 0)

    def test_reinserting_a_key_with_a_newer_updated_at_reopens_it(self, project):
        """The other half of the same contract: change the timestamp and the revived
        key gets a fresh current version."""
        name = "snap_ts_hd"
        before = _counts(project, name)
        project.run_sql(
            "update fact set updated_at = CAST('2024-05-01 00:00:00' AS TIMESTAMP) " "where id = 3"
        )
        _snapshot(project, name)
        after = _counts(project, name)
        print(
            f"SERVER[timestamp.revive_newer_updated_at] delta={_delta(before, after)} "
            f"current_ids={_ids(project, name)}"
        )
        assert _delta(before, after) == (1, 1, 0)
        assert 3 in _ids(project, name)


class TestSnapshotCheckStrategy(BaseSnapshotCase):
    """strategy=check with an explicit column list."""

    def test_first_run(self, project):
        _snapshot(project, "snap_check")
        counts = _counts(project, "snap_check")
        shape = _table_shape(project, "snap_check")
        print(f"SERVER[check.first] counts={counts} target={shape}")
        assert counts == (5, 5, 0)
        assert shape["transactional"] is True

    def test_change_in_a_checked_column_creates_a_version(self, project):
        _snapshot(project, "snap_check")
        before = _counts(project, "snap_check")
        project.run_sql("update fact set amount = 111 where id = 1")
        _snapshot(project, "snap_check")
        after = _counts(project, "snap_check")
        print(
            f"SERVER[check.tracked_column] delta={_delta(before, after)} "
            f"closed_ids={_ids(project, 'snap_check', False)}"
        )
        assert _delta(before, after) == (1, 0, 1)

    def test_change_outside_the_checked_columns_creates_nothing(self, project):
        _snapshot(project, "snap_check")
        before = _counts(project, "snap_check")
        project.run_sql(
            "update fact set updated_at = CAST('2025-01-01 00:00:00' AS TIMESTAMP) " "where id = 2"
        )
        _snapshot(project, "snap_check")
        after = _counts(project, "snap_check")
        print(f"SERVER[check.untracked_column] delta={_delta(before, after)}")
        assert _delta(before, after) == (0, 0, 0)

    def test_null_versus_empty_string_is_a_change(self, project):
        """Measured on MaxCompute: NULL and '' are two states, not one.

        ``change detection`` for ``check`` compares column values with explicit
        NULL handling, so ``name: 'Dan' -> NULL -> '' -> 'Dan'`` produced one
        new version per step (``SERVER[check.null_vs_empty]
        null=(7, 5, 2) empty=(8, 5, 3) restored=(9, 5, 4)``).  The md5 hash that
        coalesces NULL to '' only builds ``dbt_scd_id`` (the version identity),
        it is not what decides whether a row changed.
        """
        _snapshot(project, "snap_check")
        project.run_sql("update fact set name = null where id = 4")
        _snapshot(project, "snap_check")
        after_null = _counts(project, "snap_check")
        project.run_sql("update fact set name = '' where id = 4")
        _snapshot(project, "snap_check")
        after_empty = _counts(project, "snap_check")
        project.run_sql("update fact set name = 'Dan' where id = 4")
        _snapshot(project, "snap_check")
        after_restore = _counts(project, "snap_check")
        print(
            "SERVER[check.null_vs_empty] "
            f"null={after_null} empty={after_empty} restored={after_restore}"
        )
        assert _delta(after_null, after_empty) == (1, 0, 1)
        assert _delta(after_empty, after_restore) == (1, 0, 1)

    def test_hard_delete_is_not_captured_without_the_option(self, project):
        """Same contract as the timestamp strategy: dbt ignores vanished rows
        unless ``hard_deletes`` is set, on MaxCompute as elsewhere."""
        _snapshot(project, "snap_check")
        before = _counts(project, "snap_check")
        project.run_sql("delete from fact where id in (3, 4)")
        _snapshot(project, "snap_check")
        after = _counts(project, "snap_check")
        print(f"SERVER[check.delete_no_option] delta={_delta(before, after)}")
        assert _delta(before, after) == (0, 0, 0)


class TestSnapshotCheckAllColumns(BaseSnapshotCase):
    """check_cols='all': every column takes part in the hash."""

    SNAPSHOT_CHECK_ALL_SQL = SNAPSHOT_CHECK_SQL.replace(
        'check_cols=["name", "amount"]', 'check_cols="all"'
    ).replace("snap_check", "snap_all")

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {"snap_all.sql": self.SNAPSHOT_CHECK_ALL_SQL}

    def test_a_column_outside_the_other_check_list_still_counts(self, project):
        _snapshot(project, "snap_all")
        before = _counts(project, "snap_all")
        project.run_sql(
            "update fact set updated_at = CAST('2025-01-01 00:00:00' AS TIMESTAMP) " "where id = 1"
        )
        _snapshot(project, "snap_all")
        after = _counts(project, "snap_all")
        print(f"SERVER[check_all] before={before} delta={_delta(before, after)}")
        assert before == (5, 5, 0)
        assert _delta(before, after) == (1, 0, 1)


class TestSnapshotAppend2Target(BaseSnapshotCase):
    """Append Delta (``table.format.version=2``) as the snapshot table."""

    SNAPSHOT_APPEND2_SQL = SNAPSHOT_TIMESTAMP_SQL.replace("snap_ts", "snap_a2")

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {
            "snap_a2.sql": self.SNAPSHOT_APPEND2_SQL.replace(
                'updated_at="updated_at")',
                'updated_at="updated_at", tblproperties={"table.format.version": "2"})',
            )
        }

    def test_first_update_and_delete(self, project):
        _snapshot(project, "snap_a2")
        shape = _table_shape(project, "snap_a2")
        first = _counts(project, "snap_a2")
        print(f"SERVER[append2.first] counts={first} target={shape}")
        assert first == (5, 5, 0)
        assert shape["transactional"] is True
        project.run_sql(
            "update fact set amount = 555, "
            "updated_at = CAST('2024-04-01 00:00:00' AS TIMESTAMP) where id = 1"
        )
        _snapshot(project, "snap_a2")
        after_update = _counts(project, "snap_a2")
        print(f"SERVER[append2] first={first} update_delta={_delta(first, after_update)}")
        assert _delta(first, after_update) == (1, 0, 1)


class TestSnapshotPlainSource:
    """A plain (non-transactional) source table - a table someone else owns."""

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "plain_fact.sql": (
                "{{ config(materialized='table') }}\n"
                "select 1 as id, 'a' as name, cast(10 as double) as amount, "
                "cast('2024-01-01 00:00:00' as timestamp) as updated_at\n"
                "union all\n"
                "select 2, 'b', 20, cast('2024-01-02 00:00:00' as timestamp)\n"
            ),
        }

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {"snap_ts.sql": SNAPSHOT_TIMESTAMP_SQL.replace("ref('fact')", "ref('plain_fact')")}

    def test_snapshot_and_row_disappearing_from_the_source(self, project):
        run_dbt(["run"])
        shape = _table_shape(project, "plain_fact")
        print(f"SERVER[plain_source.source_table] {shape}")
        assert shape["transactional"] is False
        _snapshot(project, "snap_ts")
        first = _counts(project, "snap_ts")
        print(f"SERVER[plain_source.first] counts={first}")
        assert first == (2, 2, 0)
        project.run_sql("insert overwrite table plain_fact select * from plain_fact where id = 1")
        _snapshot(project, "snap_ts")
        after = _counts(project, "snap_ts")
        print(f"SERVER[plain_source.rows_removed_no_option] delta={_delta(first, after)}")
        assert _delta(first, after) == (0, 0, 0)


class TestSnapshotPreexistingPlainTarget(BaseSnapshotCase):
    """The snapshot table already exists as a plain (non-transactional) table."""

    def test_is_rejected_before_the_merge(self, project):
        project.run_sql(
            "create table snap_ts as select id, name, amount, updated_at,"
            + META_COLUMNS
            + " from fact where 1 = 2"
        )
        shape = _table_shape(project, "snap_ts")
        print(f"SERVER[target.plain_table] {shape}")
        assert shape["transactional"] is False
        ok, message, _ = _try_snapshot("snap_ts")
        print(f"SERVER[target.plain_table.outcome] succeeded={ok} message={message[:400]}")
        assert not ok, "a plain table cannot hold snapshot history; the run must not pass"
        lowered = message.lower()
        assert "transactional" in lowered, (
            "the message has to name the missing table property rather than "
            "leave the user with a server error"
        )
        assert "odps-" not in lowered, f"opaque server error leaked to the user: {message[:200]}"


class TestSnapshotPreexistingDeltaTarget(BaseSnapshotCase):
    """The snapshot table already exists as a PK Delta table (unique key = pk)."""

    def test_history_is_kept_or_the_run_says_it_cannot(self, project):
        _snapshot(project, "snap_ts")
        typed = _typed_columns(project, "snap_ts")
        project.run_sql("drop table snap_ts")
        columns = ", ".join(
            f"`{name}` {typ}" + (" not null" if name == "id" else "") for name, typ in typed
        )
        project.run_sql(
            f"create table snap_ts ({columns}, primary key(id)) "
            'tblproperties("transactional"="true", "write.bucket.num"="16")'
        )
        shape = _table_shape(project, "snap_ts")
        print(f"SERVER[target.delta_pk] columns={typed} shape={shape}")
        assert shape["transactional"] is True
        # Measured before the adapter checked for keys: the first snapshot into an
        # empty PK Delta table succeeded (5, 5, 0) and the *next* one reported
        # success too while leaving id 1 with a single, expired version - the
        # record lost its current version in silence.  The contract is now to
        # refuse the target as soon as it is looked at.
        ok, message, _ = _try_snapshot("snap_ts")
        counts = _counts(project, "snap_ts") if ok else None
        print(
            f"SERVER[target.delta_pk.first] succeeded={ok} counts={counts} "
            f"message={message[:300]}"
        )
        assert not ok, (
            "a primary key cannot hold the expired and the current version of one "
            f"unique key; the run passed anyway: counts={counts}"
        )
        assert "primary key" in message.lower(), message[:300]
        assert "odps-" not in message.lower(), f"opaque server error leaked: {message[:200]}"


class TestSnapshotHardDeleteModes(BaseSnapshotCase):
    """The other two ``hard_deletes`` modes, both strategies.

    ``new_record`` does not expire the old version: it adds a version marked in
    ``dbt_is_deleted`` (the column is there from the first run, because dbt builds it as
    part of the snapshot query).  To place those values dbt-core calls the **dispatched
    macro** ``get_columns_in_relation`` - not the adapter method - and this adapter had no
    implementation, so every ``new_record`` snapshot died with
    ``get_columns_in_relation macro not implemented for adapter maxcompute`` before
    reaching the server.  Measured before and after; that macro is the reason this option
    works now.
    """

    def test_timestamp_new_record_marks_the_deleted_row(self, project):
        _write_snapshot_file(project, "snap_hd_nr", "hard_deletes='new_record'")
        _snapshot(project, "snap_hd_nr")
        shape = _table_shape(project, "snap_hd_nr")
        print(f"SERVER[hard_deletes.new_record.table] {shape}")
        assert "dbt_is_deleted" in shape["columns"]
        before = _counts(project, "snap_hd_nr")
        project.run_sql("delete from fact where id in (3, 4)")
        ok, message, _ = _try_snapshot("snap_hd_nr")
        after = _counts(project, "snap_hd_nr") if ok else before
        deleted_rows = project.run_sql(
            "select count(*) from snap_hd_nr where dbt_is_deleted = 'True'", fetch="one"
        )[0]
        print(
            f"SERVER[hard_deletes.new_record] succeeded={ok} "
            f"delta={_delta(before, after)} marked_deleted={deleted_rows} "
            f"message={message[:300]}"
        )
        assert ok, f"hard_deletes='new_record' failed: {message[:300]}"
        assert _delta(before, after) == (2, 0, 2)
        assert int(deleted_rows) == 2

    def test_check_strategy_with_invalidate(self, project):
        directory = os.path.join(project.project_root, "snapshots")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "snap_check_hd.sql"), "w") as handle:
            handle.write(
                "{% snapshot snap_check_hd %}\n"
                "{{ config(target_schema=schema, unique_key='id', "
                "strategy='check', check_cols=['name', 'amount'], "
                "hard_deletes='invalidate') }}\n"
                "select * from {{ ref('fact') }}\n"
                "{% endsnapshot %}\n"
            )
        _snapshot(project, "snap_check_hd")
        before = _counts(project, "snap_check_hd")
        project.run_sql("delete from fact where id = 5")
        _snapshot(project, "snap_check_hd")
        after = _counts(project, "snap_check_hd")
        print(f"SERVER[check.hard_deletes_invalidate] delta={_delta(before, after)}")
        # Measured on both strategies: `invalidate` expires the current version of
        # a vanished key and adds no row (timestamp: (0, -2, 2), check: (0, -1, 1)).
        assert _delta(before, after) == (0, -1, 1)


class TestSnapshotConfigKeysAreNotSilentlyDropped(BaseSnapshotCase):
    """Config keys the snapshot materialization cannot honour must be said out loud."""

    @pytest.mark.parametrize(
        "name,key",
        [
            ("snap_part", "partition_by='dt string'"),
            ("snap_life", "lifecycle=30"),
            ("snap_nontx", "transactional=false"),
            ("snap_pk", "primary_keys=['id']"),
        ],
    )
    def test_unsupported_key_is_reported_and_the_table_stays_supported(self, project, name, key):
        _write_snapshot_file(project, name, key)
        ok, message, events = _try_snapshot(name)
        shape = _table_shape(project, name) if ok else {}
        warned = "does not apply" in _warned_text(events)
        print(
            f"SERVER[config.{name}] succeeded={ok} warned={warned} table={shape} "
            f"message={message[:200]}"
        )
        assert ok, f"{key} should not break a snapshot that otherwise works: {message[:200]}"
        assert warned, f"{key} was dropped without telling the user: {shape}"
        # Whatever the key asked for, the table dbt created is still one that
        # can hold history.
        assert shape["transactional"] is True
        assert shape["partitioned"] is False
        if name == "snap_part":
            assert "partition" in _warned_text(events)
        if name == "snap_life":
            assert shape["lifecycle"] != 30, (
                "lifecycle on a snapshot table would expire history; if the "
                "server now applies it, the docs and this case both change"
            )


class TestSnapshotRenamedMetaColumns(BaseSnapshotCase):
    """`snapshot_meta_column_names` renames the four dbt columns; everything
    downstream (staging, merge, the target checks) has to follow the names."""

    SNAPSHOT_RENAMED_SQL = (
        "{% snapshot snap_renamed %}\n"
        "{{ config(target_schema=schema, unique_key='id', strategy='timestamp', "
        "updated_at='updated_at', snapshot_meta_column_names="
        "{'dbt_scd_id': 'ver_id', 'dbt_updated_at': 'ver_updated_at', "
        "'dbt_valid_from': 'ver_from', 'dbt_valid_to': 'ver_to'}) }}\n"
        "select * from {{ ref('fact') }}\n"
        "{% endsnapshot %}\n"
    )

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {"snap_renamed.sql": self.SNAPSHOT_RENAMED_SQL}

    def test_history_is_still_correct_under_other_names(self, project):
        _snapshot(project, "snap_renamed")
        shape = _table_shape(project, "snap_renamed")
        first = _counts(project, "snap_renamed", valid_to="ver_to")
        print(f"SERVER[renamed.first] counts={first} target={shape}")
        assert first == (5, 5, 0)
        assert {"ver_id", "ver_to", "ver_from", "ver_updated_at"} <= set(shape["columns"])
        assert "dbt_scd_id" not in shape["columns"]

        project.run_sql(
            "update fact set amount = 321, "
            "updated_at = CAST('2024-06-01 00:00:00' AS TIMESTAMP) where id = 2"
        )
        _snapshot(project, "snap_renamed")
        after = _counts(project, "snap_renamed", valid_to="ver_to")
        print(
            f"SERVER[renamed.update] delta={_delta(first, after)} "
            f"expired_ids={_ids(project, 'snap_renamed', False, valid_to='ver_to')}"
        )
        assert _delta(first, after) == (1, 0, 1)


class TestSnapshotDbtValidToCurrent(BaseSnapshotCase):
    """`dbt_valid_to_current`: the live version is a sentinel, not NULL.

    dbt-core handles that in two places - it reads the live rows with
    ``valid_to = <sentinel> or valid_to is null``, and it does the same in
    ``default__snapshot_merge_sql``'s matched branch.  The MaxCompute merge macro
    is an override, so the sentinel only works if the override carries the branch
    too; without it nothing is ever matched and the expired version stays live
    next to the new one.
    """

    # `to_timestamp('...')` is not valid MaxCompute (the server wants 2-3 args), so the
    # sentinel is written as an explicit cast.
    SENTINEL = "cast('9999-12-31 00:00:00' as timestamp)"

    SNAPSHOT_VTC_SQL = """{% snapshot snap_vtc %}
{{ config(target_schema=schema, unique_key='id', strategy='timestamp', updated_at='updated_at', dbt_valid_to_current="cast('9999-12-31 00:00:00' as timestamp)") }}
select * from {{ ref('fact') }}
{% endsnapshot %}
"""

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {"snap_vtc.sql": self.SNAPSHOT_VTC_SQL}

    def test_one_current_version_per_key_with_a_sentinel(self, project):
        _snapshot(project, "snap_vtc")
        current = f"= {self.SENTINEL}"
        first = _counts(project, "snap_vtc", current_expr=current)
        print(f"SERVER[valid_to_current.first] counts={first}")
        assert (
            first[0] == 5 and first[1] == 5
        ), f"the first run should mark all five rows current by the sentinel: {first}"

        project.run_sql(
            "update fact set amount = 654, "
            "updated_at = CAST('2024-07-01 00:00:00' AS TIMESTAMP) where id = 1"
        )
        _snapshot(project, "snap_vtc")
        after = _counts(project, "snap_vtc", current_expr=current)
        current_ids_for_one = project.run_sql(
            f"select count(*) from snap_vtc where id = 1 and dbt_valid_to = {self.SENTINEL}",
            fetch="one",
        )[0]
        print(
            f"SERVER[valid_to_current.update] delta={_delta(first, after)} "
            f"current_versions_of_id_1={current_ids_for_one} counts={after}"
        )
        assert int(current_ids_for_one) == 1, (
            f"id 1 has {current_ids_for_one} current versions: the merge did not "
            "recognise the sentinel, so the expired version was never closed out"
        )
        assert _delta(first, after) == (1, 0, 1)

    def test_second_update_and_rerun_keep_one_current_version(self, project):
        """The sentinel branch has to stay correct over repeated runs.

        A second key changing, then a no-op run: each key keeps exactly one live
        version, and rows already expired with a real timestamp are not treated
        as live (which is what a `= <sentinel>` check that forgot the NULL case
        would break).
        """
        current = f"= {self.SENTINEL}"
        before = _counts(project, "snap_vtc", current_expr=current)
        project.run_sql(
            "update fact set amount = 666, "
            "updated_at = CAST('2024-07-02 00:00:00' AS TIMESTAMP) where id = 2"
        )
        _snapshot(project, "snap_vtc")
        after_update = _counts(project, "snap_vtc", current_expr=current)
        _snapshot(project, "snap_vtc")
        after_rerun = _counts(project, "snap_vtc", current_expr=current)
        print(
            f"SERVER[valid_to_current.second] from={before} after_update={after_update} "
            f"after_noop_rerun={after_rerun}"
        )
        assert _delta(before, after_update) == (1, 0, 1)
        assert _delta(after_update, after_rerun) == (0, 0, 0)
        duplicated = project.run_sql(
            f"""
            select id, count(*) from snap_vtc
            where dbt_valid_to = {self.SENTINEL}
            group by id having count(*) > 1
            """,
            fetch="all",
        )
        assert duplicated == [], f"keys left with two live versions: {duplicated}"


class TestSnapshotCompositeUniqueKey(BaseSnapshotCase):
    """A list ``unique_key``, and what does and does not count as a change to it.

    Measured with the staging helper columns filtered out (see the materialization):
    the second run stops failing with ``dbt_unique_key_1 is ambiguous`` and the
    snapshot table never grows helper columns.  Two semantics are pinned here, both
    from the server's own rows:

    * a non-key column change + newer ``updated_at`` expires the old version;
    * a change **to a key column itself** is a different record - the old version
      stays current (``delta (1, 1, 0)``), because the expiry join is on the key.
      Snapshot history keys on identity, not on similarity.
    """

    SNAPSHOT_MULTIKEY_SQL = (
        "{% snapshot snap_mk %}\n"
        "{{ config(target_schema=schema, unique_key=['id', 'name'], "
        "strategy='timestamp', updated_at='updated_at') }}\n"
        "select * from {{ ref('fact') }}\n"
        "{% endsnapshot %}\n"
    )

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {"snap_mk.sql": self.SNAPSHOT_MULTIKEY_SQL}

    def _helper_columns(self, project):
        return [c for c in _table_shape(project, "snap_mk")["columns"] if "unique_key" in c]

    def test_helper_columns_never_become_snapshot_columns(self, project):
        _snapshot(project, "snap_mk")
        first = _counts(project, "snap_mk")
        print(
            f"SERVER[composite_key.first] counts={first} helper_cols={self._helper_columns(project)}"
        )
        assert first == (5, 5, 0)
        assert self._helper_columns(project) == []

    def test_non_key_change_expires_the_old_version(self, project):
        project.run_sql(
            "update fact set amount = 777, "
            "updated_at = CAST('2024-08-01 00:00:00' AS TIMESTAMP) where id = 1"
        )
        ok, message, _ = _try_snapshot("snap_mk")
        after = _counts(project, "snap_mk") if ok else _counts(project, "snap_mk")
        print(
            f"SERVER[composite_key.non_key_change] succeeded={ok} counts={after} "
            f"helper_cols={self._helper_columns(project)} msg={message[:200]}"
        )
        assert ok, f"a list unique_key must run: {message[:200]}"
        assert (
            after[0] == 6 and after[2] == 1
        ), f"expected one expired version next to a new current one, got {after}"
        assert self._helper_columns(project) == []

    def test_changing_a_key_column_makes_a_new_record_not_a_new_version(self, project):
        before = _counts(project, "snap_mk")
        project.run_sql(
            "update fact set name = 'Alice2', "
            "updated_at = CAST('2024-08-02 00:00:00' AS TIMESTAMP) where id = 1"
        )
        _snapshot(project, "snap_mk")
        after = _counts(project, "snap_mk")
        print(
            f"SERVER[composite_key.key_column_change] delta={_delta(before, after)} counts={after}"
        )
        assert _delta(before, after) == (1, 1, 0), (
            "the key itself changed, so dbt treats it as a different record; the "
            "previous version stays current - this is the documented cost of putting "
            "a mutable column into unique_key"
        )

    def test_composite_key_as_one_expression_is_the_usable_form(self, project):
        """The workaround dbt documents for composite keys, measured here.

        A list `unique_key` makes dbt-core emit `dbt_unique_key_1/2` columns that
        MaxCompute's analyser calls ambiguous (measured:
        ``ODPS-0130071 ... dbt_unique_key_1 is ambiguous``).  Spelling the key as
        one expression keeps a single `dbt_unique_key` and works.
        """
        body = (
            "{% snapshot snap_mk_expr %}\n"
            "{{ config(target_schema=schema, unique_key=\"concat(id, '|', name)\", "
            "strategy='timestamp', updated_at='updated_at') }}\n"
            "select * from {{ ref('fact') }}\n"
            "{% endsnapshot %}\n"
        )
        directory = os.path.join(project.project_root, "snapshots")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "snap_mk_expr.sql"), "w") as handle:
            handle.write(body)
        _snapshot(project, "snap_mk_expr")
        first = _counts(project, "snap_mk_expr")
        print(f"SERVER[multi_key_expr.first] counts={first}")
        assert first == (5, 5, 0)
        project.run_sql(
            "update fact set updated_at = CAST('2024-09-01 00:00:00' AS TIMESTAMP) where id = 4"
        )
        _snapshot(project, "snap_mk_expr")
        after = _counts(project, "snap_mk_expr")
        print(f"SERVER[multi_key_expr.update] delta={_delta(first, after)}")
        assert _delta(first, after) == (1, 0, 1)

    def test_a_table_polluted_by_an_older_run_is_refused_with_the_remedy(self, project):
        """The pre-fix materialization could leave helper columns behind.

        A table holding them can never be snapshotted again - the staging query
        aliases the same names over `select *`, and MaxCompute answers with ten
        ambiguous-column errors.  Say what to delete instead.
        """
        project.run_sql("alter table snap_mk add columns (dbt_unique_key_9 string)")
        ok, message, _ = _try_snapshot("snap_mk")
        print(f"SERVER[composite_key.leftover_guard] succeeded={ok} message={message[:300]}")
        assert not ok, "a target with leftover staging columns must not run"
        lowered = message.lower()
        assert "dbt_unique_key_9" in lowered, message[:200]
        assert "drop columns" in lowered, message[:200]
        assert "odps-" not in lowered, f"opaque server error leaked: {message[:200]}"


class TestSnapshotSwitchToNewRecordOnExistingTable(BaseSnapshotCase):
    """Turning on ``hard_deletes='new_record'`` for a table that has no
    ``dbt_is_deleted`` column yet.

    dbt-core's ``assert_valid_snapshot_target_given_strategy`` refuses this with a
    "not a snapshot table" error naming the missing column.  This adapter's
    materialization still calls the older ``valid_snapshot_target``, so the case is
    decided here by measurement rather than by copying core: the materialization adds
    missing columns from the staging query, which may make the switch work instead.
    """

    def _write(self, project, extra_config):
        directory = os.path.join(project.project_root, "snapshots")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "snap_switch.sql"), "w") as handle:
            handle.write(
                "{% snapshot snap_switch %}\n"
                "{{ config(target_schema=schema, unique_key='id', strategy='timestamp', "
                "updated_at='updated_at'"
                + (", " + extra_config if extra_config else "")
                + ") }}\n"
                "select * from {{ ref('fact') }}\n"
                "{% endsnapshot %}\n"
            )

    def test_the_switch_keeps_history_or_says_why_not(self, project):
        self._write(project, "")
        _snapshot(project, "snap_switch")
        before = _counts(project, "snap_switch")
        columns_before = _table_shape(project, "snap_switch")["columns"]
        print(f"SERVER[switch_new_record.before] counts={before} columns={columns_before}")
        assert "dbt_is_deleted" not in columns_before

        self._write(project, "hard_deletes='new_record'")
        ok, message, _ = _try_snapshot("snap_switch")
        columns_after = _table_shape(project, "snap_switch")["columns"]
        after = _counts(project, "snap_switch") if ok else before
        print(
            f"SERVER[switch_new_record.after] succeeded={ok} delta={_delta(before, after)} "
            f"has_dbt_is_deleted={'dbt_is_deleted' in columns_after} message={message[:250]}"
        )
        # Measured with the older validator, this failed *after* submitting: six lines
        # of `ODPS-0130071 ... column snapshotted_data.dbt_is_deleted cannot be
        # resolved`.  dbt-core's strategy-aware validator refuses the same situation by
        # name, so that is the contract asserted here: no server round-trip, no ODPS code.
        assert (
            not ok and "dbt_is_deleted" not in columns_after
        ), f"unexpected outcome: ok={ok} columns={columns_after}"
        lowered = message.lower()
        assert (
            "dbt_is_deleted" in lowered
        ), f"the message must name the missing column: {message[:200]}"
        assert "odps-" not in lowered, f"opaque server error leaked to the user: {message[:200]}"


class TestSnapshotStringAsTimeMacro:
    """``snapshot_string_as_time`` is dispatchable but nothing in dbt-core calls it,

    so a wrong implementation sits there until a user writes a custom strategy that
    needs it.  Measured on MaxCompute: the one-argument ``to_timestamp('...')`` form is
    not valid SQL here (``ODPS-0130221 ... function to_timestamp needs at least 2, at
    most 3 parameters, actually have 1``), so the macro has to render something the
    server accepts, for both a full timestamp string and a date-only one.
    """

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "ts_full.sql": (
                'select {{ snapshot_string_as_time("2024-01-01 00:00:00") }} as ts, ' "1 as id\n"
            ),
            "ts_date.sql": ('select {{ snapshot_string_as_time("2024-01-01") }} as ts, 1 as id\n'),
        }

    def test_renders_sql_the_server_accepts(self, project):
        results = run_dbt(["run"], expect_pass=None)
        failures = [r for r in results.results if _node_status(r) != "success"]
        for r in results.results:
            value = None
            if _node_status(r) == "success":
                name = r.node.name
                value = project.run_sql(f"select ts from {name}", fetch="one")[0]
            print(
                f"SERVER[snapshot_string_as_time.{r.node.name}] status={_node_status(r)} value={value}"
            )
        assert not failures, f"the macro produced unusable SQL: {failures[0].message[:300]}"


class TestSnapshotTablePropertiesReachTheServer(BaseSnapshotCase):
    """`tblproperties` has to survive into the created snapshot table.

    Measured on this project: MaxCompute refuses an unknown table property at
    parse time (``create table ... tblproperties("totally.bogus.prop"="1")`` →
    ``ODPS-0130071/ParseError``), so a snapshot carrying a bogus key is an
    observable probe.  If the materialization ever stops passing the config
    through - which is exactly what a cleanup edit in this repository did once,
    deleting the ``{%- set tblproperties = config.get(...) -%}`` line while the
    two statements below kept using it - the run would succeed instead of
    failing, and the user's table properties would be dropped in silence.
    """

    def test_bogus_property_fails_because_the_server_sees_it(self, project):
        _write_snapshot_file(project, "snap_prop", "tblproperties={'totally.bogus.prop': '1'}")
        ok, message, _ = _try_snapshot("snap_prop")
        print(f"SERVER[tblproperties.reach_server] succeeded={ok} message={message[:220]}")
        assert not ok, (
            "the snapshot was created even though the server rejects that table "
            "property: the config never reached the DDL"
        )

    def test_valid_append2_property_still_builds_a_working_snapshot(self, project):
        _write_snapshot_file(project, "snap_a2ok", "tblproperties={'table.format.version': '2'}")
        _snapshot(project, "snap_a2ok")
        counts = _counts(project, "snap_a2ok")
        shape = _table_shape(project, "snap_a2ok")
        print(
            f"SERVER[tblproperties.append2_ok] counts={counts} transactional={shape['transactional']}"
        )
        assert counts == (5, 5, 0)
        assert shape["transactional"] is True
