{% macro maxcompute__create_table_as(temporary, relation, sql) -%}
    {%- set sql_header = config.get('sql_header', none) -%}
    {{ sql_header if sql_header is not none }}

    {% call statement('create_table', auto_begin=False) -%}
        create table if not exists {{ relation.render() }}
            {% set contract_config = config.get('contract') %}
            {% if contract_config.enforced and (not temporary) %}
                {{ get_assert_columns_equivalent(sql) }}
                {{ get_table_columns_and_constraints() }}
                {%- set sql = get_select_subquery(sql) %}
            {% else %}
                {{ get_table_columns(sql) }}
            {% endif %}

            {%- set is_transactional = config.get('transactional') -%}
            {%- if is_transactional -%}
                tblproperties("transactional"="true")
            {% endif -%}

            {% if temporary -%}
                LIFECYCLE 1
            {%- endif %}
            ;
    {%- endcall -%}

    insert into {{ relation.render() }} (
    {% for c in get_column_schema_from_query(sql) -%}
        {{ c.name }}{{ "," if not loop.last }}
    {% endfor %}
    )(
        {{ sql }}
    );
{%- endmacro %}


{% macro get_table_columns(sql) -%}
(
    {% set model_columns = model.columns %}
    {% for c in get_column_schema_from_query(sql) -%}
    {{ c.name }} {{ c.dtype }}
    {% if model_columns and c.name in  model_columns -%}
       {{ "COMMENT" }} '{{ model_columns[c.name].description }}'
    {%- endif %}
    {{ "," if not loop.last }}
    {% endfor %}
)
{%- endmacro %}