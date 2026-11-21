{% macro export_semantic_package(name, timezone, join_type, manifest=none, output=none,
                                  maxc='maxc', config=none, namespace=none, user_agent=none,
                                  overwrite=false, replace_sections=false) %}
  {% if execute %}
    {% set receipt = adapter.export_semantic_package(
      name=name, timezone=timezone, join_type=join_type, manifest=manifest, output=output,
      maxc=maxc, config=config, namespace=namespace, user_agent=user_agent,
      overwrite=overwrite, replace_sections=replace_sections
    ) %}
    {% do log(tojson(receipt), info=true) %}
    {{ return(receipt) }}
  {% endif %}
{% endmacro %}
