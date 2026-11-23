{% materialization view, adapter='maxcompute' %}
  {% if adapter.is_external_catalog_project(this.database) %}
    {% do return(mc_epv2_create_relation(this.incorporate(type='view'), sql)) %}
  {% endif %}

  {% do return(dbt.materialization_view_default()) %}
{% endmaterialization %}
