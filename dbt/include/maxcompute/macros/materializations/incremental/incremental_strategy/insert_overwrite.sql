{% macro mc_generate_incremental_insert_overwrite_build_sql(
    tmp_relation, target_relation, sql, unique_key, partition_by, partitions, dest_columns, tmp_relation_exists
) %}
    {% if partition_by is none %}
      {% set missing_partition_msg -%}
      The 'insert_overwrite' strategy requires the `partition_by` config.
      {%- endset %}
      {% do exceptions.raise_compiler_error(missing_partition_msg) %}
    {% endif %}

    {% if partition_by.fields|length != 1 %}
      {% set missing_partition_msg -%}
      The 'insert_overwrite' strategy requires the `partition_by` config.
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
      {% if not tmp_relation_exists %}
        {%- call statement('create_tmp_relation') -%}
          {{ create_table_as_internal(True, tmp_relation, sql, True, partition_config=partition_by) }}
        {%- endcall -%}
      {% endif %}
      -- 3. run the merge statement
      {%- call statement('main') -%}
      {% if partitions is not none and partitions != [] %} {# static #}
          {{ mc_static_insert_overwrite_merge_sql(target_relation, tmp_relation, partition_by, partitions) }}
      {% else %} {# dynamic #}
          {{ mc_dynamic_insert_overwrite_sql(target_relation, tmp_relation, partition_by) }}
      {% endif %}
      {%- endcall -%}
      -- 4. clean up the temp table
      drop table if exists {{ tmp_relation }}
{% endmacro %}

{% macro mc_static_insert_overwrite_merge_sql(target, source, partition_by, partitions) -%}
    {%- set sql_header = config.get('sql_header', none) -%}
    {{ sql_header if sql_header is not none and include_sql_header }}

    {%- call statement('drop_static_partition') -%}
    DELETE FROM {{ target }}
    WHERE {{ partition_by.render(False) }} in ({{ partitions | join(',') }})
    {%- endcall -%}

    INSERT OVERWRITE TABLE {{ target }} PARTITION({{ partition_by.render(False) }})
    (
    SELECT *
    FROM {{ source }}
    WHERE {{ partition_by.render(False) }} in ({{ partitions | join(',') }})
    )
{% endmacro %}

{% macro mc_dynamic_insert_overwrite_sql(target, source, partition_by) -%}
    {%- set sql_header = config.get('sql_header', none) -%}
    {{ sql_header if sql_header is not none and include_sql_header }}
    {% if partition_by.auto_partition() -%}
    INSERT OVERWRITE TABLE {{ target }}
    (
    SELECT *
    FROM {{ source }}
    )
    {%- else -%}
    INSERT OVERWRITE TABLE {{ target }} PARTITION({{ partition_by.render(False) }})
    (
    SELECT *
    FROM {{ source }}
    )
    {%- endif -%}
{% endmacro %}
