{% macro maxcompute__alter_column_comment(relation, column_dict) %}
  {% set existing_columns = adapter.get_columns_in_relation(relation) | map(attribute="name") | list %}
  {% for column_name in column_dict if (column_name in existing_columns) %}
    {% set comment = column_dict[column_name]['description'] %}
    {{ adapter.add_comment_to_column(relation, column_name, comment) }}
  {% endfor %}
{% endmacro %}

{% macro maxcompute__alter_relation_comment(relation, relation_comment) -%}
  {{ adapter.add_comment(relation, relation_comment) }}
{% endmacro %}
