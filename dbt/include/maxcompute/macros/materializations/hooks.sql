{#-- Copy of dbt-core's run_hooks minus the literal `commit;` it emits before #}
{#-- the first outside-transaction hook. MaxCompute's SQL parser rejects a #}
{#-- bare `commit;` (verified 2026-09-26: ParseError, while `select 1 as x` on #}
{#-- the same connection succeeds), and this adapter's begin()/commit() are #}
{#-- no-ops, so there is nothing to close. Everything else must stay in step #}
{#-- with core -- in particular the `transaction` filter, which is why the #}
{#-- materializations below call run_hooks() twice per phase. #}
{% macro run_hooks(hooks, inside_transaction=True) %}
  {% for hook in hooks | selectattr('transaction', 'equalto', inside_transaction)  %}
    {% set rendered = render(hook.get('sql')) | trim %}
    {% if (rendered | length) > 0 %}
      {% call statement(auto_begin=inside_transaction) %}
        {{ rendered }}
      {% endcall %}
    {% endif %}
  {% endfor %}
{% endmacro %}
