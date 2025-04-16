/* For examples of how to fill out the macros please refer to the postgres adapter and docs
postgres adapter macros: https://github.com/dbt-labs/dbt-core/blob/main/plugins/postgres/dbt/include/postgres/macros/adapters.sql
dbt docs: https://docs.getdbt.com/docs/contributing/building-a-new-adapter
*/

{% macro maxcompute__truncate_relation(relation) -%}
  {% call statement('truncate_relation') -%}
    {% if relation.is_table -%}
        TRUNCATE TABLE {{ relation.render() }};
    {% endif -%}
  {%- endcall %}
{% endmacro %}

{% macro maxcompute__rename_relation(from_relation, to_relation) -%}
    {% call statement('rename_relation') -%}
        {% if from_relation.is_table -%}
            ALTER TABLE {{ from_relation.render() }}
            RENAME TO {{ quote_ref(to_relation.identifier) }};
        {% elif from_relation.is_view -%}
            ALTER VIEW {{ from_relation.render() }}
            RENAME TO {{ quote_ref(to_relation.identifier) }};
        {% else -%}
            {{ get_rename_materialized_view_sql_2(from_relation, to_relation) }}
        {% endif -%}
    {%- endcall %}
{% endmacro %}

{% macro maxcompute__copy_grants() -%}
    {{ return(True) }}
{% endmacro %}

{% macro maxcompute__current_timestamp() -%}
    current_timestamp()
{%- endmacro %}

{% macro quote_ref(input_string) %}
    {% set escaped_string = input_string | replace("`", "``") %}
    `{{ escaped_string }}`
{% endmacro %}
