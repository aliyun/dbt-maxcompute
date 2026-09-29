
{% materialization incremental, adapter='maxcompute', supported_languages=['sql', 'python'] -%}
  {%- set language = model['language'] -%}
  {%- set build_code = compiled_code if language == 'python' else sql -%}
  {%- set raw_partition_by = config.get('partition_by', none) -%}
  {%- set partition_by = adapter.parse_partition_by(raw_partition_by) -%}
  {%- set partitions = config.get('partitions', none) -%}
  {%- set lifecycle = config.get('lifecycle', none) -%}
  {%- set incremental_predicates = config.get('predicates', none) or config.get('incremental_predicates', none) -%}

  {%- set cluster_by = config.get('cluster_by', none) -%}
  {%- set tblproperties = config.get('tblproperties', none) -%}
  {%- set primary_keys = config.get('primary_keys', none) -%}
  {#-- MaxCompute MERGE and the existing SQL incremental contract require a --#}
  {#-- transactional target. Keep Python-created relations consistent.       --#}
  {%- set is_transactional = true -%}
  {%- set incremental_strategy = config.get('incremental_strategy') or 'merge' -%}
  {%- set sql_hints = config.get('sql_hints', none) -%}
  {%- set sql_header = merge_sql_hints_and_header(sql_hints, config.get('sql_header', none)) -%}

  -- relations
  {%- set existing_relation = load_cached_relation(this) -%}
  {%- set target_relation = this.incorporate(type='table') -%}
  {%- set temp_relation = make_temp_relation(target_relation)-%}
  {%- set intermediate_relation = make_intermediate_relation(target_relation) -%}
  {%- set backup_relation_type = 'table' if existing_relation is none else existing_relation.type -%}
  {%- set backup_relation = make_backup_relation(target_relation, backup_relation_type) -%}
  {%- set did_python_full_refresh_swap = false -%}

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

  {% if incremental_strategy == 'microbatch' %}
    {% do mc_validate_microbatch_config(partition_by, config.get('batch_size'), config.get('event_time')) %}
  {% endif %}


  {% if unique_key_list|length > 0 and config.get('incremental_strategy')=='append' %}
      {% do exceptions.raise_compiler_error('append strategy is not supported for incremental models with a unique key when using MaxCompute') %}
  {% endif %}

  {% if language == 'python' and cluster_by %}
      {% do exceptions.raise_compiler_error(
          "MaxFrame Python models do not support cluster_by because "
          "MaxCompute has no equivalent BigQuery clustering contract"
      ) %}
  {% endif %}
  {% set contract_config = config.get('contract') %}
  {% if language == 'python' and contract_config.enforced %}
      {% do exceptions.raise_compiler_error(
          "MaxFrame Python models do not support enforced contracts yet"
      ) %}
  {% endif %}


  -- the temp_ and backup_ relations should not already exist in the database; get_relation
  -- will return None in that case. Otherwise, we get a relation that we can drop
  -- later, before we try to use this name for the current operation. This has to happen before
  -- BEGIN, in a separate transaction
  {%- set preexisting_temp_relation = load_cached_relation(temp_relation)-%}
  {%- set preexisting_intermediate_relation = load_cached_relation(intermediate_relation)-%}
  {%- set preexisting_backup_relation = load_cached_relation(backup_relation) -%}
   -- grab current tables grants config for comparision later on
  {% set grant_config = config.get('grants') %}
  {{ drop_relation_if_exists(preexisting_temp_relation) }}
  {{ drop_relation_if_exists(preexisting_intermediate_relation) }}
  {{ drop_relation_if_exists(preexisting_backup_relation) }}

  {#-- Two passes per phase, as in dbt-core: run_hooks() keeps only the hooks #}
  {#-- whose `transaction` flag matches the pass, so calling it once with its #}
  {#-- default argument skipped every `transaction: false` hook -- which is   #}
  {#-- what core's before_begin() and after_commit() helpers produce.         #}
  {#-- MaxCompute has no transactions (connections.begin()/commit() are       #}
  {#-- no-ops), so the two passes pick hooks and fix their order only.        #}
  {{ run_hooks(pre_hooks, inside_transaction=False) }}
  {{ run_hooks(pre_hooks, inside_transaction=True) }}

  {% if existing_relation is none %}
    {% if language == 'python' %}
      {% set stage_suffix = '__dbt_mf_' ~ (invocation_id | replace('-', ''))[:8] %}
      {% set stage_relation = make_temp_relation(target_relation, stage_suffix) %}
      {{ drop_relation_if_exists(load_relation(stage_relation)) }}
      {% call statement('main', language='python') -%}
{{ maxframe_write_table(
    compiled_code,
    stage_relation,
    lifecycle=1,
    add_sentinel=true
).lstrip() }}
      {%- endcall %}
      {% call statement('create_maxframe_relation', language='sql') -%}
        {{ create_table_as_internal(
            false,
            target_relation,
            maxframe_select_without_sentinel(stage_relation),
            is_transactional,
            primary_keys,
            config.get('delta_table_bucket_num', 16),
            partition_by,
            lifecycle,
            tblproperties
        ) }}
      {%- endcall %}
      {{ adapter.drop_relation(stage_relation) }}
    {% else %}
      {%- call statement('main') -%}
        {{ create_table_as_internal(False, target_relation, sql, True, partition_config=partition_by, lifecycle=lifecycle, tblproperties=tblproperties) }}
      {%- endcall -%}
    {% endif %}
  {% elif full_refresh_mode %}
      {% do log("Hard refreshing " ~ existing_relation) %}
      {% if language == 'python' %}
        {% set stage_suffix = '__dbt_mf_' ~ (invocation_id | replace('-', ''))[:8] %}
        {% set stage_relation = make_temp_relation(intermediate_relation, stage_suffix) %}
        {{ drop_relation_if_exists(load_relation(stage_relation)) }}
        {% call statement('main', language='python') -%}
{{ maxframe_write_table(
    compiled_code,
    stage_relation,
    lifecycle=1,
    add_sentinel=true
).lstrip() }}
        {%- endcall %}
        {% call statement('create_maxframe_relation', language='sql') -%}
          {{ create_table_as_internal(
              false,
              intermediate_relation,
              maxframe_select_without_sentinel(stage_relation),
              is_transactional,
              primary_keys,
              config.get('delta_table_bucket_num', 16),
              partition_by,
              lifecycle,
              tblproperties
          ) }}
        {%- endcall %}
        {{ adapter.drop_relation(stage_relation) }}
        {% set existing_relation = load_cached_relation(existing_relation) %}
        {% if existing_relation is not none %}
          {{ adapter.rename_relation(existing_relation, backup_relation) }}
        {% endif %}
        {{ adapter.rename_relation(intermediate_relation, target_relation) }}
        {% set did_python_full_refresh_swap = true %}
      {% else %}
        {{ adapter.drop_relation(existing_relation) }}
        {%- call statement('main') -%}
          {{ create_table_as_internal(False, target_relation, sql, True, partition_config=partition_by, lifecycle=lifecycle, tblproperties=tblproperties) }}
        {%- endcall -%}
      {% endif %}
  {% else %}
    {% set temp_relation_exists = false %}
    {% if language == 'python' or on_schema_change != 'ignore' %}
      {#-- Check first, since otherwise we may not build a temp table --#}
      {#-- Python always needs to create a temp table --#}
      {% if language == 'python' %}
        {#-- Use a short per-invocation name. Reusing a just-dropped stage can --#}
        {#-- hit stale MaxFrame / MaxCompute metadata on the next dbt run.    --#}
        {% set stage_suffix = '__dbt_mf_' ~ (invocation_id | replace('-', ''))[:8] %}
        {% set stage_relation = make_temp_relation(target_relation, stage_suffix) %}
        {{ drop_relation_if_exists(load_relation(stage_relation)) }}
        {% call statement('create_temp_relation_maxframe_stage', language='python') -%}
{{ maxframe_write_table(
    compiled_code,
    stage_relation,
    lifecycle=1,
    add_sentinel=true
).lstrip() }}
        {%- endcall %}
        {% call statement('create_temp_relation', language='sql') -%}
          {{ create_table_as_internal(
              true,
              temp_relation,
              maxframe_select_without_sentinel(stage_relation),
              is_transactional,
              primary_keys,
              config.get('delta_table_bucket_num', 16),
              partition_by,
              1,
              tblproperties
          ) }}
        {%- endcall %}
        {{ adapter.drop_relation(stage_relation) }}
      {% else %}
        {%- call statement('create_temp_relation') -%}
          {{ create_table_as_internal(True, temp_relation, sql, True, partition_config=partition_by, tblproperties=tblproperties) }}
        {%- endcall -%}
      {% endif %}
      {% set temp_relation_exists = true %}
      {#-- Widen the target's declared string columns before anything reads the temp
           relation: MaxCompute keeps the first n characters of a longer value on
           insert instead of failing, so this is the last point where the data can be
           saved. `mc_expand_target_column_types` explains why contract models opt out. --#}
      {% do mc_expand_target_column_types(temp_relation, target_relation) %}
      {#-- Process schema changes. Returns dict of changes if successful. Use source columns for upserting/merging --#}
      {% set dest_columns = process_schema_changes(on_schema_change, temp_relation, existing_relation) %}
    {% endif %}

    {% if not dest_columns %}
      {% set dest_columns = adapter.get_columns_in_relation(existing_relation) %}
    {% endif %}

    {% set build_sql = mc_generate_incremental_build_sql(
        incremental_strategy, temp_relation, target_relation, build_code, unique_key, partition_by, partitions, dest_columns, temp_relation_exists, incremental_predicates, tblproperties
    ) %}

    {#- For dbt-origin strategies (merge / delete+insert / append),               -#}
    {#- mc_generate_incremental_build_sql creates the temp via its own            -#}
    {#- `{% call statement('create_temp_relation') %}` block. Jinja scoping       -#}
    {#- prevents that inner assignment from propagating, so we mark the flag here -#}
    {#- to ensure the post-run cleanup drops it. insert_overwrite and microbatch  -#}
    {#- emit their own `drop table if exists` in the generated SQL.               -#}
    {%- if incremental_strategy not in ('insert_overwrite', 'microbatch') -%}
        {% set temp_relation_exists = true %}
    {%- elif language == 'python' -%}
        {#-- These strategies include `drop table if exists` in build_sql. --#}
        {% set temp_relation_exists = false %}
    {%- endif -%}

    {% call statement("main") %}
      {{ sql_header if sql_header is not none }}
      {{ build_sql }}
    {% endcall %}
  {% endif %}


  {% set should_revoke = should_revoke(existing_relation, full_refresh_mode) %}
  {% do apply_grants(target_relation, grant_config, should_revoke=should_revoke) %}

  {% do persist_docs(target_relation, model) %}

  {{ run_hooks(post_hooks, inside_transaction=True) }}

  {%- if did_python_full_refresh_swap -%}
    {{ drop_relation_if_exists(backup_relation) }}
  {%- endif -%}

  {%- if temp_relation_exists -%}
    {{ adapter.drop_relation(temp_relation) }}
  {%- endif -%}

  {#-- Outside-transaction post hooks run last, after the scratch relations #}
  {#-- are gone -- same position as dbt-core.                              #}
  {{ run_hooks(post_hooks, inside_transaction=False) }}

  {{ return({'relations': [target_relation]}) }}
{%- endmaterialization %}

{% macro mc_generate_incremental_build_sql(
    strategy, temp_relation, target_relation, sql, unique_key, partition_by, partitions, dest_columns, temp_relation_exists, incremental_predicates, tblproperties
) %}
  {% if strategy == 'insert_overwrite' %}
    {% set build_sql = mc_generate_incremental_insert_overwrite_build_sql(
        temp_relation, target_relation, sql, unique_key, partition_by, partitions, dest_columns, temp_relation_exists, tblproperties
    ) %}
  {% elif strategy == 'microbatch' %}
    {% set build_sql = mc_generate_microbatch_build_sql(
        temp_relation, target_relation, sql, unique_key, partition_by, partitions, dest_columns, temp_relation_exists, tblproperties
    ) %}
  {% else %} {# strategy == 'dbt origin' #}
    {%- call statement('create_temp_relation') -%}
      {% if not temp_relation_exists %}
          {{ create_table_as_internal(True, temp_relation, sql, True, partition_config=partition_by, tblproperties=tblproperties) }}
      {% endif %}
    {%- endcall -%}
    {#-- Same widening as above, at the point the strategies that build their own temp
         (merge / delete+insert / append) have one. --#}
    {% do mc_expand_target_column_types(temp_relation, target_relation) %}
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
