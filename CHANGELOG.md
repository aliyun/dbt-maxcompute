# Changelog

All notable changes to `dbt-maxcompute` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.11.4] — 2026-10-19

### Changed

- `truncate_insert` now supports full-refresh through the existing incremental
  rebuild path, allowing schema changes that require recreating the target.
  Normal runs and models configured with `full_refresh=false` still preserve
  the table object. Full-refresh replaces the object and does not guarantee
  preservation of its Label grants.

## [1.11.3] — 2026-10-15

### Added

- MaxFrame Python table/incremental models and Python scalar/aggregate UDFs
  graduate to Generally Available within their documented supported scope.
  Existing limitations, including remote MaxFrame DAG cancellation, remain.
- Opt-in `truncate_insert` incremental strategy replaces all rows in a
  non-partitioned target with one `INSERT OVERWRITE`, preserving the target
  table object. Existing strategies and their full-refresh behavior are unchanged.
  Existing project/package macro overrides keep their own validation and behavior.
- SQL integration regression entry point: `scripts/run-integration-tests.sh`
  runs a minimal real-SQL set (table, view, incremental, dbt tests, persisted
  docs) plus an invalid-SQL case that must fail, and verifies that every test
  schema it created is dropped again.
- `integration.yml` workflow with a credential gate: a run that cannot reach a
  MaxCompute project is reported as not run or blocked, never as a passing
  integration. Fork pull requests, which cannot read repository secrets, show a
  skipped integration job.
- `docs/integration-tests.md` covers local and CI runs, including the
  three-tier (schema-enabled) project requirement and `auth_type: chain` for
  keeping access keys out of files.
- New `catalog_strict_metadata` profile option (default `false`). When enabled,
  `dbt docs generate` fails if a relation listed for the catalog stays unreadable
  after the metadata retries; dbt-core records the failure in `catalog.json`
  `errors`, making the gap visible to consumers of the artifact.

### Upgrade notes

- SQL continues to default `odps.sql.python.version` to `cp311`, as in the
  `1.11.3` beta releases. Upgrading from a beta preserves the runtime default
  for both dbt function references and functions called by raw SQL name.
- When upgrading from `1.11.2`, which did not set this hint globally, models
  calling legacy CPython 2.7 or 3.7 UDFs must explicitly set
  `sql_hints={'odps.sql.python.version': 'cp27'}` or `cp37`, respectively.
  Hooks that invoke a legacy UDF must include the corresponding `SET` in
  their own SQL statement. A single statement cannot mix Python runtimes.

### Fixed

- Incremental materializations now execute hooks configured with
  `transaction: false`, rather than silently skipping them.
- Incremental column widening preserves MaxCompute column type details and
  validates the widening plan before changing the target schema.
- Snapshots validate that an existing target can retain history before
  submitting a merge, and preserve the snapshot key columns.
- The optional `[maxframe]` installation now constrains PyArrow below 26 to match
  MaxFrame 2.x's NumPy 1 requirement. PyArrow 26 could install successfully but fail
  to import, causing `dbt seed` to report that PyArrow was missing. CI now checks
  the built wheel with the extra installed and initializes the PyODPS Arrow writer.
- Functional tests without credentials are now reported as *skipped* with the
  reason, instead of failing with a missing-file error inside the profile
  fixture.
- `dbt docs generate` no longer drops unreadable relations from `catalog.json`
  without naming them. Each skipped relation is now listed in a single warning
  that explains the gap and how to opt into strict mode.
- `get_odps_table_by_relation` retries now also cover `NoSuchObject` raised by the
  metadata lookup itself, not only by reloading the returned object.


- Microbatch models whose event-time column is a `datetime` can now run on the
  adapter's default profile. dbt-core renders the batch window from a UTC
  `datetime`, so the text carries an offset (`event_time >= '2025-05-01
  00:00:00+00:00'`); MaxCompute compares that against `timestamp` columns but
  refuses it for `datetime` ones, whose string form is `yyyy-mm-dd hh:mi:ss`
  exactly, so every batch failed while the physical plan was generated. The
  boundary is now stated without the offset suffix - and only where the session
  clock reads UTC, so the window denotes the same instant. Sessions pinned to
  another zone are untouched, and microseconds are kept rather than rounded:
  a `datetime` column rejects fractional seconds too, so a `--sample` window
  against one still fails to compile instead of silently changing the window.
  See `docs/microbatch-support.md`.

- Microbatch models that declare their event column as a `date` partition field are now refused
  at compile time instead of running empty. dbt-core compares each batch against a timestamp-shaped
  boundary (`event_time >= '2025-05-01 00:00:00'` since #40, with an offset before it), and
  MaxCompute evaluates a `DATE` against either text as `NULL` rather than raising: every batch
  selected zero rows, the `insert overwrite` of an empty query changed nothing, and `dbt run`
  reported the model as a success with the target left empty. Measured on a real project - four
  source rows across two days, `PASS=2`, zero rows - in `Etc/GMT`, `Asia/Shanghai` and `Etc/GMT+8`
  sessions alike, so no session clock can line the two up. Rendering a date-only boundary would fix
  that comparison but cannot be done where the boundary is written: that point is handed a field
  name, not a column type, and the date-only text is measured to be refused outright by a `datetime`
  column and to evaluate to `NULL` for `timestamp` and `timestamp_ntz`, i.e. it would empty or break
  the models that work today. The error names the replacement, which is measured end to end: expose
  the event column as an instant *in the relation the window is applied to* (a `cast()` inside the
  microbatch model's own SELECT is too late) and partition on it as `timestamp`.
  See `docs/microbatch-support.md`.

## [1.11.3b3] — 2026-08-26

### Fixed

- MaxFrame Python models now support dynamic credential providers, including
  `auth_type: chain`, without requiring a global `CredentialProviderAccount`
  monkey patch. Nested PyODPS option contexts reuse the provider's existing
  thread-safe refresh state instead of attempting to copy its internal lock.

## [1.11.3b2] — 2026-08-11

### Added

- **MaxFrame Python models (Production Preview)** with table and incremental
  materializations, MaxCompute partitions, five incremental strategies,
  microbatch windows, schema evolution, isolated sessions, empty-output-safe
  staging, and bounded retry handling for transient DAG transport failures.
- **Python scalar and aggregate UDFs (Beta)** using dbt `functions:` resources,
  MaxCompute CPython 3.11 by default, generated `@annotate` signatures,
  content-addressed PY resources, safe in-place updates, managed-resource
  cleanup, and user-supplied dependency resources.

### Changed

- SQL submitted by the adapter now defaults
  `odps.sql.python.version` to `cp311`. Legacy CPython 3.7 UDF calls must
  explicitly override the session hint to `cp37`.
- Python function resources can inherit a project-wide
  `functions: +runtime_version: "3.11"` default, avoiding repeated CP311
  configuration while satisfying dbt Core's function contract.
- The README now reports capability-level maturity instead of the obsolete
  repository-wide Alpha label.
- The MaxFrame guide now documents pandas-style boolean row filtering and the
  `DataFrame.id` name collision for users migrating from other DataFrame APIs.
- MaxFrame custom-function models now recommend a Python 3.11 client while
  leaving SDK installation and submission enabled on other adapter-supported
  Python versions. Controlled targets can opt into a strict submission check.
- Successful PythonPack builds use the production cache by default. Large
  scientific dependencies can use a managed MaxCompute runtime image instead
  of rebuilding native packages for each environment.

### Fixed

- Partition columns are included when a MaxFrame Python model reads a
  partitioned `ref` or `source`.
- Session-scoped `tmp_mf_*` tables and `mf_udf_*` functions are cleaned for
  every session created by a node, including failed sessions replaced during
  retry.
- Transient transport, throttling, timeout, and HTTP 5xx failures retry the
  compiled model in a new MaxFrame session with bounded attempts.

### Known limitations

- MaxFrame cancellation is not yet propagated to an active remote DAG.
- Python UDFs do not yet expose SQL UDFs, UDTFs, overloads, grants, default
  arguments, or dynamic PyPI installation.

## [1.11.2] — 2026-06-03

### Added

- **MaxQA (MCQA V2) execution mode** — new profile-level `execution_mode: maxqa`
  routes SQL through MaxCompute's interactive query acceleration engine,
  delivering sub-second to single-digit-second latency for eligible
  workloads. Supports optional `quota_name`, server-side fallback
  (`maxqa_fallback`, default on), and fallback to a specific offline quota
  (`maxqa_fallback_quota`). Per-model override via `sql_hints`:
  `{'dbt.execution_mode': 'maxqa'}` / `{'dbt.execution_mode': 'offline'}`.
  (`d35ab13`)
- **MaxQA example models** in `examples/maxcompute-showcase/models/05_maxqa/`
  — table, view, incremental, and force-offline demonstrations. (`1d4d96c`)

### Fixed

- **Ephemeral test assertions updated** for dbt-core 1.11.7's switch from
  `CREATE VIEW` to `CREATE OR REPLACE VIEW`. (`f352841`)

## [1.11.1] — 2026-05-18

Patch release that resolves a cluster of correctness bugs across the
`incremental`, `materialized_view`, `right(...)`, and `hash(...)` paths
discovered in a code review of the 1.11.0 GA. All fixes ship with a
functional regression test and are non-breaking.

### Fixed

#### Incremental materialization

- **`merge` / `delete+insert` on non-auto partitioned targets now include
  the partition column** — previously the SELECT excluded the partition
  field, producing a column-count mismatch on insert. Auto-partitioned
  targets are unaffected. (`1d5d6e0`)
- **`append` strategy emits an explicit `PARTITION (...)` clause** on
  non-auto partitioned targets and drops the partition column from the
  data column list. Without this, every `dbt run` failed with a column
  count mismatch the moment a partitioned table was the target.
  (`4cd381c`)
- **`delete+insert` with a list-shaped `unique_key` is rewritten to
  `WHERE (k1, k2, ...) IN (SELECT k1, k2, ... FROM src)`** instead of the
  Postgres `DELETE ... USING <src>` form, which MaxCompute SQL does not
  support. Single-column `unique_key` paths are unchanged. (`e4a5f3c`)
- **`insert_overwrite` accepts multi-column `partition_by`** — the
  previous implementation hard-coded a single partition field. Removed
  dead `include_sql_header` plumbing in the same macro that was never
  read. (`5837d76`)
- **Temp relations are dropped after `merge` / `delete+insert` / `append`
  runs.** Previously the helper view created during incremental runs
  persisted in the schema indefinitely, accumulating one stale
  `<model>__dbt_tmp_*` per run. (`f708822`)

#### Materialized views

- **Real configuration-change detection for materialized views.** The
  configuration-change macro returned an empty Jinja string (truthy, not
  `None`), which short-circuited dbt-core's REFRESH branch and forced a
  full `DROP + CREATE` on every run — even when nothing in the config had
  changed. Detection is now delegated to
  `adapter.materialized_view_config_changes`, which compares the current
  `lifecycle`, `table_comment`, `disable_rewrite`, and `partition_by`
  against the live table read via PyODPS and returns `None` when they
  match. Result: identical-config re-runs take the cheap
  `ALTER MATERIALIZED VIEW ... REBUILD` path. (`409f228`)

  Scope: changes to `columns`, `column_comment`, `tblProperties`, or
  `build_deferred` still require `--full-refresh` because PyODPS does not
  expose those fields reliably post-create — same trade-off as
  `dbt-postgres` and `dbt-redshift`.

#### SQL macros

- **`hash(NULL)` now equals `md5('')` instead of `NULL`.** The macro used
  `coalesce(... = NULL, '')`, but `<expr> = NULL` is itself `NULL` in
  SQL, so a `NULL` input fell through and the cast to string yielded
  `NULL`. Replaced with `IS NULL` so the literal-empty branch actually
  fires. (`6cc9a1f`)
- **`right(string, length_expression)` returns the last
  `length_expression` characters.** Previous implementation passed
  `length(string) - 1` as the substring length, returning everything
  except the first character regardless of the requested length.
  (`b27a680`)

### Added

- **`examples/maxcompute-showcase/`** — a runnable reference dbt project
  demonstrating MaxCompute-specific features (partitioning variants, all
  five incremental strategies, materialized views, lifecycle, delta
  tables, and snapshots). Aimed at users who already know dbt and are
  new to MaxCompute. Lives in source only; excluded from the published
  wheel and sdist. (`a2a8c58`)

### Tested

Each fix above has a paired functional regression test under
`tests/functional/maxcompute/`. Full pre-release run against a live
MaxCompute project:

- 15 unit tests — passed
- 17 fix-specific functional regressions — passed
- `tests/functional/adapter/test_basic.py` (dbt-adapter base e2e suite) —
  11 passed, 4 pre-existing skips (flaky `BaseAdapterMethod`,
  `BaseDocsGenerate` × 2, `BaseDocsGenReferences`)

## [1.11.0] — 2026-04-02

Initial GA on the dbt-core 1.11 line. See git history for details.
