{{
    config(
        materialized='table',
        lifecycle=30,
        table_comment='Curated orders fact, retained for 30 days.'
    )
}}

-- Explicit 30-day lifecycle override (project default is 1 day).
-- `LIFECYCLE n` causes MaxCompute to drop the table (or, on a partitioned
-- table, the partition) `n` days after last modification — this is the
-- main cost-control knob for the warehouse.
select
    order_id,
    customer_id,
    country,
    amount,
    status,
    order_ts
from {{ source('raw', 'orders') }}
