

{% macro maxcompute__datediff(first_date, second_date, datepart) %}
    {% set datepart = datepart.lower() %}

    {%- if datepart == 'day' -%}

        datediff({{second_date}}, {{first_date}})

    {%- elif datepart == 'week' -%}

        case when {{first_date}} < {{second_date}}
            then floor( datediff({{second_date}}, {{first_date}}) / 7 )
            else ceil( datediff({{second_date}}, {{first_date}}) / 7 )
            end

        -- did we cross a week boundary (Sunday)
        + case
            when {{first_date}} < {{second_date}} and dayofweek(cast({{second_date}} as timestamp)) < dayofweek(cast({{first_date}} as timestamp)) then 1
            when {{first_date}} > {{second_date}} and dayofweek(cast({{second_date}} as timestamp)) > dayofweek(cast({{first_date}} as timestamp)) then -1
            else 0 end

    {%- elif datepart == 'month' -%}

        case when {{first_date}} < {{second_date}}
            then floor(months_between({{second_date}}, {{first_date}}))
            else ceil(months_between({{second_date}}, {{first_date}}))
            end

        -- did we cross a month boundary?
        + case
            when {{first_date}} < {{second_date}} and dayofmonth(cast({{second_date}} as timestamp)) < dayofmonth(cast({{first_date}} as timestamp)) then 1
            when {{first_date}} > {{second_date}} and dayofmonth(cast({{second_date}} as timestamp)) > dayofmonth(cast({{first_date}} as timestamp)) then -1
            else 0 end

    {%- elif datepart == 'quarter' -%}

        ((year({{second_date}}) - year({{first_date}})) * 4 + quarter({{second_date}}) - quarter({{first_date}}))

    {%- elif datepart == 'year' -%}

        year({{second_date}}) - year({{first_date}})

    {%- elif datepart in ('hour', 'quarter', 'minute', 'second') -%}

        {%- set divisor -%}
            {%- if datepart == 'hour' -%} 3600
            {%- elif datepart == 'quarter' -%} 900
            {%- elif datepart == 'minute' -%} 60
            {%- elif datepart == 'second' -%} 1
            {%- endif -%}
        {%- endset -%}

        case when {{first_date}} < {{second_date}}
            then ceil((
                unix_timestamp( {{second_date}} ) - unix_timestamp( {{first_date}} )
            ) / {{divisor}})
            else floor((
                unix_timestamp( {{second_date}} ) - unix_timestamp( {{first_date}} )
            ) / {{divisor}})
            end

    {%- elif datepart in ('millisecond', 'microsecond') -%}
        {%- set divisor -%}
            {%- if datepart == 'microsecond' -%} 1000
            {%- elif datepart == 'millisecond' -%} 1
            {%- endif -%}
        {%- endset -%}

        case when {{first_date}} < {{second_date}}
            then ceil((
                to_millis( {{second_date}} ) - to_millis( {{first_date}} )
            ) / {{divisor}})
            else floor((
                to_millis( {{second_date}} ) - to_millis( {{first_date}} )
            ) / {{divisor}})
            end
    {%- else -%}

        {{ exceptions.raise_compiler_error("macro datediff not support for datepart ~ '" ~ datepart ~ "'") }}

    {%- endif -%}

{% endmacro %}
