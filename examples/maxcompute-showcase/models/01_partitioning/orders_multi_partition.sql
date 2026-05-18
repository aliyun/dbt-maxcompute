{{
    config(
        materialized='table',
        partition_by={
            'fields': 'country, order_date',
            'data_types': 'string, string'
        }
    )
}}

-- Two-level static partition: country first, then order_date. Both columns
-- are materialised as MaxCompute partition columns (and must be of static
-- types — `string` here). Queries that filter on either column get
-- partition pruning.
select
    order_id,
    customer_id,
    amount,
    status,
    order_ts,
    country,
    cast(order_ts as date) as order_date
from {{ source('raw', 'orders') }}
