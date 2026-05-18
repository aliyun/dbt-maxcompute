{{
    config(
        materialized='incremental',
        incremental_strategy='merge',
        unique_key='order_id'
    )
}}

-- Classic upsert on a non-partitioned table. New rows are inserted, rows
-- whose `order_id` already exists are updated in place. Use this when
-- your source can mutate rows (refunds, status changes, ...).
select
    order_id,
    customer_id,
    country,
    amount,
    status,
    order_ts
from {{ source('raw', 'orders') }}

{% if is_incremental() %}
  where order_id not in (select order_id from {{ this }} where status = 'cancelled')
{% endif %}
