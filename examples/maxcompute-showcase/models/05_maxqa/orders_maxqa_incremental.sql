{{
    config(
        materialized='incremental',
        unique_key='order_id',
        incremental_strategy='merge',
        lifecycle=1,
        table_comment='Incremental merge via MaxQA.'
    )
}}

select
    order_id,
    customer_id,
    country,
    amount,
    status,
    order_ts
from {{ source('raw', 'orders') }}
{% if is_incremental() %}
where order_ts > (select max(order_ts) from {{ this }})
{% endif %}
