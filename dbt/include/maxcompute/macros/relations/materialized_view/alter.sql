{% macro maxcompute__get_alter_materialized_view_as_sql(
    relation,
    new_config,
    sql,
    existing_relation,
    backup_relation,
    intermediate_relation
) %}
    {{ get_replace_sql(existing_relation, relation, sql) }}
{% endmacro %}

{% macro maxcompute__get_materialized_view_configuration_changes(existing_relation, new_config) %}
    {#- An empty Jinja macro returns the empty string (truthy), not None, which   -#}
    {#- short-circuits dbt-core's `is none` refresh branch and forces a replace   -#}
    {#- on every run. Delegate to the adapter so we return real None when nothing -#}
    {#- changed and a dict when something did.                                    -#}
    {%- set changes = adapter.materialized_view_config_changes(existing_relation, config.model) -%}
    {{ return(changes) }}
{% endmacro %}
