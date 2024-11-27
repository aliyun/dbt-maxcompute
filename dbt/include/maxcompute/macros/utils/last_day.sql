{%- macro maxcompute_last_day(date, datepart) -%}
    {% set datepart = datepart.lower() %}
    {%- if datepart == 'quarter' -%}
        {{ exceptions.raise_compiler_error("macro last_day not support for datepart ~ '" ~ datepart ~ "'") }}
    {%- else -%}
        cast(
            {{
                dbt.dateadd('day', '-1',
                    dbt.dateadd(datepart, '1',
                        dbt.date_trunc(datepart, date)
                    )
                )
            }}
            as date)
    {%- endif -%}
{%- endmacro -%}
