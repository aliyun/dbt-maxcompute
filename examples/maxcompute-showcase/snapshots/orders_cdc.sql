{% snapshot orders_cdc %}

{{
    config(
        target_schema=target.schema ~ '_snapshots',
        strategy='timestamp',
        unique_key='order_id',
        updated_at='order_ts',
        invalidate_hard_deletes=True
    )
}}

-- SCD-2 snapshot of the orders source. dbt-maxcompute materialises the
-- snapshot table as a *transactional* (delta) table so that the per-row
-- MERGE used to close out and insert new versions actually runs cheaply.
-- The snapshot lands in `<target_schema>_snapshots`, not the model
-- schemas, so you can drop the showcase schemas without losing history.
select
    order_id,
    customer_id,
    country,
    amount,
    status,
    order_ts
from {{ source('raw', 'orders') }}

{% endsnapshot %}
