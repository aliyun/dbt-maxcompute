{{
    config(
        materialized='incremental',
        incremental_strategy='insert_overwrite',
        partition_by={
            'fields': 'order_date',
            'data_types': 'string'
        }
    )
}}

-- The default-ish strategy on MaxCompute. Each `dbt run` recomputes the
-- set of partitions the SELECT yields and overwrites them whole. On a
-- partitioned source you'd usually pair this with an incremental filter
-- so you only rewrite the partitions that actually changed.
select
    order_id,
    customer_id,
    country,
    amount,
    status,
    order_ts,
    cast(order_ts as string) as order_date
from {{ source('raw', 'orders') }}

{% if is_incremental() %}
  -- A real project would scope this to the dates you intend to rewrite,
  -- e.g. the last two days to catch late-arriving rows. The seeded dataset
  -- has no `_updated_at`, so we just take everything in incremental mode.
  where 1=1
{% endif %}
