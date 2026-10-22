{% macro maxcompute__deploy_python_function(target_relation, function_type) %}
    {% if model.get('overloads', []) %}
        {% do exceptions.raise_compiler_error(
            "dbt-maxcompute does not support function overloads. "
            ~ "Define each signature as a separately named function; "
            ~ "overloads would overwrite the same MaxCompute function."
        ) %}
    {% endif %}

    {% for argument in model.arguments %}
        {% if argument.get('default_value', none) is not none %}
            {% do exceptions.raise_compiler_error(
                "MaxCompute Python UDFs do not support default argument values; "
                ~ "remove default_value from argument '" ~ argument.name ~ "'"
            ) %}
        {% endif %}
    {% endfor %}

    {% if config.get('grants') %}
        {% do exceptions.raise_compiler_error(
            "Function grants are not implemented by dbt-maxcompute. Apply MaxCompute "
            ~ "function permissions outside this materialization."
        ) %}
    {% endif %}

    {% if config.get('volatility') is not none %}
        {% do unsupported_volatility_warning(config.get('volatility')) %}
    {% endif %}

    {% set persist_docs_config = config.get('persist_docs', {}) %}
    {% if persist_docs_config and persist_docs_config.get('relation') %}
        {% do exceptions.warn(
            "MaxCompute functions do not expose a persisted description field; "
            ~ "persist_docs.relation is ignored for function '" ~ model.name ~ "'"
        ) %}
    {% endif %}

    {% set maxcompute_config = config.get('maxcompute', {}) or {} %}
    {% if maxcompute_config is not mapping %}
        {% do exceptions.raise_compiler_error(
            "The function config.maxcompute value must be a mapping."
        ) %}
    {% endif %}

    {% set argument_types = [] %}
    {% for argument in model.arguments %}
        {% do argument_types.append(argument.data_type) %}
    {% endfor %}

    {% set deployment = adapter.create_or_update_python_udf(
        target_relation,
        model.compiled_code,
        argument_types,
        model.returns.data_type,
        config.get('entry_point'),
        config.get('runtime_version'),
        function_type,
        config.get('packages', []),
        maxcompute_config.get('resources', []),
        maxcompute_config.get('python_libraries', [])
    ) %}

    {{ return(
        "-- MaxCompute Python " ~ function_type ~ " UDF "
        ~ deployment['action'] ~ ": " ~ target_relation.render()
        ~ " (resource " ~ deployment['resource_name'] ~ ")"
    ) }}
{% endmacro %}


{% macro maxcompute__scalar_function_python(target_relation) %}
    {{ return(maxcompute__deploy_python_function(target_relation, 'scalar')) }}
{% endmacro %}


{% macro maxcompute__aggregate_function_python(target_relation) %}
    {{ return(maxcompute__deploy_python_function(target_relation, 'aggregate')) }}
{% endmacro %}


{% macro maxcompute__table_function_python(target_relation) %}
    {% do exceptions.raise_compiler_error(
        "MaxCompute supports Python UDTFs, but dbt Core does not currently provide "
        ~ "a supported table function resource contract. Use type: scalar or "
        ~ "type: aggregate."
    ) %}
{% endmacro %}


{% macro maxcompute__scalar_function_sql(target_relation) %}
    {% do exceptions.raise_compiler_error(
        "SQL function resources are not implemented by dbt-maxcompute yet. "
        ~ "Use a Python function resource."
    ) %}
{% endmacro %}


{% macro maxcompute__aggregate_function_sql(target_relation) %}
    {% do exceptions.raise_compiler_error(
        "SQL aggregate function resources are not implemented by dbt-maxcompute. "
        ~ "Use a Python aggregate function resource."
    ) %}
{% endmacro %}


{% macro maxcompute__function_execute_build_sql(build_sql, existing_relation, target_relation) %}
    {% if model.language != 'python' %}
        {% do exceptions.raise_compiler_error(
            "dbt-maxcompute currently implements function resources for Python only."
        ) %}
    {% endif %}
    {% do log(build_sql, info=true) %}
    {% do adapter.commit() %}
    {% do store_raw_result(
        'main',
        message=build_sql,
        code='PYTHON_UDF',
        rows_affected=none
    ) %}
{% endmacro %}
