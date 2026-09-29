{# dbt-core's snapshot staging table calls the *dispatched* macro for the
   target's columns when the snapshot uses `hard_deletes='new_record'`
   (dbt/include/global_project/macros/materializations/snapshots/helpers.sql).
   Without an implementation here, that option fails with
   "get_columns_in_relation macro not implemented for adapter maxcompute" before
   anything reaches MaxCompute.  The adapter already reads columns through pyodps,
   so delegate instead of re-implementing the metadata query in SQL. -#}
{% macro maxcompute__get_columns_in_relation(relation) -%}
  {{ return(adapter.get_columns_in_relation(relation)) }}
{%- endmacro %}


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
