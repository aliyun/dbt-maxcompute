{{
    config(
        materialized='incremental',
        incremental_strategy='append',
        partition_by={
            'fields': 'order_date',
            'data_types': 'string'
        }
    )
}}

-- Append-only into a partitioned table. The adapter emits an explicit
-- `INSERT INTO ... PARTITION (order_date)` and excludes the partition
-- column from the data column list — without that, dbt-maxcompute would
-- previously try to insert `order_date` twice and fail. Each run appends
-- rows; partitions are not overwritten, so use this only when your source
-- gives you net-new rows each time.
select
    order_id,
    customer_id,
    country,
    amount,
    status,
    order_ts,
    cast(order_ts as string) as order_date
from {{ source('raw', 'orders') }}
