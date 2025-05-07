
{% materialization incremental,  adapter='maxcompute' -%}
  {%- set raw_partition_by = config.get('partition_by', none) -%}
  {%- set partition_by = adapter.parse_partition_by(raw_partition_by) -%}
  {%- set partitions = config.get('partitions', none) -%}
  {%- set lifecycle = config.get('lifecycle', none) -%}
  {%- set incremental_predicates = config.get('predicates', none) or config.get('incremental_predicates', none) -%}

  {%- set cluster_by = config.get('cluster_by', none) -%}
  {%- set incremental_strategy = config.get('incremental_strategy') or 'merge' -%}

  -- relations
  {%- set existing_relation = load_cached_relation(this) -%}
  {%- set target_relation = this.incorporate(type='table') -%}
  {%- set temp_relation = make_temp_relation(target_relation)-%}
  {%- set backup_relation_type = 'table' if existing_relation is none else existing_relation.type -%}
  {%- set backup_relation = make_backup_relation(target_relation, backup_relation_type) -%}

  -- configs
  {%- set unique_key = config.get('unique_key') -%}
  {%- if unique_key is string -%}
    {%- set unique_key_list = unique_key.split(',') -%}
  {%- elif unique_key is iterable -%}
    {%- set unique_key_list = unique_key -%}
  {%- else -%}
    {%- set unique_key_list = [] -%}
  {%- endif -%}

  {%- set full_refresh_mode = (should_full_refresh() or existing_relation.is_view) -%}
  {%- set on_schema_change = incremental_validate_on_schema_change(config.get('on_schema_change'), default='ignore') -%}


  {% if unique_key_list|length > 0 and config.get('incremental_strategy')=='append' %}
      {% do exceptions.raise_compiler_error('append strategy is not supported for incremental models with a unique key when using MaxCompute') %}
  {% endif %}


  -- the temp_ and backup_ relations should not already exist in the database; get_relation
  -- will return None in that case. Otherwise, we get a relation that we can drop
  -- later, before we try to use this name for the current operation. This has to happen before
  -- BEGIN, in a separate transaction
  {%- set preexisting_temp_relation = load_cached_relation(temp_relation)-%}
  {%- set preexisting_backup_relation = load_cached_relation(backup_relation) -%}
   -- grab current tables grants config for comparision later on
  {% set grant_config = config.get('grants') %}
  {{ drop_relation_if_exists(preexisting_temp_relation) }}
  {{ drop_relation_if_exists(preexisting_backup_relation) }}

  {{ run_hooks(pre_hooks) }}

  {% if existing_relation is none %}
    {%- call statement('main') -%}
        {{ create_table_as_internal(False, target_relation, sql, True, partition_config=partition_by, lifecycle=lifecycle) }}
    {%- endcall -%}
  {% elif full_refresh_mode %}
      {% do log("Hard refreshing " ~ existing_relation) %}
      {{ adapter.drop_relation(existing_relation) }}
      {%- call statement('main') -%}
        {{ create_table_as_internal(False, target_relation, sql, True, partition_config=partition_by, lifecycle=lifecycle) }}
      {%- endcall -%}
  {% else %}
    {% set temp_relation_exists = false %}
    {% if on_schema_change != 'ignore' %}
      {#-- Check first, since otherwise we may not build a temp table --#}
      {#-- Python always needs to create a temp table --#}
      {%- call statement('create_temp_relation') -%}
        {{ create_table_as_internal(True, temp_relation, sql, True, partition_config=partition_by) }}
      {%- endcall -%}
      {% set temp_relation_exists = true %}
      {#-- Process schema changes. Returns dict of changes if successful. Use source columns for upserting/merging --#}
      {% set dest_columns = process_schema_changes(on_schema_change, temp_relation, existing_relation) %}
    {% endif %}

    {% if not dest_columns %}
      {% set dest_columns = adapter.get_columns_in_relation(existing_relation) %}
    {% endif %}

    {% set build_sql = mc_generate_incremental_build_sql(
        incremental_strategy, temp_relation, target_relation, sql, unique_key, partition_by, partitions, dest_columns, temp_relation_exists, incremental_predicates
    ) %}

    {% call statement("main") %}
      {{ build_sql }}
    {% endcall %}
  {% endif %}


  {% set should_revoke = should_revoke(existing_relation, full_refresh_mode) %}
  {% do apply_grants(target_relation, grant_config, should_revoke=should_revoke) %}

  {% do persist_docs(target_relation, model) %}

  {{ run_hooks(post_hooks) }}

  {%- if temp_relation_exists -%}
    {{ adapter.drop_relation(temp_relation) }}
  {%- endif -%}

  {{ return({'relations': [target_relation]}) }}
{%- endmaterialization %}

{% macro mc_generate_incremental_build_sql(
    strategy, temp_relation, target_relation, sql, unique_key, partition_by, partitions, dest_columns, temp_relation_exists, incremental_predicates
) %}
  {% if strategy == 'insert_overwrite' %}
    {% set build_sql = mc_generate_incremental_insert_overwrite_build_sql(
        temp_relation, target_relation, sql, unique_key, partition_by, partitions, dest_columns, temp_relation_exists
    ) %}
  {% elif strategy == 'microbatch' %}
    {% set build_sql = mc_generate_microbatch_build_sql(
        temp_relation, target_relation, sql, unique_key, partition_by, partitions, dest_columns, temp_relation_exists
    ) %}
  {% else %} {# strategy == 'dbt origin' #}
    {%- call statement('create_temp_relation') -%}
      {% if not temp_relation_exists %}
          {{ create_table_as_internal(True, temp_relation, sql, True, partition_config=partition_by) }}
      {% endif %}
    {%- endcall -%}
    {% set strategy_sql_macro_func = adapter.get_incremental_strategy_macro(context, strategy) %}
    {% set strategy_arg_dict = ({'target_relation': target_relation, 'temp_relation': temp_relation, 'unique_key': unique_key, 'dest_columns': dest_columns, 'incremental_predicates': incremental_predicates }) %}
    {% set build_sql = strategy_sql_macro_func(strategy_arg_dict) %}
  {% endif %}
  {{ return(build_sql) }}
{% endmacro %}

{% macro get_quoted_list(column_names) %}
    {% set quoted = [] %}
    {% for col in column_names -%}
        {%- do quoted.append(adapter.quote(col)) -%}
    {%- endfor %}
    {{ return(quoted) }}
{% endmacro %}

{% macro maxcompute__get_incremental_microbatch_sql(arg_dict) %}

  {% if arg_dict["unique_key"] %}
    {% do return(adapter.dispatch('get_incremental_merge_sql', 'dbt')(arg_dict)) %}
  {% else %}
    {{ exceptions.raise_compiler_error("dbt-maxcompute 'microbatch' requires a `unique_key` config") }}
  {% endif %}
{% endmacro %}
