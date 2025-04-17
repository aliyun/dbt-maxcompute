{% macro maxcompute__persist_docs(relation, model, for_relation, for_columns) -%}
  {% if for_relation and config.persist_relation_docs() and model.description %}
    {% do run_query(alter_relation_comment(relation, model.description)) %}
  {% endif %}

  {% if for_columns and config.persist_column_docs() and model.columns %}
    {{ alter_column_comment(relation, model.columns) }}
  {% endif %}
{% endmacro %}

{% macro maxcompute__alter_column_comment(relation, column_dict) %}
  {% set existing_columns = adapter.get_columns_in_relation(relation) | map(attribute="name") | list %}
  {% for column_name in column_dict if (column_name in existing_columns) %}
    {% set comment = column_dict[column_name]['description'] %}
    {{ adapter.add_comment_to_column(relation, column_name, comment) }}
  {% endfor %}
{% endmacro %}

{% macro maxcompute__alter_relation_comment(relation, relation_comment) -%}
  {%- set sql_hints = config.get('sql_hints', none) -%}
  {%- set sql_header = merge_sql_hints_and_header(sql_hints, config.get('sql_header', none)) -%}

  {{ sql_header if sql_header is not none }}
  {{ adapter.add_comment(relation, relation_comment) }}
{% endmacro %}
