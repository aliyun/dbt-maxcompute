{% materialization raw,  adapter='maxcompute' -%}
  {% call statement("main") %}
      {{ sql }}
  {% endcall %}
  {{ return({'relations': []}) }}
{%- endmaterialization %}
