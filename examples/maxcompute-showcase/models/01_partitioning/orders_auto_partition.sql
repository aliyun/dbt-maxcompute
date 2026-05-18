{{
    config(
        materialized='table',
        partition_by={
            'fields': 'order_ts',
            'data_types': 'timestamp',
            'granularity': 'day'
        }
    )
}}

-- Auto-partitioned table. Because `data_types` is a temporal type
-- (timestamp / date / datetime / timestamp_ntz), MaxCompute derives the
-- partition value from the named source column on insert. You don't write
-- a synthetic `ds` column yourself and you don't need a PARTITION clause.
select
    order_id,
    customer_id,
    country,
    amount,
    status,
    order_ts
from {{ source('raw', 'orders') }}
