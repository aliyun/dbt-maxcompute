# 03_materialized_view

MaxCompute materialized views are persisted, refreshable tables maintained
by the engine. `dbt-maxcompute` materialises them via
`CREATE MATERIALIZED VIEW`, and on subsequent `dbt run` invocations decides
between `REBUILD` (cheap, server-side refresh) and `DROP + CREATE`
(expensive, full re-materialisation) by diffing the config.

## What the adapter compares

When you re-run an existing MV, the adapter reads the live table back via
PyODPS and compares these fields against your model config:

| Config              | Detected? | Notes                                              |
| ------------------- | --------- | -------------------------------------------------- |
| `lifecycle`         | ✓         | Read from `Table.lifecycle`.                       |
| `table_comment`     | ✓         | Read from `Table.comment`.                         |
| `disable_rewrite`   | ✓         | Read from `Table.is_materialized_view_rewrite_enabled`. |
| `partition_by`      | ✓         | Compared by partition field names.                 |
| `columns`           | ✗         | Re-run with `--full-refresh`.                      |
| `column_comment`    | ✗         | Re-run with `--full-refresh`.                      |
| `tblProperties`     | ✗         | Re-run with `--full-refresh`.                      |
| `build_deferred`    | ✗         | MaxCompute does not expose it post-create.         |

If none of the *detected* fields changed, the MV is refreshed in place via
`ALTER MATERIALIZED VIEW ... REBUILD`. Otherwise it is replaced.

> **Why this matters.** Before this was wired up, the configuration-change
> macro returned an empty Jinja string (which is truthy, not None), which
> short-circuited dbt-core's refresh branch and forced a full DROP+CREATE
> on every run. Same data, lots of churn.

## Files

- `orders_daily_mv.sql` — a daily aggregation materialised view, with
  `lifecycle` and a single-column partition, so you can verify the
  REBUILD path: run it twice in a row and the table's `creation_time`
  should not move.
