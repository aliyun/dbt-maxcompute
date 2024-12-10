{% macro maxcompute__create_table_as(temporary, relation, sql) -%}
    {%- set is_transactional = config.get('transactional') or config.get('delta') -%}
    {%- set primary_keys = config.get('primary_keys') -%}
    {%- set delta_table_bucket_num = config.get('delta_table_bucket_num', 16)-%}
    {{ create_table_as_internal(temporary, relation, sql, is_transactional, primary_keys, delta_table_bucket_num) }}
{%- endmacro %}


{% macro create_table_as_internal(temporary, relation, sql, is_transactional, primary_keys=none, delta_table_bucket_num=16, partition_by=none) -%}
    {%- set sql_header = config.get('sql_header', none) -%}
    {{ sql_header if sql_header is not none }}
    {%- set is_delta = is_transactional and primary_keys is not none and primary_keys|length > 0 -%}

    {% call statement('create_table', auto_begin=False) -%}
        create table {{ relation.render() }} (
            {% set contract_config = config.get('contract') %}
            {% if contract_config.enforced and (not temporary) %}
                {{ get_assert_columns_equivalent(sql) }}
                {{ get_table_columns_and_constraints_without_brackets() }}
                {%- set sql = get_select_subquery(sql) %}
            {%- else -%}
                {{ get_table_columns(sql, primary_keys) }}
            {%- endif -%}
            {% if is_delta -%}
                ,primary key(
                {%- for pk in primary_keys -%}
                    {{ pk }}{{ "," if not loop.last }}
                {%- endfor -%})
            {%- endif -%}
            )
            {% if partition_by -%}
                {%- if partition_by.auto_partition -%}
                auto partitioned by (trunc_time(`{{ partition_by.field }}`, "{{ partition_by.granularity }}"))
                {%- else -%}
                partitioned by (`{{ partition_by.field }}`)
                {%- endif -%}
            {%- endif -%}
            {%- if is_transactional -%}
                {%- if is_delta -%}
                    tblproperties("transactional"="true", "write.bucket.num"="{{ delta_table_bucket_num }}")
                {%- else -%}
                    tblproperties("transactional"="true")
                {%- endif -%}
            {%- endif -%}
            {%- if temporary %}
                LIFECYCLE 1
            {%- endif %}
            ;
    {%- endcall -%}

    insert into {{ relation.render() }} (
    {% for c in get_column_schema_from_query(sql) -%}
        `{{ c.name }}`{{ "," if not loop.last }}
    {% endfor %}
    )(
        {{ sql }}
    );
{%- endmacro %}


{% macro get_table_columns(sql, primary_keys=none) -%}
    {% set model_columns = model.columns %}
    {% for c in get_column_schema_from_query(sql) -%}
    {{ c.name }} {{ c.dtype }}
    {% if primary_keys and c.name in  primary_keys -%}not null{%- endif %}
    {% if model_columns and c.name in  model_columns -%}
       {{ "COMMENT" }} {{ quote_and_escape(model_columns[c.name].description) }}
    {%- endif %}
    {{ "," if not loop.last }}
    {% endfor %}
{%- endmacro %}

{% macro quote_and_escape(input_string) %}
    {% set escaped_string = input_string | replace("'", "\\'") %}
    '{{ escaped_string }}'
{% endmacro %}

-- Compared to get_table_columns_and_constraints, only the surrounding brackets are deleted
{% macro get_table_columns_and_constraints_without_brackets() -%}
    {# loop through user_provided_columns to create DDL with data types and constraints #}
    {%- set raw_column_constraints = adapter.render_raw_columns_constraints(raw_columns=model['columns']) -%}
    {%- set raw_model_constraints = adapter.render_raw_model_constraints(raw_constraints=model['constraints']) -%}
    {% for c in raw_column_constraints -%}
      {{ c }}{{ "," if not loop.last or raw_model_constraints }}
    {% endfor %}
    {% for c in raw_model_constraints -%}
        {{ c }}{{ "," if not loop.last }}
    {% endfor -%}
{%- endmacro %}
