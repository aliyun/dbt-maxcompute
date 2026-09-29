{{
    config(
        materialized='incremental',
        incremental_strategy='microbatch',
        unique_key='order_id',
        event_time='order_ts',
        begin='2025-05-01',
        batch_size='day',
        partition_by={
            'fields': 'order_ts',
            'data_types': 'timestamp',
            'granularity': 'day'
        }
    )
}}

-- ⚠️  `microbatch` is still a preview feature in dbt-core. The contract is:
--   - target MUST be partitioned
--   - `partition_by.granularity` MUST equal `batch_size`
--   - `unique_key` is not used by the write path: a batch is a partition
--     overwrite, so repeated keys inside one window are all kept. Use `merge`
--     or `delete+insert` when rows must be updated by key
--   - leave `timezone` out of the profile: the batch window is read on the
--     session clock while partitions are cut on UTC days, so a non-UTC session
--     timezone makes adjacent windows overwrite each other
--
-- Each batch overwrites one partition (here, one day of `order_ts`). Re-run
-- with `dbt run --event-time-start 2025-05-03 --event-time-end 2025-05-05`
-- to backfill a range without touching unaffected days.
-- See docs/microbatch-support.md for what has been measured.
select
    order_id,
    customer_id,
    country,
    amount,
    status,
    order_ts
from {{ source('raw', 'orders') }}
