{{
    config(
        materialized='incremental',
        incremental_strategy='delete+insert',
        unique_key=['order_id', 'customer_id']
    )
}}

-- delete+insert with a *list* unique_key. The adapter rewrites the DELETE
-- as `WHERE (order_id, customer_id) IN (SELECT order_id, customer_id FROM src)`
-- because MaxCompute's DELETE does not support the Postgres `USING` form.
-- Use this strategy when you want the same semantics as `merge` but find
-- two statements easier to reason about than one.
select
    order_id,
    customer_id,
    country,
    amount,
    status,
    order_ts
from {{ source('raw', 'orders') }}
