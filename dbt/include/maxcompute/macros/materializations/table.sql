{% materialization table, adapter='maxcompute', supported_languages=['sql', 'python'] %}

  {% if adapter.is_external_catalog_project(this.database) %}
    {% do return(mc_epv2_create_relation(this.incorporate(type='table'), compiled_code)) %}
  {% endif %}

  {%- set language = model['language'] -%}
  {%- set lifecycle = config.get('lifecycle', none) -%}
  {%- set tblproperties = config.get('tblproperties', none) -%}
  {%- set partition_config = adapter.parse_partition_by(config.get('partition_by', none)) -%}
  {%- set primary_keys = config.get('primary_keys', none) -%}
  {%- set is_transactional = config.get('transactional') or config.get('delta') -%}
  {%- set existing_relation = load_cached_relation(this) -%}
  {%- set target_relation = this.incorporate(type='table') -%}
  {%- set intermediate_relation = make_intermediate_relation(target_relation) -%}
  {%- set preexisting_intermediate_relation = load_cached_relation(intermediate_relation) -%}
  {%- set backup_relation_type = 'table' if existing_relation is none else existing_relation.type -%}
  {%- set backup_relation = make_backup_relation(target_relation, backup_relation_type) -%}
  {%- set preexisting_backup_relation = load_cached_relation(backup_relation) -%}
  {%- set grant_config = config.get('grants') -%}

  {{ drop_relation_if_exists(preexisting_intermediate_relation) }}
  {{ drop_relation_if_exists(preexisting_backup_relation) }}

  {{ run_hooks(pre_hooks, inside_transaction=False) }}
  {{ run_hooks(pre_hooks, inside_transaction=True) }}

  {% if language == 'python' %}
    {% if config.get('cluster_by') %}
      {% do exceptions.raise_compiler_error(
          "MaxFrame Python models do not support cluster_by because "
          "MaxCompute has no equivalent BigQuery clustering contract"
      ) %}
    {% endif %}
    {% set contract_config = config.get('contract') %}
    {% if contract_config.enforced %}
      {% do exceptions.raise_compiler_error(
          "MaxFrame Python models do not support enforced contracts yet"
      ) %}
    {% endif %}

    {#-- MaxFrame 2.8 cannot execute a sink whose result is empty. Write a  --#}
    {#-- type-preserving sentinel to a non-partitioned stage, then filter it --#}
    {#-- out in MaxCompute SQL while applying final table properties.        --#}
    {% set stage_suffix = '__dbt_mf_' ~ (invocation_id | replace('-', ''))[:8] %}
    {% set stage_relation = make_temp_relation(intermediate_relation, stage_suffix) %}
    {% set preexisting_stage_relation = load_relation(stage_relation) %}
    {{ drop_relation_if_exists(preexisting_stage_relation) }}

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
          partition_config,
          lifecycle,
          tblproperties
      ) }}
    {%- endcall %}
    {{ adapter.drop_relation(stage_relation) }}
  {% else %}
    {% call statement('main', language=language) -%}
      {{ create_table_as(False, intermediate_relation, compiled_code, language) }}
    {%- endcall %}
  {% endif %}

  {% do create_indexes(intermediate_relation) %}

  {% if existing_relation is not none %}
    {% set existing_relation = load_cached_relation(existing_relation) %}
    {% if existing_relation is not none %}
      {{ adapter.rename_relation(existing_relation, backup_relation) }}
    {% endif %}
  {% endif %}

  {{ adapter.rename_relation(intermediate_relation, target_relation) }}

  {{ run_hooks(post_hooks, inside_transaction=True) }}

  {% set should_revoke = should_revoke(existing_relation, full_refresh_mode=True) %}
  {% do apply_grants(target_relation, grant_config, should_revoke=should_revoke) %}
  {% do persist_docs(target_relation, model) %}

  {{ adapter.commit() }}

  {{ drop_relation_if_exists(backup_relation) }}
  {{ run_hooks(post_hooks, inside_transaction=False) }}

  {{ return({'relations': [target_relation]}) }}
{% endmaterialization %}
