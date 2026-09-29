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
    {#-- Submit the DDL. This macro used to only *render* it: dbt-core's
         `SQLAdapter.alter_column_type` evaluates the macro and discards what it
         returns, so nothing reached the server and the widening pass was a no-op.
         MaxCompute truncates an over-long value on insert instead of failing, so a
         widening that is never sent loses data silently. --#}
    {% call statement('alter_column_type', auto_begin=False) -%}
        alter table {{ relation.render() }} change column {{ adapter.quote(column_name) }} {{ adapter.quote(column_name) }} {{ new_column_type }}
    {%- endcall %}
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


{% macro mc_expand_target_column_types(source_relation, target_relation) -%}
    {#-- The one call site for the widening pass, so every materialization that builds a
         temp relation and merges it into an existing model asks the same question the
         same way. A model picks how far to go with `expand_column_types`: `bounded`
         (default: keep the declared bound), `widen` (give it up rather than the value),
         or `off` (leave the schema alone).

         A contract-enforced model is skipped, matching dbt-core: its column types are
         declared, and silently widening a declared column contradicts the contract. --#}
    {%- set contract_config = config.get('contract') -%}
    {%- if not contract_config or not contract_config.enforced -%}
        {% do adapter.expand_target_column_types(
            from_relation=source_relation,
            to_relation=target_relation,
            mode=config.get('expand_column_types', 'bounded')
        ) %}
    {%- endif -%}
{% endmacro %}
