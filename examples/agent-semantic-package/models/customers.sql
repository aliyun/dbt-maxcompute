{{ config(alias=var('table_prefix') ~ '_customers') }}

select 1 as customer_id, 'east' as region
union all select 2, 'west'
union all select 3, 'south'
