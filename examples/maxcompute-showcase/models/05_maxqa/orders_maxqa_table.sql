{{
    config(
        materialized='table',
        lifecycle=1,
        table_comment='Table built via MaxQA — inherits execution_mode from profile.'
    )
}}

-- This model uses whatever execution_mode the profile specifies.
-- If the profile sets execution_mode=maxqa, both the CREATE TABLE and
-- INSERT INTO will be submitted through the MaxQA endpoint; the server
-- will automatically fall back to offline for DDL if needed.
select
    country,
    count(*)       as order_cnt,
    sum(amount)    as total_amount,
    min(order_ts)  as first_order,
    max(order_ts)  as last_order
from {{ source('raw', 'orders') }}
where status = 'paid'
group by country
