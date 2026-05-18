
{% macro maxcompute__right(string_text, length_expression) %}

    case when {{ length_expression }} = 0
        then ''
    else
        substr(
        {{ string_text }},
        (length({{ string_text }})-cast({{ length_expression }} as int)+1),
        cast({{ length_expression }} as int)
    )
    end

{%- endmacro -%}
