# 02_incremental

`dbt-maxcompute` supports all five dbt incremental strategies. Which one to
pick depends on the shape of your data and what your upstream guarantees.

## Decision sketch

```
Do you partition on time and rebuild whole partitions per run?
├─ Yes → insert_overwrite  (or microbatch for time-windowed runs)
└─ No
    Do you need to update existing rows by key?
    ├─ Yes, and target supports MERGE → merge
    ├─ Yes, but want simpler SQL      → delete+insert
    └─ No, append-only fact table     → append
```

## Files

| Model                              | Strategy            | When you'd reach for it |
| ---------------------------------- | ------------------- | ----------------------- |
| `orders_insert_overwrite.sql`      | `insert_overwrite`  | Date-partitioned table; each run rebuilds a fixed set of partitions. The default and the one to start with. |
| `orders_merge.sql`                 | `merge`             | Rows mutate in place and you can identify them by `unique_key`. Single statement. |
| `orders_delete_insert.sql`         | `delete+insert`     | Same goal as merge, but expressed as two statements. Sometimes a better fit when your warehouse cost model favours scans over MERGE. |
| `orders_append.sql`                | `append`            | Append-only event stream. Just adds new rows; no key matching, no overwrite. |
| `orders_microbatch.sql`            | `microbatch`        | Backfill / catch-up runs over a time range, one partition per batch. Beta in dbt-core. |

## Notes specific to MaxCompute

- `insert_overwrite` always operates on whole partitions. If your model is
  unpartitioned the strategy degrades to `TRUNCATE + INSERT` of the whole
  table — usually not what you want.
- `delete+insert` with a list-shaped `unique_key` is rewritten by the
  adapter to a `WHERE (k1, k2) IN (SELECT k1, k2 FROM source)` form
  because MaxCompute's `DELETE` does not support a `USING` clause.
- `merge` on a non-auto-partitioned table will, by default, exclude the
  partition columns from the `UPDATE SET` list. Moving a row across
  partitions is rarely intended and forces extra dynamic-partition work.
  Override with an explicit `merge_update_columns`.
- `append` against a non-auto partitioned target emits an explicit
  `INSERT INTO ... PARTITION (...)` clause for you.
- `microbatch` requires `partition_by` and the `partition_by.granularity`
  must equal `batch_size` — the adapter validates this and raises a clear
  compiler error if you mismatch them.
