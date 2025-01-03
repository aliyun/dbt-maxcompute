{% macro mc_generate_incremental_merge_build_sql(
    tmp_relation, target_relation, sql, unique_key, partition_by, dest_columns, tmp_relation_exists, incremental_predicates
) %}
    {%- set source_sql -%}
        {%- if tmp_relation_exists -%}
        (
        select * from {{ tmp_relation }}
        )
        {%- else -%} {#-- wrap sql in parens to make it a subquery --#}
        (
            {{sql}}
        )
        {%- endif -%}
    {%- endset -%}

    {%- set predicates = [] if incremental_predicates is none else [] + incremental_predicates -%}
    {% set build_sql = get_merge_sql(target_relation, source_sql, unique_key, dest_columns, predicates) %}
    {{ return(build_sql) }}

{% endmacro %}
