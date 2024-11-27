-- https://help.aliyun.com/zh/maxcompute/user-guide/datetrunc
{% macro maxcompute__date_trunc(datepart, date) -%}
    {% set datepart = datepart.lower() %}
    {%- if datepart in ['day', 'month', 'year', 'hour'] %}
        datetrunc({{date}}, '{{datepart}}')
    {%- elif datepart in ['minute', 'second'] -%}
        {%- set diviser -%}
            {%- if datepart == 'minute' -%} 60
            {%- else -%} 1
            {%- endif -%}
        {%- endset -%}
        from_unixtime(unix_timestamp({{date}}) - (unix_timestamp({{date}}) % {{diviser}}))
    {%- else -%}
       {{ exceptions.raise_compiler_error("macro datetrunc not support for datepart ~ '" ~ datepart ~ "'") }}
    {%- endif -%}
{%- endmacro %}
