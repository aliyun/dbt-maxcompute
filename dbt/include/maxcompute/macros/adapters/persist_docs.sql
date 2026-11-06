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
  {#-- 复用 dbt-core 的 validate_doc_columns：schema 里写了、库里却没有的列要像其他
       适配器一样给出告警，而不是静默丢掉。 --#}
  {% set column_dict = validate_doc_columns(relation, column_dict, existing_columns) %}
  {% for column_name, column_info in column_dict.items() %}
    {% set comment = column_info['description'] %}
    {#-- 没写描述的列跳过：None 进字面量会变成 'None' 这种看起来像数据的垃圾。 --#}
    {% if comment %}
      {{ adapter.add_comment_to_column(relation, column_name, comment) }}
    {% endif %}
  {% endfor %}
{% endmacro %}

{% macro maxcompute__alter_relation_comment(relation, relation_comment) -%}
  {%- set sql_hints = config.get('sql_hints', none) -%}
  {%- set sql_header = merge_sql_hints_and_header(sql_hints, config.get('sql_header', none)) -%}

  {{ sql_header if sql_header is not none }}
  {{ adapter.add_comment(relation, relation_comment) }}
{% endmacro %}
