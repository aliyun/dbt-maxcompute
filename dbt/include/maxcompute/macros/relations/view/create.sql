{% macro maxcompute__create_view_as(relation, sql, replace=true, sql_header=none) -%}
  {%- set use_config_header = sql_header is none -%}
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
    {% if replace and use_config_header and maxcompute__should_inline_column_docs('view', relation.database) %}
      {% set probe_header = [sql_header, 'set dbt.retry=false;'] | select | join('\n') %}
      {% set view_columns = adapter.get_view_column_definitions_for_docs(
          get_empty_subquery_sql(sql, probe_header), model.columns
      ) %}
      {% if view_columns %}
        ({{ view_columns | join(', ') }})
        {% if config.persist_relation_docs() and model.description %}
          COMMENT {{ adapter.quote_string_literal(model.description) }}
        {% endif %}
      {% endif %}
    {% endif %}
  as (
    {{ sql }}
  );
{%- endmacro %}
