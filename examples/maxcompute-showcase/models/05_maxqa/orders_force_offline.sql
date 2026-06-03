{{
    config(
        materialized='table',
        lifecycle=1,
        sql_hints={'dbt.execution_mode': 'offline'},
        table_comment='Force offline even when profile default is maxqa.'
    )
}}

-- Model-level override: dbt.execution_mode=offline forces this model
-- through the standard offline engine regardless of the profile setting.
select
    customer_id,
    count(*)    as order_cnt,
    sum(amount) as lifetime_value
from {{ source('raw', 'orders') }}
group by customer_id
