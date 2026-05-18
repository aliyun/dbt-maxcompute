{{
    config(
        materialized='table',
        partition_by={
            'fields': 'country',
            'data_types': 'string'
        }
    )
}}

-- A regular (static) partitioned table: one partition per distinct country.
-- The partition column appears in the SELECT just like a normal column;
-- the adapter recognises it and emits `PARTITION (country)` for you.
select
    order_id,
    customer_id,
    amount,
    status,
    order_ts,
    country
from {{ source('raw', 'orders') }}
