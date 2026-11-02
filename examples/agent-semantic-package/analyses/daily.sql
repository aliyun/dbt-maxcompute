set odps.sql.timezone=UTC;

select cast(o.ordered_at as date) as metric_time__day,
       sum(coalesce(o.amount, 0)) as paid_revenue,
       count(o.order_id) as paid_orders,
       sum(coalesce(o.amount, 0)) / nullif(count(o.order_id), 0) as paid_aov
from {{ ref('orders') }} o
where o.status = 'paid'
group by cast(o.ordered_at as date)
order by metric_time__day
