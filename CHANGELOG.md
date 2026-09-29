# Changelog

All notable changes to `dbt-maxcompute` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

### Fixed

- Functional tests without credentials are now reported as *skipped* with the
  reason, instead of failing with a missing-file error inside the profile
  fixture.

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
