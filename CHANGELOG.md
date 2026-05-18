# Changelog

All notable changes to `dbt-maxcompute` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
