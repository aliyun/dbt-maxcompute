-- only change varchar to string, dbt-adapters/dbt/include/global_project/macros/materializations/snapshots/strategies.sql
{% macro maxcompute__snapshot_hash_arguments(args) -%}
    md5({%- for arg in args -%}
        coalesce(cast({{ arg }} as string), '')
        {% if not loop.last %} || '|' || {% endif %}
    {%- endfor -%})
{%- endmacro %}


-- dbt-adapters/dbt/include/global_project/macros/materializations/snapshots/strategies.sql
{% macro maxcompute__snapshot_string_as_time(timestamp) -%}
    {%- set result = "to_timestamp('" ~ timestamp ~ "')" -%}
    {{ return(result) }}
{%- endmacro %}


{% macro build_snapshot_staging_table(strategy, sql, target_relation) %}
    {% set temp_relation = make_temp_relation(target_relation) %}

    {% set select = snapshot_staging_table(strategy, sql, target_relation) %}

    {% call statement('build_snapshot_staging_relation') %}
        {{ create_table_as_internal(True, temp_relation, select, True) }}
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
     and DBT_INTERNAL_DEST.{{ columns.dbt_valid_to }} is null
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
      {% set final_sql = create_table_as_internal(False, target_relation, build_sql, True) %}

  {% else %}

      {% set columns = config.get("snapshot_table_column_names") or get_snapshot_table_column_names() %}

      {{ adapter.valid_snapshot_target(target_relation, columns) }}

      {% set build_or_select_sql = snapshot_staging_table(strategy, sql, target_relation) %}
      {% set staging_table = build_snapshot_staging_table(strategy, sql, target_relation) %}

      -- this may no-op if the database does not require column expansion
      {% do adapter.expand_target_column_types(from_relation=staging_table,
                                               to_relation=target_relation) %}

      {% set missing_columns = adapter.get_missing_columns(staging_table, target_relation)
                                   | rejectattr('name', 'equalto', 'dbt_change_type')
                                   | rejectattr('name', 'equalto', 'DBT_CHANGE_TYPE')
                                   | rejectattr('name', 'equalto', 'dbt_unique_key')
                                   | rejectattr('name', 'equalto', 'DBT_UNIQUE_KEY')
                                   | list %}

      {% do create_columns(target_relation, missing_columns) %}

      {% set source_columns = adapter.get_columns_in_relation(staging_table)
                                   | rejectattr('name', 'equalto', 'dbt_change_type')
                                   | rejectattr('name', 'equalto', 'DBT_CHANGE_TYPE')
                                   | rejectattr('name', 'equalto', 'dbt_unique_key')
                                   | rejectattr('name', 'equalto', 'DBT_UNIQUE_KEY')
                                   | list %}

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
