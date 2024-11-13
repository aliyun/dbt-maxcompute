{% macro maxcompute__dateadd(datepart, interval, from_date_or_timestamp) %}
    {%- if datepart in ['day', 'month', 'year'] %}
       dateadd({{ from_date_or_timestamp }}, {{ interval }}, '{{ datepart }}')
    {%- elif datepart == 'hour' -%}
       from_unixtime(unix_timestamp({{from_date_or_timestamp}}) + {{interval}}*3600)
    {%- else -%}
       {{ exceptions.raise_compiler_error("macro dateadd not implemented for datepart ~ '" ~ datepart ~ "' ~ on ODPS") }}
    {%- endif -%}
{% endmacro %}
