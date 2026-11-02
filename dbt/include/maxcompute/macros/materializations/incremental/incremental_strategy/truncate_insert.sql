{% macro mc_truncate_insert_uses_builtin(context) -%}
    {#-- Custom strategies could already use this name. Apply the new checks
         only when both dbt override points resolve to our own macros. --#}
    {% set entry = context.get('get_incremental_truncate_insert_sql') %}
    {% if entry is none or entry.macro is undefined or entry.macro.unique_id != 'macro.dbt_maxcompute.get_incremental_truncate_insert_sql' %}
        {{ return(false) }}
    {% endif %}
    {% set implementation = adapter.dispatch('get_incremental_truncate_insert_sql', 'dbt') %}
    {{ return(implementation.macro is defined and implementation.macro.unique_id == 'macro.dbt_maxcompute.maxcompute__get_incremental_truncate_insert_sql') }}
{%- endmacro %}

{% macro mc_validate_truncate_insert_config(partition_by, partitions, predicates, existing_relation, full_refresh_mode) -%}
    {% if partition_by is not none or partitions %}
        {% do exceptions.raise_compiler_error(
            "The 'truncate_insert' strategy only supports non-partitioned tables; "
            "remove partition_by/partitions or use insert_overwrite for partitioned tables."
        ) %}
    {% endif %}
    {% if predicates %}
        {% do exceptions.raise_compiler_error(
            "The 'truncate_insert' strategy replaces the entire table and does not "
            "support predicates/incremental_predicates."
        ) %}
    {% endif %}
    {#-- Full refresh replaces the old relation; only validate an overwrite target. --#}
    {% if existing_relation is not none and not full_refresh_mode %}
        {% if not existing_relation.is_table %}
            {% do exceptions.raise_compiler_error(
                "The 'truncate_insert' strategy requires an existing table; "
                "it will not replace a view or external relation."
            ) %}
        {% endif %}
        {% set table = adapter.get_odps_table_by_relation(existing_relation) %}
        {% if table is none %}
            {% do exceptions.raise_compiler_error(
                "Cannot read the 'truncate_insert' target metadata to verify it is non-partitioned."
            ) %}
        {% endif %}
        {#-- External ODPS tables are also mapped to dbt's ordinary table type. --#}
        {% if table.type.value == 'EXTERNAL_TABLE' %}
            {% do exceptions.raise_compiler_error(
                "The 'truncate_insert' strategy does not support external target tables."
            ) %}
        {% endif %}
        {% if table.table_schema.partitions %}
            {% do exceptions.raise_compiler_error(
                "The 'truncate_insert' target is partitioned; use insert_overwrite instead."
            ) %}
        {% endif %}
    {% endif %}
{%- endmacro %}

{% macro get_incremental_truncate_insert_sql(arg_dict) -%}
    {{ return(adapter.dispatch('get_incremental_truncate_insert_sql', 'dbt')(arg_dict)) }}
{%- endmacro %}

{% macro maxcompute__get_incremental_truncate_insert_sql(arg_dict) -%}
    {#-- OVERWRITE maps by position and cannot take a destination column list.
         Read the target order after on_schema_change, which may return source
         columns in a different order. Only the temp table is later dropped. --#}
    {% set target = arg_dict['target_relation'] %}
    {% set columns = adapter.get_columns_in_relation(target) %}
    {% set column_names = [] %}
    {% for column in columns %}
        {% do column_names.append(quote_ref(column.name) | trim) %}
    {% endfor %}
    insert overwrite table {{ target }}
    select {{ column_names | join(', ') }}
    from {{ arg_dict['temp_relation'] }}
{%- endmacro %}
