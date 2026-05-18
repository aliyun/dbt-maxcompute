{{
    config(
        materialized='materialized_view',
        lifecycle=7,
        partition_by={
            'fields': 'order_date',
            'data_types': 'string'
        },
        on_configuration_change='apply'
    )
}}

-- Daily revenue rollup as a materialized view. Re-running `dbt run` with
-- this file unchanged triggers `ALTER MATERIALIZED VIEW ... REBUILD`, not
-- a DROP+CREATE — you can verify with `desc extended {{ schema }}.orders_daily_mv`
-- and checking the table's creation time.
select
    cast(order_ts as string) as order_date,
    country,
    count(*)            as order_count,
    sum(amount)         as gross_revenue,
    sum(case when status = 'paid' then amount else 0 end) as net_revenue
from {{ source('raw', 'orders') }}
group by 1, 2
