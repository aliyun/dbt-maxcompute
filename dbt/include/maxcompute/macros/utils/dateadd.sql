--https://help.aliyun.com/zh/maxcompute/user-guide/dateadd
{% macro maxcompute__dateadd(datepart, interval, from_date_or_timestamp) %}
    {% set datepart = datepart.lower() %}
    {%- if datepart in ['year', 'yyyy', 'month', 'mon', 'mm', 'day', 'dd'] %}
       dateadd({{ from_date_or_timestamp }}, {{ interval }}, '{{ datepart }}')
    {%- elif datepart in ['hour', 'hh', 'mi', 'ss', 'ff3'] -%}
       dateadd(cast({{ from_date_or_timestamp }} as {{ dbt.type_timestamp() }}), {{ interval }}, '{{ datepart }}')
    {%- elif datepart == 'week' -%}
       dateadd({{ from_date_or_timestamp }}, {{ interval }}*7, 'day')
    {%- elif datepart == 'quarter' -%}
       dateadd({{ from_date_or_timestamp }}, {{ interval }}*3, 'month')
    {%- elif datepart in ['minute', 'second', 'millisecond'] -%}
        {% set mapped_datepart = {
            'minute': 'mi',
			'second': 'ss',
            'millisecond': 'ff3'
        }[datepart] %}
        dateadd(cast({{ from_date_or_timestamp }} as {{ dbt.type_timestamp() }}), {{interval}}, '{{ mapped_datepart }}')
    {%- else -%}
       {{ exceptions.raise_compiler_error("macro dateadd not support for datepart ~ '" ~ datepart ~ "'") }}
    {%- endif -%}
{% endmacro %}
