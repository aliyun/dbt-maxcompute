{{ config(alias=var('table_prefix') ~ '_orders') }}

select 1 as order_id, 1 as customer_id, timestamp '2026-01-31 23:30:00' as ordered_at,
       cast(120 as decimal(18,2)) as amount, 'paid' as status
union all select 2, 2, timestamp '2026-02-01 00:30:00', 80, 'paid'
union all select 3, 1, timestamp '2026-02-01 12:00:00', 50, 'cancelled'
union all select 4, 1, timestamp '2026-02-02 10:00:00', 200, 'paid'
union all select 5, 3, timestamp '2026-02-02 11:00:00', 40, 'paid'
union all select 6, 2, timestamp '2026-02-03 09:00:00', cast(null as decimal(18,2)), 'paid'
union all select 7, 999, timestamp '2026-02-03 10:00:00', 60, 'paid'
union all select 8, 3, timestamp '2026-02-03 11:00:00', 500, 'refunded'
