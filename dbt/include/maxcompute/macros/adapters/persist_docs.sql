{% macro maxcompute__uses_default_docs_macro(macro_function, resolved_macro) -%}
  {%- do return(
      macro_function is defined
      and macro_function.macro is defined
      and macro_function.macro.package_name == 'dbt'
      and resolved_macro.macro.package_name == 'dbt_maxcompute'
  ) -%}
{%- endmacro %}

{% macro maxcompute__should_inline_column_docs(relation_type, database) -%}
  {%- set materialized = config.get('materialized') -%}
  {%- set view_model = materialized == 'view' and relation_type == 'view' and not config.get('sql_header') -%}
  {%- set contract_table = materialized == 'table' and relation_type == 'table'
      and model['language'] == 'sql' and config.get('contract').enforced -%}
  {%- do return(
      (view_model or contract_table)
      and config.persist_column_docs()
      and model.columns.values() | selectattr('description') | list | length > 0
      and not adapter.is_external_catalog_project(database)
      and maxcompute__uses_default_docs_macro(persist_docs, adapter.dispatch('persist_docs', 'dbt'))
      and maxcompute__uses_default_docs_macro(
          alter_column_comment, adapter.dispatch('alter_column_comment', 'dbt')
      )
      and (not view_model or not config.persist_relation_docs()
           or maxcompute__uses_default_docs_macro(
               alter_relation_comment, adapter.dispatch('alter_relation_comment', 'dbt')
           ))
  ) -%}
{%- endmacro %}

{% macro maxcompute__persist_docs(relation, model, for_relation, for_columns) -%}
  {% if for_relation and config.persist_relation_docs() and model.description %}
    {% set relation_comment_sql = alter_relation_comment(relation, model.description) | trim %}
    {% if relation_comment_sql %}
      {% do run_query(relation_comment_sql) %}
    {% endif %}
  {% endif %}

  {% if for_columns and config.persist_column_docs() and model.columns %}
    {{ alter_column_comment(relation, model.columns) }}
  {% endif %}
{% endmacro %}

{% macro maxcompute__alter_column_comment(relation, column_dict) %}
  {% if model is defined and config is defined
        and maxcompute__should_inline_column_docs(relation.type, relation.database) %}
    {% do adapter.add_comments_to_columns(relation, column_dict) %}
  {% else %}
    {% set existing_columns = adapter.get_columns_in_relation(relation) | map(attribute="name") | list %}
    {% for column_name in column_dict if (column_name in existing_columns) %}
      {% set comment = column_dict[column_name]['description'] %}
      {{ adapter.add_comment_to_column(relation, column_name, comment) }}
    {% endfor %}
  {% endif %}
{% endmacro %}

{% macro maxcompute__alter_relation_comment(relation, relation_comment) -%}
  {%- if relation.is_view and model is defined
      and maxcompute__should_inline_column_docs(relation.type, relation.database)
      and adapter.view_comment_matches(relation, relation_comment) -%}
    {{- return('') -}}
  {%- endif -%}

  {%- set sql_hints = config.get('sql_hints', none) -%}
  {%- set sql_header = merge_sql_hints_and_header(sql_hints, config.get('sql_header', none)) -%}

  {{ sql_header if sql_header is not none }}
  {{ adapter.add_comment(relation, relation_comment) }}
{% endmacro %}
