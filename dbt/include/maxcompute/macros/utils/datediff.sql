--https://help.aliyun.com/zh/maxcompute/user-guide/datediff
{% macro maxcompute__datediff(first_date, second_date, datepart) %}
    {% set datepart = datepart.lower() %}

    {# Direct mapping for native MaxCompute dateparts #}
    {% set native_dateparts = {
        'year': 'year', 'yyyy': 'yyyy',
        'month': 'month', 'mon': 'mon', 'mm': 'mm',
        'week': 'week',
        'day': 'day', 'dd': 'dd',
        'hour': 'hour', 'hh': 'hh',
        'minute': 'mi', 'mi': 'mi',
        'second': 'ss', 'ss': 'ss',
        'millisecond': 'ff3', 'ff3': 'ff3',
        'microsecond': 'ff6', 'ff6': 'ff6'
    } %}

    {%- if datepart in native_dateparts -%}
        datediff({{ second_date }}, {{ first_date }}, '{{ native_dateparts[datepart] }}')
    {%- elif datepart == 'quarter' -%}
        ((year({{ second_date }}) - year({{ first_date }})) * 4 + quarter({{ second_date }}) - quarter({{ first_date }}))
    {%- else -%}
        {{ exceptions.raise_compiler_error("macro datediff not support for datepart ~ '" ~ datepart ~ "'") }}
    {%- endif -%}

{% endmacro %}
