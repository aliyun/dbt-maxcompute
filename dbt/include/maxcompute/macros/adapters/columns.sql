{% macro maxcompute__alter_column_type(relation, column_name, new_column_type) -%}
    alter table {{ relation.render() }} change column {{ adapter.quote(column_name) }} {{ adapter.quote(column_name) }} {{ new_column_type }};
{% endmacro %}


{% macro maxcompute__alter_relation_add_remove_columns(relation, add_columns, remove_columns) %}
  {% if add_columns is not none and add_columns|length > 0%}
      {% set sql -%}
         alter {{ relation.type }} {{ relation.render() }} add columns
                {% for column in add_columns %}
                   {{ column.name }} {{ column.data_type }}{{ ',' if not loop.last }}
                {% endfor %};
      {%- endset -%}
      {% do run_query(sql) %}
  {% endif %}
  {% if remove_columns is not none and remove_columns|length > 0%}
      {% set sql -%}
         alter {{ relation.type }} {{ relation.render() }} drop columns
                {% for column in remove_columns %}
                   {{ column.name }} {{ ',' if not loop.last }}
                {% endfor %};
      {%- endset -%}
      {% do run_query(sql) %}
  {% endif %}
{% endmacro %}
