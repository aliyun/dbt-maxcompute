# 04_operations

Production-shaped knobs that don't fit in the partitioning or incremental
buckets: lifecycle and transactional / delta tables.

## Files

- `orders_with_lifecycle.sql` — explicit `lifecycle: 30`. The whole project
  defaults to `lifecycle: 1` (to make this showcase self-cleaning), so this
  model exists to demonstrate the override.
- `orders_transactional.sql` — a **transactional + primary-key** table,
  which MaxCompute treats as a *delta table*. The adapter recognises
  `transactional: true` together with `primary_keys: [...]` and emits the
  right CREATE syntax (including the `write.bucket.num` tblproperty).
  Delta tables are what unlock low-cost upserts and time-travel queries.

## What's not here

- `cluster_by` — accepted by the adapter today but not emitted into the
  CREATE TABLE statement. Don't put it in production models until that
  changes.
- Resource group / quota hints — set via `hints` in your profile or via
  `pre_hook`, not via model config.
