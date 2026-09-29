# Snapshot support on MaxCompute

Which snapshot combinations work on MaxCompute, and what the adapter does when one does
not.  Every "measured" statement below came out of a run of
`tests/functional/maxcompute/test_snapshot_contract.py` against a live three-tier
project (dbt-core 1.11.2, pyodps 0.13.2), where each case reads the snapshot table back
from the server and reports `(total rows, current versions, expired versions)`.  What was
not measured is listed under *Not verified* instead of being implied.

## What a snapshot needs from the warehouse

dbt keeps snapshot history in two steps: build a staging table of the changes, then run
one `merge into` against the snapshot table to expire the versions whose data changed and
insert the new current versions.  On MaxCompute that `merge into` only runs on a
**transactional** table, and the expired version of a key has to sit next to its current
version.  So:

* the snapshot table dbt creates is always created `TBLPROPERTIES("transactional"="true")`,
  unpartitioned and without a primary key - measured on the first run;
* a snapshot table that already exists is checked against those two requirements before
  anything is built, including the staging table (`TestSnapshotPreexistingPlainTarget`,
  `TestSnapshotPreexistingDeltaTarget`);
* the *source* does not have to be transactional - a snapshot only reads from it
  (`TestSnapshotPlainSource` measured a plain source table).

## Strategies

| strategy | what marks a new version | measured |
| --- | --- | --- |
| `timestamp` | `dbt_valid_from < updated_at` for the column named in `updated_at` | update of 2 keys -> `+2` versions, 2 expired, all keys still current; re-run with no change adds nothing |
| `check` | value difference over `check_cols` (or `"all"`) | a tracked column -> 1 new version; an untracked column -> nothing; `check_cols="all"` notices a column the narrow list ignores |

Two details worth knowing, both measured rather than assumed:

* **NULL and empty string are two different values for `check`.** Taking one checked
  column `'Dan' -> NULL -> '' -> 'Dan'` produced a new version at *every* step.  The md5
  that builds `dbt_scd_id` does coalesce NULL to `''`, but that hash only *names* a
  version; it is not what decides whether a row changed.
* **The version identity is `md5(unique_key, updated_at)`.** Re-inserting a key with the
  *same* `updated_at` after it was expired writes nothing: the new row's `dbt_scd_id`
  equals the expired row's, the merge's matched branch requires `dbt_valid_to is null`,
  and the not-matched branch no longer fires.  Give the revived row a newer
  `updated_at` and it becomes current again (measured: `+1` version, `+1` current).

### Hard deletes are opt-in, in dbt - not in this adapter

A row that disappears from the source is ignored unless the snapshot sets `hard_deletes`:

| `hard_deletes` | measured on MaxCompute |
| --- | --- |
| unset | nothing: the last version stays current forever (asserted so nobody "fixes" this by accident) |
| `'invalidate'` | the current version gets `dbt_valid_to` and no row is added; works for both strategies (`timestamp` `0,-2,+2`; `check` `0,-1,+1`), and a key that comes back with a newer `updated_at` becomes current again |
| `'new_record'` | a new version marked `'True'` in `dbt_is_deleted` is added (`+2` versions, 2 marked deleted).  This option needs the **dispatched macro** `get_columns_in_relation`, which had no MaxCompute implementation: every `new_record` snapshot died with `get_columns_in_relation macro not implemented for adapter maxcompute` before reaching the server.  The macro is now here, delegating to the adapter's existing pyodps column reader |

`hard_deletes` also accepts the older `invalidate_hard_deletes=True` spelling (what the
upstream adapter tests use); that alias is what the cases in
`tests/functional/adapter/test_simple_snapshot.py` rely on.

## dbt snapshot options that used to be unverified

Three options had never been run against MaxCompute.  All three are measured now, and two
of them needed a fix.

| option | measured result | what had to change |
| --- | --- | --- |
| `snapshot_meta_column_names` (renaming `dbt_scd_id`, `dbt_valid_to`, ...) | works: the snapshot table holds only the renamed columns, no `dbt_*` leftovers; after updating one key, `+1` version and `+1` expired row with the same number of current rows | nothing |
| `dbt_valid_to_current` (a sentinel marks the live version instead of NULL) | **was silently wrong**: after an update, the changed key had two live versions and nothing was closed out | the merge macro here is an override, and it hard-coded `dbt_valid_to is null` in its matched branch.  It now mirrors dbt-core's `(dest.valid_to = <sentinel> or dest.valid_to is null)` |
| `unique_key` as a list (composite key) | **used to die on the second run** with ten `column reference ... dbt_unique_key_1 is ambiguous` errors, after the first run had looked fine | the staging query's helper columns were filtered by exact name only, so `dbt_unique_key_1/2` got `alter table ... add columns`-ed into the snapshot table, and the next run aliased those names a second time over `select *`.  The filter now matches the name shape |

A snapshot table that an **older** adapter version already polluted with those helper
columns cannot be repaired by upgrading: every later run would keep failing with the same
ambiguity.  Such a target is now refused up front, naming the columns to remove
(`alter table ... drop columns`) - covered by
`TestSnapshotCompositeUniqueKey::test_a_table_polluted_by_an_older_run_is_refused_with_the_remedy`.

One composite-key behaviour is worth stating because it surprises people: changing a
**key column itself** is a new record, not a new version - the previous version stays
current (`+1` total, `+1` current), because the expiry join is on the key.  Putting a
mutable column into `unique_key` opts into that.
`TestSnapshotCompositeUniqueKey` pins all three numbers.

### `snapshot_string_as_time` rendered SQL MaxCompute rejects

Nothing in dbt-core calls this macro, so a broken implementation is invisible until a custom
snapshot strategy uses it - which is exactly how it stayed broken here.  It rendered
`to_timestamp('<value>')`, and MaxCompute has no one-argument form:

```
ODPS-0130221 ... Invalid number of arguments - function to_timestamp needs at least 2, at most 3 parameters, actually have 1
```

It now renders an explicit cast (`cast('<value>' as timestamp)`, with a date-only value
padded to midnight, because the cast wants a full timestamp).  Measured both ways through a
model that calls the macro: before, both forms errored with the message above
(`raw/macro-probe-100933.sanitized.log`, round ended `1 failed, 4 warnings in 12.19s`);
after, `SERVER[snapshot_string_as_time.ts_full] status=success value=2024-01-01 00:00:00`
and the date-only form the same (`raw/stime-fix2-101214.sanitized.log`, round ended
`3 passed in 252.65s` - that round also re-ran the `dbt_valid_to_current` cases, still green).

Note the difference with the dbt-core default, which this page otherwise mirrors: where a
value is *written* into the snapshot table, dbt uses the adapter's own rendering, so the cast
form above is what a MaxCompute snapshot table stores.

## Target table types

| snapshot table | measured result |
| --- | --- |
| created by dbt (transactional, no key) | supported: first run, re-run, update, hard delete with `invalidate`/`new_record`, revive |
| Append Delta - `tblproperties={'table.format.version': '2'}` | supported: table comes out transactional, first run and update behave as above |
| pre-existing **plain** (non-transactional) table | refused before anything is built: `Snapshot target ... is a non-transactional MaxCompute table ... recreate it with TBLPROPERTIES("transactional"="true")`.  Before this check the same run failed later, inside the merge, with `ODPS-0130071 ... merge into target table must be transactional table` - after a staging table had already been created |
| pre-existing **PK Delta** table (primary key on the unique key) | refused: `Snapshot target ... has a primary key (id) ... the record ends up with no current version`.  Before the check this combination *reported success*: the first snapshot filled the table (5 current), and after a source update the key had exactly one row left - the expired one - so the current version was silently gone |
| view / external table / metadata unreadable | **not** refused: the adapter cannot tell, so it lets the statement speak rather than guess |

Only a key over *data* columns is refused.  A key on the snapshot's own version id
(`dbt_scd_id`, or whatever `snapshot_table_column_names` renames it to) does not prevent two
versions of one business key from coexisting, so it is accepted - that distinction is pinned
by the offline cases in `tests/unit/test_snapshot_target_validation.py`.

## Config keys the snapshot materialization does not apply

`partition_by`, `primary_keys` / `delta`, `transactional=false` and `lifecycle` are
accepted by dbt's config system and then ignored, because the snapshot table has to be
unpartitioned, keyless, and kept for as long as the history is.  The run now warns and
still creates a table that can hold history - measured for all four: the snapshot
succeeded, the warning named the key, and the table stayed transactional, unpartitioned,
without a key; the server still reported no lifecycle (`-1`) for the snapshot that asked for `lifecycle=30`.

```
Snapshot 'snap_part' sets partition_by (a snapshot table is never partitioned: ...) ;
the MaxCompute snapshot materialization does not apply them.
```

Warn, not error: the history dbt writes is still correct, only the shape the key asked
for is not what you get, and a hard failure would break pipelines that work today.  The
two table-type cases above are errors because continuing there produces a wrong result.

## Not verified

* two-tier projects (no schema) - every measurement ran on a three-tier project;
* invalid or oversized `snapshot_meta_column_names` values, and the reject-path cases in
  dbt's own adapter test suite (this adapter does not implement those checks);
* snapshotting through a view, a materialized view, or a partitioned source where only some
  partitions change (an ephemeral *model* is now measured - see below);
* switching an existing snapshot table between `check` and `timestamp`;
* two `dbt snapshot` runs writing the same target at the same time;
* `dbt_is_deleted` cleanup behaviour over long histories (`new_record` combined with
  `check_cols='all'` is now measured - see below).

## Ephemeral models and `check_cols='all'` are measured

Four cases from dbt's own adapter suite (`tests/functional/adapter/test_ephemeral_snapshot_hard_deletes.py`)
snapshot an **ephemeral model** over a source table, which is the shape that had been listed as
unverified.  All four pass on a live project - `4 passed, 20 warnings in 318.25s` - covering
`check`/`check_cols='all'` with `hard_deletes='new_record'`, the `timestamp` strategy, a
`check_cols` list that deliberately excludes a newly added source column, and a new column plus a
hard delete in the same run.

Two of the fixtures had to be re-spelled, and both reasons were measured rather than taken from
documentation:

* the source table is created `transactional`, because one case deletes a row from it - a `delete`
  against a non-transactional table is refused with `ODPS-0130071 ... trying to delete from a
  non-transactional table is not allowed. Set tblproperties ("transactional" = "true") ...`, and
  the same statement against a transactional table works (2 of 3 rows left after deleting `id = 3`);
  `ODPS-0130161`, mentioned below, is a *parse* error and belongs to the `INTEGER` case;
* seed rows insert `cast('...' as timestamp)` - a bare string literal in a `timestamp` column is
  refused as an incompatible type.

Two things that look like dialect differences are not: `add column ... DEFAULT '<literal>'` parses
fine here (so upstream's own schema-change statement is kept verbatim), and `INTEGER` is simply not
a MaxCompute column type at all - `create table t (id INTEGER, ...)` fails on plain *and*
transactional tables, while `INT` and `BIGINT` both work.

## Why those gaps existed: the snapshot macros are a copy of dbt-core's

`dbt/include/maxcompute/macros/materializations/snapshots/snapshot.sql` starts with a
comment saying it is a copy of dbt-core's files ("only change varchar to string,
dbt-adapters/dbt/include/global_project/macros/materializations/snapshots/strategies.sql").
Both bugs above came from that copy having fallen behind core, not from MaxCompute:

* core's `default__snapshot_merge_sql` grew a `dbt_valid_to_current` branch in its matched
  condition; the copied override never got it.
* core's materialization builds its excluded-column list as
  `['dbt_change_type', 'DBT_CHANGE_TYPE', 'dbt_unique_key', 'DBT_UNIQUE_KEY']` **plus**
  `dbt_unique_key_<n>` for each element of a list `unique_key`; the copy kept only the four
  exact names, so the numbered columns leaked into the snapshot table.

One consequence of that drift is now fixed, and it is worth stating what it cost before:

* the materialization called `adapter.valid_snapshot_target(relation, columns)` while core calls
  `adapter.assert_valid_snapshot_target_given_strategy(relation, columns, strategy)`, which adds
  a strategy-specific check.  Turning on `hard_deletes='new_record'` for a snapshot table that
  has no `dbt_is_deleted` column therefore reached the server and came back with six copies of
  `ODPS-0130071 ... column snapshotted_data.dbt_is_deleted cannot be resolved`.  Calling core's
  entry point refuses the same situation before submitting anything, naming the column - and the
  adapter's own shape checks still run, because core's helper calls `valid_snapshot_target` first.

The copy also keeps `create_table_as_internal(..., True, ...)` and the explicit
`insert (...) values (...)` shape, which are genuinely MaxCompute-specific and stay.

The same comparison, done line by line, is also how a regression of our own making was caught:
a cleanup edit in this repository deleted the materialization's `grant_config` and
`tblproperties` assignments while leaving the statements that consume them.  Jinja reads an
undefined name as falsy, so nothing failed - snapshot `grants` and `tblproperties` would simply
have stopped being applied - and the live suite stayed green over that state.  Both assignments
are back, and two guards keep them there:

* `tests/unit/test_snapshot_macro_integrity.py` - the copied materialization must still assign
  every variable it uses; the test was checked by deleting a line and watching it fail;
* `TestSnapshotTablePropertiesReachTheServer` - behavioural: MaxCompute rejects an unknown table
  property at parse time, so a snapshot configured with a bogus property must fail the run
  (measured: `SERVER[tblproperties.reach_server] succeeded=False`), while
  `tblproperties={'table.format.version': '2'}` still builds a working snapshot
  (`counts=(5, 5, 0)`, transactional).  If the config were dropped, the first case would pass and
  the second would be unverifiable.

`grants:` on a snapshot is measured rather than assumed, and the two halves are worth keeping
apart.  The materialization does submit the statement - after the merge it runs `should_revoke`
and `apply_grants`, and a configured grant reaches the server as
`grant select on table <snapshot> to USER <grantee>`.  MaxCompute does record what it accepts:
granting `select` on a table to a user that exists in the project was accepted, and
`show grants on table` read that privilege back for that principal.  Not measured here: the
`revoke` half (when a grant config changes between runs), grants to roles rather than users, and
privileges other than `select`.

`tests/functional/adapter/test_grants.py::TestSnapshotGrants` fails against this test project for
a reason that is the fixture, not the product: it grants to the `DBT_TEST_USER_1` principal
hard-coded in that file; `list users` on this project returns six accounts and none of them is
that principal, so the server refuses the statement with
`InvalidParameter: the project does not support this account provider`.  So a red run of
that case says nothing about snapshot grants - and the caveat runs the other way too, because a
green suite is what let the dropped `grant_config` above slip through unnoticed.

The rest of the snapshot overrides were compared against the same dbt-core version and are
intentional, not drift: batched `alter table ... add columns (...)` in
`maxcompute__create_columns` (core adds one column per statement; MaxCompute's syntax is the
batched form), `maxcompute__post_snapshot` dropping the staging relation (there are no temp
tables to auto-drop), `md5` over `coalesce(cast(<col> as string), '')` in
`maxcompute__snapshot_hash_arguments` (core's `varchar` wording, same semantics here), and
forcing the transactional flag in `build_snapshot_staging_table`.  `expanded_data_type`, which
core uses in `create_columns`, is not overridden by this adapter, so it resolves to the same
string `data_type` produces - no hidden difference there.

## Reproduce

```bash
export DBT_PROFILE_PATH=/path/to/dbt_profile.yml   # type / project / schema / endpoint / auth
python -m pytest tests/functional/maxcompute/test_snapshot_contract.py -v -s
```

Each class gets its own schema and dbt drops it at teardown; the printed `SERVER[...]`
lines are the measurements.  With no profile or credentials the cases skip with a reason
rather than passing, so a run that reports nothing is visible as nothing.
