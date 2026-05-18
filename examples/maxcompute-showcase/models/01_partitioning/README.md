# 01_partitioning

How `partition_by` works in `dbt-maxcompute`. The adapter accepts a
dictionary with `fields` and `data_types`, both comma-separated strings:

```yaml
config:
  partition_by:
    fields: "col_a, col_b"
    data_types: "string, bigint"
```

The combination of `fields` and `data_types` decides the partition style:

| `data_types`                                  | partition style                                   |
| --------------------------------------------- | ------------------------------------------------- |
| (omitted) — defaults to `string`              | static / regular partitioned table                |
| `date` / `datetime` / `timestamp` / `timestamp_ntz` | **auto-partition** — partition value derived server-side from a data column |
| anything else (`bigint`, `string`, ...)       | static / regular partitioned table                |

For auto-partition only a single field is permitted. The partition column
is materialised by MaxCompute from the source column; you keep that source
column in your SELECT and do **not** add the partition column yourself.

## Files

- `orders_static_partition.sql` — single-column string partition (`country`).
  The classic "one partition per business slice" pattern.
- `orders_auto_partition.sql` — auto-partition on `order_ts` with daily
  granularity. MaxCompute derives `ds` (or your `generate_column_name`)
  from `order_ts` at insert time.
- `orders_multi_partition.sql` — two static partition columns
  (`country`, `order_date`). Useful when both are queried independently
  enough to justify two levels of pruning.
