-- https://help.aliyun.com/zh/maxcompute/user-guide/datetrunc
{% macro maxcompute__date_trunc(datepart, date) -%}
    {% set datepart = datepart.lower() %}
      {%- if datepart in ['year', 'yyyy', 'month', 'mon', 'mm', 'day', 'dd', 'hour', 'hh', 'mi', 'ss', 'ff3', 'q', 'w'] %}
          datetrunc({{date}}, '{{datepart}}')
      {%- elif datepart in ['quarter', 'minute', 'second', 'isoweek', 'millisecond'] -%}
	    {% set mapped_datepart = {
            'quarter': 'q',
            'minute': 'mi',
			'second': 'ss',
            'isoweek': 'w',
            'millisecond': 'ff3'
        }[datepart] %}
        datetrunc({{date}}, '{{ mapped_datepart }}')
      {%- else -%}
         {{ exceptions.raise_compiler_error("macro datetrunc not support for datepart ~ '" ~ datepart ~ "'") }}
      {%- endif -%}
{%- endmacro %}