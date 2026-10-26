{{ config(alias=var('table_prefix') ~ '_time_spine') }}

{{ dbt.date_spine('day', "date '2026-01-31'", "date '2026-02-04'") }}
