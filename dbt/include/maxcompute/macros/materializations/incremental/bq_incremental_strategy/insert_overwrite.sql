{% macro mc_generate_incremental_insert_overwrite_build_sql(
    tmp_relation, target_relation, sql, unique_key, partition_by, partitions, dest_columns, tmp_relation_exists
) %}
    {% if partition_by is none %}
      {% set missing_partition_msg -%}
      The 'bq_insert_overwrite' strategy requires the `partition_by` config.
      {%- endset %}
      {% do exceptions.raise_compiler_error(missing_partition_msg) %}
    {% endif %}

    {% if partition_by.fields|length != 1 %}
      {% set missing_partition_msg -%}
      The 'bq_insert_overwrite' strategy requires the `partition_by` config.
      {%- endset %}
      {% do exceptions.raise_compiler_error(missing_partition_msg) %}
    {% endif %}

    {% set build_sql = mc_insert_overwrite_sql(
        tmp_relation, target_relation, sql, unique_key, partition_by, partitions, dest_columns, tmp_relation_exists
    ) %}

    {{ return(build_sql) }}

{% endmacro %}

{% macro mc_insert_overwrite_sql(
    tmp_relation, target_relation, sql, unique_key, partition_by, partitions, dest_columns, tmp_relation_exists
) %}
  {% if partitions is not none and partitions != [] %} {# static #}
      {{ mc_static_insert_overwrite_sql(tmp_relation, target_relation, sql, partition_by, partitions, dest_columns, tmp_relation_exists) }}
  {% else %} {# dynamic #}
      {{ mc_dynamic_insert_overwrite_sql(tmp_relation, target_relation, sql, unique_key, partition_by, dest_columns, tmp_relation_exists) }}
  {% endif %}
{% endmacro %}

{% macro mc_static_insert_overwrite_sql(
    tmp_relation, target_relation, sql, partition_by, partitions, dest_columns, tmp_relation_exists
) %}

      {% set predicate -%}
          {{ partition_by.render(False) }} in ({{ partitions | join (', ') }})
      {%- endset %}

      {%- set source_sql -%}
        (
          {% if tmp_relation_exists -%}
            select * from {{ tmp_relation }}
          {%- else -%}
            {{sql}}
          {%- endif %}
        )
      {%- endset -%}

      {%- call statement('main') -%}
        {{ get_insert_overwrite_merge_sql(target_relation, source_sql, dest_columns, [predicate], include_sql_header = not tmp_relation_exists) }};
      {%- endcall -%}

      {%- if tmp_relation_exists -%}
      -- 2. clean up the temp table
        drop table if exists {{ tmp_relation }};
      {%- endif -%}
{% endmacro %}

{% macro mc_dynamic_insert_overwrite_sql(tmp_relation, target_relation, sql, unique_key, partition_by, dest_columns, tmp_relation_exists) %}
      {% set predicate -%}
          {{partition_by.render(False)}} in (select distinct {{partition_by.render(False)}} from {{ tmp_relation }})
      {%- endset %}

      {%- set source_sql -%}
      (
        select * from {{ tmp_relation }}
      )
      {%- endset -%}
      {% if not tmp_relation_exists %}
        {%- call statement('create_tmp_relation') -%}
          {{ create_table_as_internal(True, tmp_relation, sql, True, partition_config=partition_by) }}
        {%- endcall -%}
      {% else %}
        -- 1. temp table already exists, we used it to check for schema changes
      {% endif %}
      -- 3. run the merge statement
      {%- call statement('main') -%}
        {{ get_insert_overwrite_merge_sql(target_relation, source_sql, dest_columns, [predicate]) }};
      {%- endcall -%}
      -- 4. clean up the temp table
      drop table if exists {{ tmp_relation }}
{% endmacro %}
