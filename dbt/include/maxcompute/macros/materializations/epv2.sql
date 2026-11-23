{% macro mc_epv2_create_relation(target, sql) %}
  {% if model['language'] != 'sql' %}
    {% do exceptions.raise_compiler_error('EP v2 materializations require a SQL model.') %}
  {% endif %}
  {% if load_relation(target) is not none %}
    {% do exceptions.raise_compiler_error('EP v2 table and view models can only create new relations. ' ~ target ~ ' already exists.') %}
  {% endif %}

  {% set header = mc_epv2_write_header() %}
  {{ run_hooks(pre_hooks, inside_transaction=False) }}
  {{ run_hooks(pre_hooks, inside_transaction=True) }}

  {% if target.is_view %}
    {% call statement('main', auto_begin=False) %}
      {{ maxcompute__create_view_as(target, sql, replace=false, sql_header=header) }}
    {% endcall %}
  {% else %}
    {% set partition_by = adapter.parse_partition_by(config.get('partition_by')) %}
    {{ mc_epv2_create_table(target, sql, partition_by, header) }}
    {% set columns = adapter.get_columns_in_relation(target) %}
    {% set partition_columns = columns | selectattr('is_partition') | map(attribute='name') | list %}
    {% set partition_spec = get_quoted_csv(partition_columns) if partition_columns else none %}
    {{ mc_epv2_overwrite(target, sql, header, columns | map(attribute='name') | list, partition_spec) }}
  {% endif %}

  {{ mc_epv2_finish(target) }}
  {% do return({'relations': [target]}) %}
{% endmacro %}


{% macro mc_epv2_incremental(target, sql) %}
  {% if model['language'] != 'sql' %}
    {% do exceptions.raise_compiler_error('EP v2 materializations require a SQL model.') %}
  {% endif %}
  {% if config.get('incremental_strategy') != 'insert_overwrite' %}
    {% do exceptions.raise_compiler_error('EP v2 incremental models require incremental_strategy="insert_overwrite".') %}
  {% endif %}
  {% if flags.FULL_REFRESH or should_full_refresh() %}
    {% do exceptions.raise_compiler_error('EP v2 incremental models do not support full refresh.') %}
  {% endif %}
  {% if config.get('partitions') %}
    {% do exceptions.raise_compiler_error('Specify a single complete partition with partition_values for EP v2.') %}
  {% endif %}
  {% if (config.get('on_schema_change') or 'ignore') != 'ignore' %}
    {% do exceptions.raise_compiler_error('EP v2 incremental models require on_schema_change="ignore".') %}
  {% endif %}

  {% set partition_by = adapter.parse_partition_by(config.get('partition_by')) %}
  {% set values = adapter.epv2_partition_values(partition_by, config.get('partition_values')) %}
  {% set header = mc_epv2_write_header() %}

  {{ run_hooks(pre_hooks, inside_transaction=False) }}
  {{ run_hooks(pre_hooks, inside_transaction=True) }}

  {% if load_relation(target) is none %}
    {{ mc_epv2_create_table(target, sql, partition_by, header) }}
  {% endif %}
  {% set columns, assignments = adapter.epv2_new_partition_write(target, values) %}
  {{ mc_epv2_overwrite(target, sql, header, columns, assignments | join(', '), assignments | join(' and ')) }}

  {{ mc_epv2_finish(target) }}
  {% do return({'relations': [target]}) %}
{% endmacro %}


{% macro mc_epv2_create_table(target, sql, partition_by, header) %}
  {% call statement('create_table', auto_begin=False) %}
    {{ mc_create_table_ddl(target, sql, partition_by, config.get('lifecycle'), config.get('tblproperties'), header,
                           is_transactional=config.get('transactional') or config.get('delta'),
                           primary_keys=config.get('primary_keys'), delta_table_bucket_num=config.get('delta_table_bucket_num', 16)) }}
  {% endcall %}
{% endmacro %}


{% macro mc_epv2_overwrite(target, sql, header, columns, partition_spec, predicate=none) %}
  {% call statement('main', auto_begin=False) %}
    {{ header }}
    insert overwrite table {{ target }} {% if partition_spec %}partition ({{ partition_spec }}){% endif %}
    select {{ get_quoted_csv(columns) }}
    from (
      {{ sql }}
    ) as dbt_epv2_source
    {% if predicate %}where {{ predicate }}{% endif %}
  {% endcall %}
{% endmacro %}


{% macro mc_epv2_write_header() %}
  set dbt.retry=false;
  {{ merge_sql_hints_and_header(config.get('sql_hints'), config.get('sql_header')) }}
{% endmacro %}


{% macro mc_epv2_finish(target) %}
  {% do apply_grants(target, config.get('grants'), should_revoke=false) %}
  {% do persist_docs(target, model) %}
  {{ run_hooks(post_hooks, inside_transaction=True) }}
  {{ adapter.commit() }}
  {{ run_hooks(post_hooks, inside_transaction=False) }}
{% endmacro %}
