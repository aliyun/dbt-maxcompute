/* For examples of how to fill out the macros please refer to the postgres adapter and docs
postgres adapter macros: https://github.com/dbt-labs/dbt-core/blob/main/plugins/postgres/dbt/include/postgres/macros/adapters.sql
dbt docs: https://docs.getdbt.com/docs/contributing/building-a-new-adapter
*/

{% macro maxcompute__truncate_relation(relation) -%}
    {% if relation.is_table -%}
        TRUNCATE TABLE {{ relation.render() }};
    {% endif -%}
{% endmacro %}

{% macro maxcompute__rename_relation(from_relation, to_relation) -%}
        {% if from_relation.is_table -%}
            ALTER TABLE {{ from_relation.rendor() }}
            RENAME TO {{ to_relation.identifier }};
        {% else -%}
            ALTER VIEW {{ from_relation.database }}.{{ from_relation.schema }}.{{ from_relation.identifier }}
            RENAME TO {{ to_relation.identifier }};
        {% endif -%}
{% endmacro %}

{% macro maxcompute__alter_column_type(relation, column_name, new_column_type) -%}
    ALTER TABLE {{ relation.render() }}
    CHANGE {{ column_name }} {{ column_name }} {{ new_column_type }};
{% endmacro %}

{% macro maxcompute__copy_grants() -%}
    {{ return(True) }}
{% endmacro %}

/* {# override dbt/include/global_project/macros/relations/table/create.sql #} */
{% macro maxcompute__create_table_as(temporary, relation, sql) -%}
  {% set is_transactional = config.get('transactional') -%}

  {%- if is_transactional -%}
    {{ create_transactional_table_as(temporary, relation, sql) }}

  {%- else -%}
    CREATE TABLE IF NOT EXISTS {{ relation.render() }}
    {% if temporary %}
      LIFECYCLE 1
    {% endif %}
    AS (
      {{ sql }}
    )
    ;
  {%- endif %}
{% endmacro %}


/* {# override dbt/include/global_project/macros/relations/view/create.sql #} */
{% macro maxcompute__create_view_as(relation, sql) -%}
    CREATE OR REPLACE VIEW {{ relation.render() }} AS (
        {{ sql }}
    );
{% endmacro %}

{% macro create_transactional_table_as(temporary, relation, sql) -%}
    {% call statement('create_table', auto_begin=False) -%}
        create table {{ relation.render() }}
        {{ get_schema_from_query(sql) }}
        tblproperties("transactional"="true")
        {% if temporary %}
            LIFECYCLE 1
        {% endif %}
        ;
    {% endcall %}
      insert into {{ relation.render() }}
      (
          {{ sql }}
      );
{% endmacro %}

{% macro get_schema_from_query(sql) -%}
(
    {% set model_columns = model.columns %}
    {% for c in get_column_schema_from_query(sql) -%}
    {{ c.name }} {{ c.dtype }}
    {% if model_columns and c.name in  model_columns -%}
       {{ "COMMENT" }} '{{ model_columns[c.name].description }}'
    {%- endif %}
    {{ "," if not loop.last or raw_model_constraints }}

    {% endfor %}
)
{%- endmacro %}


{% macro maxcompute__current_timestamp() -%}
    current_timestamp()
{%- endmacro %}
