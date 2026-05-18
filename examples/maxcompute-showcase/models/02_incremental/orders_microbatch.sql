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
--   - `unique_key` is required by dbt-maxcompute (we raise a clear compiler
--     error if missing rather than silently doing the wrong thing)
--
-- Each batch overwrites one partition (here, one day of `order_ts`). Re-run
-- with `dbt run --event-time-start 2025-05-03 --event-time-end 2025-05-05`
-- to backfill a range without touching unaffected days.
select
    order_id,
    customer_id,
    country,
    amount,
    status,
    order_ts
from {{ source('raw', 'orders') }}
