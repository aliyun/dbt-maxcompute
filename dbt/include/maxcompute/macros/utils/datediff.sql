--https://help.aliyun.com/zh/maxcompute/user-guide/datediff
{% macro maxcompute__datediff(first_date, second_date, datepart) %}
    {% set datepart = datepart.lower() %}

    {%- if datepart in ['year', 'yyyy', 'month', 'mon', 'mm', 'day', 'dd', 'hour', 'hh', 'mi', 'ss' ,'ff3'] -%}
        datediff({{second_date}}, {{first_date}}, '{{datepart}}')
	{%- elif datepart in ['minute', 'second', 'microsecond'] -%}
	    {% set mapped_datepart = {
            'minute': 'mi',
            'second': 'ss',
			'microsecond': 'ff3'
        }[datepart] %}
        datediff({{ second_date }}, {{ first_date }}, '{{ mapped_datepart }}')
    {%- elif datepart == 'week' -%}
        case when datediff({{first_date}}, {{second_date}}) < 0
            then floor( datediff({{second_date}}, {{first_date}}) / 7 )
            else ceil( datediff({{second_date}}, {{first_date}}) / 7 )
            end

        -- did we cross a week boundary (Sunday)
        + case
            when datediff({{first_date}}, {{second_date}}) < 0 and dayofweek(cast({{second_date}} as timestamp)) < dayofweek(cast({{first_date}} as timestamp)) then 1
            when datediff({{first_date}}, {{second_date}}) > 0 and dayofweek(cast({{second_date}} as timestamp)) > dayofweek(cast({{first_date}} as timestamp)) then -1
            else 0 end
    {%- elif datepart == 'quarter' -%}
        ((year({{second_date}}) - year({{first_date}})) * 4 + quarter({{second_date}}) - quarter({{first_date}}))
    {%- elif datepart == 'microsecond' -%}
        case when datediff({{first_date}}, {{second_date}}) < 0
            then ceil((
                to_millis( {{second_date}} ) - to_millis( {{first_date}} )
            ) / 1000
            else floor((
                to_millis( {{second_date}} ) - to_millis( {{first_date}} )
            ) / 1000
            end
    {%- else -%}

        {{ exceptions.raise_compiler_error("macro datediff not support for datepart ~ '" ~ datepart ~ "'") }}

    {%- endif -%}

{% endmacro %}