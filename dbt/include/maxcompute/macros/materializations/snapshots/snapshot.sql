-- only change varchar to string, dbt-adapters/dbt/include/global_project/macros/materializations/snapshots/strategies.sql
{% macro maxcompute__snapshot_hash_arguments(args) -%}
    md5({%- for arg in args -%}
        coalesce(cast({{ arg }} as string), '')
        {% if not loop.last %} || '|' || {% endif %}
    {%- endfor -%})
{%- endmacro %}


-- dbt-adapters/dbt/include/global_project/macros/materializations/snapshots/strategies.sql
{#- Measured: `to_timestamp('<x>')` does not exist on MaxCompute - the server answers
    "ODPS-0130221 ... function to_timestamp needs at least 2, at most 3 parameters,
    actually have 1" - so a custom snapshot strategy that rendered a timestamp through
    this macro got an unusable statement.  An explicit cast is the form the live suite
    already uses elsewhere, and it accepts both '2024-01-01' and a full
    '2024-01-01 00:00:00'. -#}
{% macro maxcompute__snapshot_string_as_time(timestamp) -%}
    {#- `timestamp` arrives unquoted (it is the macro argument), so the literal has to
        be re-quoted here or the cast would be applied to an arithmetic expression. -#}
    {%- set literal = "'" ~ timestamp ~ "'" -%}
    {%- if (timestamp | length) == 10 -%}
        {#- date-only literal: MaxCompute's cast wants a full timestamp -#}
        {%- set literal = "'" ~ timestamp ~ " 00:00:00'" -%}
    {%- endif -%}
    {%- set result = "cast(" ~ literal ~ " as timestamp)" -%}
    {{ return(result) }}
{%- endmacro %}


{% macro build_snapshot_staging_table(strategy, sql, target_relation, tblproperties) %}
    {% set temp_relation = make_temp_relation(target_relation) %}

    {% set select = snapshot_staging_table(strategy, sql, target_relation) %}

    {% call statement('build_snapshot_staging_relation') %}
        {{ create_table_as_internal(True, temp_relation, select, True, tblproperties=tblproperties) }}
    {% endcall %}

    {% do return(temp_relation) %}
{% endmacro %}


-- dbt-adapters/dbt/include/global_project/macros/materializations/snapshots/helper.sql
{% macro maxcompute__post_snapshot(staging_relation) %}
    {% do adapter.drop_relation(staging_relation) %}
{% endmacro %}


-- dbt-adapters/dbt/include/global_project/macros/materializations/snapshots/helper.sql
-- The original method of adding 1 column at a time in a loop has been changed to adding all columns at once.
{% macro maxcompute__create_columns(relation, columns) %}
    {% if columns|length > 0 %}
    {% call statement() %}
      alter table {{ relation.render() }} add columns (
        {% for column in columns %}
          `{{ column.name }}` {{ column.data_type }} {{- ',' if not loop.last -}}
        {% endfor %}
      );
    {% endcall %}
    {% endif %}
{% endmacro %}


{% macro maxcompute__snapshot_merge_sql(target, source, insert_cols) -%}
    {%- set insert_cols_csv = insert_cols | join(', ') -%}

    {%- set columns = config.get("snapshot_table_column_names") or get_snapshot_table_column_names() -%}

    merge into {{ target.render() }} as DBT_INTERNAL_DEST
    using {{ source }} as DBT_INTERNAL_SOURCE
    on DBT_INTERNAL_SOURCE.{{ columns.dbt_scd_id }} = DBT_INTERNAL_DEST.{{ columns.dbt_scd_id }}

    when matched
     {%- if config.get("dbt_valid_to_current") %}
     {#- With `dbt_valid_to_current` the live version is marked by that value, not
         by NULL. dbt-core's default merge honours it; this override has to as
         well, otherwise nothing ever matches and the expired version stays live
         next to the new one (measured: after one update, id 1 had 2 current
         versions and nothing was closed out). -#}
     {%- set dest_valid_to = ("DBT_INTERNAL_DEST." ~ columns.dbt_valid_to) | trim %}
     {%- set current_value = config.get("dbt_valid_to_current") | trim %}
     and ( {{ equals(dest_valid_to, current_value) }} or {{ dest_valid_to }} is null )
     {%- else %}
     and DBT_INTERNAL_DEST.{{ columns.dbt_valid_to }} is null
     {%- endif %}
     and DBT_INTERNAL_SOURCE.dbt_change_type in ('update', 'delete')
        then update
        set DBT_INTERNAL_DEST.{{ columns.dbt_valid_to }} = DBT_INTERNAL_SOURCE.{{ columns.dbt_valid_to }}

    when not matched
     and DBT_INTERNAL_SOURCE.dbt_change_type = 'insert'
        then insert ({{ insert_cols_csv }})
        values (
        {% for column in insert_cols %}
           DBT_INTERNAL_SOURCE.{{ column }} {{- ',' if not loop.last -}}
        {% endfor %});

{% endmacro %}

-- dbt-adapters/dbt/include/global_project/macros/materializations/snapshots/snapshot.sql
-- Create the snapshot table as a transactional table to support merge operations
{% materialization snapshot, adapter='maxcompute' %}

  {%- set target_table = model.get('alias', model.get('name')) -%}

  {%- set strategy_name = config.get('strategy') -%}
  {%- set unique_key = config.get('unique_key') %}
  -- grab current tables grants config for comparision later on
  {%- set grant_config = config.get('grants') -%}
  {%- set tblproperties = config.get('tblproperties', none) -%}

  {#- Config keys the snapshot materialization does not apply.  Say so instead
      of staying quiet: the snapshot table dbt creates is always an
      unpartitioned, transactional, primary-key-free table, because closing out
      an expired version is a merge and a second version of the same unique key
      must be allowed to coexist with the first. -#}
  {%- set ignored_configs = [] -%}
  {%- if config.get('partition_by') is not none -%}
    {%- do ignored_configs.append('partition_by (a snapshot table is never partitioned: the merge that expires a version writes whole rows)') -%}
  {%- endif -%}
  {%- if config.get('primary_keys') or config.get('delta') -%}
    {%- do ignored_configs.append('primary_keys/delta (a primary key would not allow the current and the expired version of one unique key side by side)') -%}
  {%- endif -%}
  {%- if config.get('transactional') is not none and not config.get('transactional') -%}
    {%- do ignored_configs.append('transactional=false (a snapshot table has to be transactional to merge)') -%}
  {%- endif -%}
  {%- if config.get('lifecycle') is not none -%}
    {%- do ignored_configs.append('lifecycle (not applied to a snapshot table: expiring rows out of a history table would delete history)') -%}
  {%- endif -%}
  {%- if ignored_configs | length > 0 -%}
    {% do exceptions.warn(
        "Snapshot '" ~ model.name ~ "' sets " ~ (ignored_configs | join('; '))
        ~ "; the MaxCompute snapshot materialization does not apply them. "
        ~ "See docs/snapshot-support.md."
    ) %}
  {%- endif -%}

  {% set target_relation_exists, target_relation = get_or_create_relation(
          database=model.database,
          schema=model.schema,
          identifier=target_table,
          type='table') -%}

  {%- if not target_relation.is_table -%}
    {% do exceptions.relation_wrong_type(target_relation, 'table') %}
  {%- endif -%}


  {{ run_hooks(pre_hooks, inside_transaction=False) }}

  {{ run_hooks(pre_hooks, inside_transaction=True) }}

  {% set strategy_macro = strategy_dispatch(strategy_name) %}
  {# The model['config'] parameter below is no longer used, but passing anyway for compatibility #}
  {# It was a dictionary of config, instead of the config object from the context #}
  {% set strategy = strategy_macro(model, "snapshotted_data", "source_data", model['config'], target_relation_exists) %}

  {% if not target_relation_exists %}

      {% set build_sql = build_snapshot_table(strategy, model['compiled_code']) %}
      {% set build_or_select_sql = build_sql %}
      {% set final_sql = create_table_as_internal(False, target_relation, build_sql, True, tblproperties=tblproperties) %}

  {% else %}

      {% set columns = config.get("snapshot_table_column_names") or get_snapshot_table_column_names() %}

      {#- dbt-core's newer entry point: it runs the adapter's own
          `valid_snapshot_target` (including the MaxCompute shape checks below) and
          then the strategy-specific one - a `hard_deletes='new_record'` snapshot
          against a table without `dbt_is_deleted` used to reach the server and come
          back as six copies of "column snapshotted_data.dbt_is_deleted cannot be
          resolved" (measured).  Core refuses that up front, by name. -#}
      {{ adapter.assert_valid_snapshot_target_given_strategy(target_relation, columns, strategy) }}

      {% set build_or_select_sql = snapshot_staging_table(strategy, sql, target_relation) %}
      {% set staging_table = build_snapshot_staging_table(strategy, sql, target_relation, tblproperties) %}

      -- this may no-op if the database does not require column expansion
      {% do adapter.expand_target_column_types(from_relation=staging_table,
                                               to_relation=target_relation) %}

      {#- The staging query's own helper columns must never become snapshot
          columns: with a list `unique_key` they arrive as dbt_unique_key_1/2,
          and a snapshot table that holds them makes the *next* staging query
          ambiguous (each later run re-aliases the same names over `select *`).
          `equalto` cannot express that, so filter by name shape here. -#}
      {% set missing_columns = [] %}
      {% for column in adapter.get_missing_columns(staging_table, target_relation) %}
        {% set column_name = column.name | lower %}
        {% if column_name != 'dbt_change_type' and not column_name.startswith('dbt_unique_key') %}
          {% do missing_columns.append(column) %}
        {% endif %}
      {% endfor %}

      {% do create_columns(target_relation, missing_columns) %}

      {% set source_columns = [] %}
      {% for column in adapter.get_columns_in_relation(staging_table) %}
        {% set column_name = column.name | lower %}
        {% if column_name != 'dbt_change_type' and not column_name.startswith('dbt_unique_key') %}
          {% do source_columns.append(column) %}
        {% endif %}
      {% endfor %}

      {% set quoted_source_columns = [] %}
      {% for column in source_columns %}
        {% do quoted_source_columns.append(adapter.quote(column.name)) %}
      {% endfor %}

      {% set final_sql = snapshot_merge_sql(
            target = target_relation,
            source = staging_table,
            insert_cols = quoted_source_columns
         )
      %}

  {% endif %}


  {{ check_time_data_types(build_or_select_sql) }}

  {% call statement('main') %}
      {{ final_sql }}
  {% endcall %}

  {% set should_revoke = should_revoke(target_relation_exists, full_refresh_mode=False) %}
  {% do apply_grants(target_relation, grant_config, should_revoke=should_revoke) %}

  {% do persist_docs(target_relation, model) %}

  {% if not target_relation_exists %}
    {% do create_indexes(target_relation) %}
  {% endif %}

  {{ run_hooks(post_hooks, inside_transaction=True) }}

  {{ adapter.commit() }}

  {% if staging_table is defined %}
      {% do post_snapshot(staging_table) %}
  {% endif %}

  {{ run_hooks(post_hooks, inside_transaction=False) }}

  {{ return({'relations': [target_relation]}) }}

{% endmaterialization %}
