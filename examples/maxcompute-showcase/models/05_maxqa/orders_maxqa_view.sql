{{
    config(
        materialized='view',
        table_comment='View built via MaxQA.'
    )
}}

select
    order_id,
    customer_id,
    country,
    amount,
    status
from {{ source('raw', 'orders') }}
where amount > 100
