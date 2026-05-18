{% macro maxcompute__hash(expression) -%}
    case when {{ expression }} is null
        then md5('')
    else
        md5({{ expression }})
    end
{%- endmacro %}
