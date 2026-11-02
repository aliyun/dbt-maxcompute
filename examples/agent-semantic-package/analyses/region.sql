set odps.sql.timezone=UTC;

select c.region as customer__region,
       sum(coalesce(o.amount, 0)) as paid_revenue,
       count(o.order_id) as paid_orders,
       sum(coalesce(o.amount, 0)) / nullif(count(o.order_id), 0) as paid_aov
from {{ ref('orders') }} o
left join {{ ref('customers') }} c on o.customer_id = c.customer_id
where o.status = 'paid'
group by c.region
order by customer__region
