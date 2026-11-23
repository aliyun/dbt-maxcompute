{%- macro maxcompute__support_multiple_grantees_per_dcl_statement() -%}
    {{ return(False) }}
{%- endmacro -%}


{% macro maxcompute__get_show_grant_sql(relation) %}
    show grants on {{ relation.render() }}
{% endmacro %}


{%- macro maxcompute__get_grant_sql(relation, privilege, grantees) -%}
    grant {{ privilege }} on table {{ relation.render() }} to USER {{ grantees | join(', ') }}
{%- endmacro -%}


{%- macro maxcompute__get_revoke_sql(relation, privilege, grantees) -%}
    revoke {{ privilege }} on table {{ relation.render() }} from USER {{ grantees | join(', ') }}
{%- endmacro -%}



{% macro maxcompute__call_dcl_statements(dcl_statement_list) %}
    {#
      -- By default, supply all grant + revoke statements in a single semicolon-separated block,
      -- so that they're all processed together.

      -- Some databases do not support this. Those adapters will need to override this macro
      -- to run each statement individually.
    #}
    {% for dcl_statement in dcl_statement_list %}
        {{ adapter.run_security_sql(dcl_statement) }}
    {% endfor %}
{% endmacro %}


{% macro maxcompute__apply_grants(relation, grant_config, should_revoke=True) %}
    {% if not adapter.is_external_catalog_project(relation.database) %}
        {{ adapter.get_odps_table_by_relation(relation, 10) }}
    {% endif %}
    {% if grant_config %}
        {% if should_revoke %}
            {#-- We think previous grants may have carried over --#}
            {#-- Show current grants and calculate diffs --#}
            {% set current_grants_dict = adapter.run_security_sql(get_show_grant_sql(relation)) %}
            {% set needs_granting = diff_of_two_dicts(grant_config, current_grants_dict) %}
            {% set needs_revoking = diff_of_two_dicts(current_grants_dict, grant_config) %}
            {% if not (needs_granting or needs_revoking) %}
                {{ log('On ' ~ relation.render() ~': All grants are in place, no revocation or granting needed.')}}
            {% endif %}
        {% else %}
            {#-- We don't think there's any chance of previous grants having carried over. --#}
            {#-- Jump straight to granting what the user has configured. --#}
            {% set needs_revoking = {} %}
            {% set needs_granting = grant_config %}
        {% endif %}
        {% if needs_granting or needs_revoking %}
            {% set revoke_statement_list = get_dcl_statement_list(relation, needs_revoking, get_revoke_sql) %}
            {% set grant_statement_list = get_dcl_statement_list(relation, needs_granting, get_grant_sql) %}
            {% set dcl_statement_list = revoke_statement_list + grant_statement_list %}
            {% if dcl_statement_list %}
                {{ call_dcl_statements(dcl_statement_list) }}
            {% endif %}
        {% endif %}
    {% endif %}
{% endmacro %}
