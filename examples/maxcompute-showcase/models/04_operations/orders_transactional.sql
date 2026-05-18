{{
    config(
        materialized='table',
        transactional=true,
        primary_keys=['order_id'],
        delta_table_bucket_num=8
    )
}}

-- A MaxCompute *delta table*: transactional storage + a declared primary
-- key. The adapter emits:
--     CREATE TABLE ... (order_id ... NOT NULL, ..., primary key (order_id))
--     TBLPROPERTIES ("transactional"="true", "write.bucket.num"="8")
-- Delta tables support row-level UPDATE / DELETE / MERGE efficiently and
-- power dbt-maxcompute's snapshot materialization under the hood.
--
-- Trade-off: bucket count is fixed at create time. Pick `delta_table_bucket_num`
-- with eventual table size in mind — too few buckets bottlenecks writes,
-- too many wastes metadata. 8 is a reasonable starting point for small
-- demo data; production tables commonly run 16–256.
select
    order_id,
    customer_id,
    country,
    amount,
    status,
    order_ts
from {{ source('raw', 'orders') }}
