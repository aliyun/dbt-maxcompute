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
            ALTER TABLE {{ from_relation.render() }}
            RENAME TO {{ to_relation.identifier }};
        {% else -%}
            ALTER VIEW {{ from_relation.render() }}
            RENAME TO {{ to_relation.identifier }};
        {% endif -%}
{% endmacro %}

{% macro maxcompute__copy_grants() -%}
    {{ return(True) }}
{% endmacro %}

{% macro maxcompute__current_timestamp() -%}
    current_timestamp()
{%- endmacro %}
