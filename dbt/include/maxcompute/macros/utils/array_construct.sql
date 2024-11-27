{% macro maxcompute__array_construct(inputs, data_type) -%}
    {% set data_type = data_type.lower() %}
    {%- if inputs|length > 0 -%}
       {%- if data_type == 'string' -%}
          array({{ '\"' + inputs|join('\", \"') + '\"' }})
       {%- else -%}
          array({{ inputs|join(', ')}})
       {%- endif -%}
    {%- else -%}
        {%- if data_type == 'string' -%}
            array()
        {%- elif data_type == 'integer' or data_type == 'int'-%}
            array_remove(array(1), 1)
        {%- elif data_type == 'bigint' -%}
            array_remove(array(1L), 1L)
        {%- elif data_type == 'decimal' -%}
            array_remove(array(1BD), 1BD)
        {%- elif data_type == 'timestamp' -%}
            array_remove(array(TIMESTAMP '2017-11-11 00:00:00'), TIMESTAMP '2017-11-11 00:00:00')
        {%- else -%}
            {{ exceptions.raise_compiler_error("Unsupport datatype when create empty array ~ '" ~ data_type ~ "'") }}
        {%- endif -%}
    {%- endif -%}
{%- endmacro %}
