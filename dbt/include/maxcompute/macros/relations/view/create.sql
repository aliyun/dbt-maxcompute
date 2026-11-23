{% macro maxcompute__create_view_as(relation, sql, replace=true, sql_header=none) -%}
  {%- if sql_header is none -%}
    {%- set sql_hints = config.get('sql_hints', none) -%}
    {%- set sql_header = merge_sql_hints_and_header(sql_hints, config.get('sql_header', none)) -%}
  {%- endif -%}

  {{ sql_header if sql_header is not none }}
  create {% if replace %}or replace {% endif %}view {{ relation.render() }}
    {% set contract_config = config.get('contract') %}
    {% if contract_config.enforced %}
      {{ get_assert_columns_equivalent(sql) }}
    {%- endif %}
  as (
    {{ sql }}
  );
{%- endmacro %}
