{% macro import_semantic_package(name, revision, output, table_prefix, time_spine_start, time_spine_end,
                                  project_name='semantic_import', maxc='maxc', config=none,
                                  namespace=none, user_agent=none, entity_names=none, time_mapping=none,
                                  primary_keys=none, totals_only=none, unkeyed_models=none) %}
  {% if execute %}
    {% set receipt = adapter.import_semantic_package(
      name=name, revision=revision, output=output, table_prefix=table_prefix,
      time_spine_start=time_spine_start, time_spine_end=time_spine_end,
      project_name=project_name, maxc=maxc, config=config, namespace=namespace,
      user_agent=user_agent, entity_names=entity_names, time_mapping=time_mapping,
      primary_keys=primary_keys, totals_only=totals_only, unkeyed_models=unkeyed_models
    ) %}
    {% do log(tojson(receipt), info=true) %}
    {{ return(receipt) }}
  {% endif %}
{% endmacro %}
