{% macro maxcompute__create_table_as(temporary, relation, sql) -%}
    {%- set is_transactional = config.get('transactional') or config.get('delta') -%}
    {%- set primary_keys = config.get('primary_keys') -%}
    {%- set delta_table_bucket_num = config.get('delta_table_bucket_num', 16)-%}
    {%- set raw_partition_by = config.get('partition_by', none) -%}
    {%- set lifecycle = config.get('lifecycle', none) -%}
    {%- set partition_config = adapter.parse_partition_by(raw_partition_by) -%}
    {{ create_table_as_internal(temporary, relation, sql, is_transactional, primary_keys, delta_table_bucket_num, partition_config, lifecycle) }}
{%- endmacro %}


{% macro create_table_as_internal(temporary, relation, sql, is_transactional, primary_keys=none, delta_table_bucket_num=16, partition_config=none, lifecycle=none) -%}
    {%- set sql_header = config.get('sql_header', none) -%}
    {%- set is_delta = is_transactional and primary_keys is not none and primary_keys|length > 0 -%}

    {% call statement('create_table', auto_begin=False) -%}
        {{ sql_header if sql_header is not none }}
        create table {{ relation.render() }} (
            {% set contract_config = config.get('contract') %}
            {% if contract_config.enforced and (not temporary) %}
                {{ get_assert_columns_equivalent(sql) }}
                {{ get_table_columns_and_constraints_without_brackets(partition_config) }}
                {%- set sql = get_select_subquery(sql) %}
            {%- else -%}
                {{ get_table_columns(sql, primary_keys, partition_config) }}
            {%- endif -%}
            {% if is_delta -%}
                ,primary key(
                {%- for pk in primary_keys -%}
                    {{ pk }}{{ "," if not loop.last }}
                {%- endfor -%})
            {%- endif -%}
            )
            {% if partition_config -%}
                {{ partition_by(partition_config) }}
            {%- endif -%}
            {%- if is_transactional -%}
                {%- if is_delta -%}
                    tblproperties("transactional"="true", "write.bucket.num"="{{ delta_table_bucket_num }}")
                {%- else -%}
                    tblproperties("transactional"="true")
                {%- endif -%}
            {%- endif -%}
            {%- if lifecycle %}
                LIFECYCLE {{ lifecycle }}
            {%- elif temporary %}
                LIFECYCLE 1
            {%- endif %}
            ;
    {%- endcall -%}
    {{ sql_header if sql_header is not none }}
    insert into {{ relation.render() }}
    {% if partition_config and partition_config.fields|length > 0 and not partition_config.auto_partition() -%}
        partition({{ partition_config.render(False) }})
    {%- endif -%}
    (
    {% for c in get_column_schema_from_query(sql) -%}
        `{{ c.name }}`{{ "," if not loop.last }}
    {% endfor %}
    )(
        {{ sql }}
    );
{%- endmacro %}


{% macro get_table_columns(sql, primary_keys=none, partition_config=None) -%}
    {% set model_columns = model.columns %}
    {% set partition_by_cols = [] if (partition_config is none or partition_config.auto_partition()) else partition_config.fields %}
    {% set ns = namespace(needs_comma=false) %}  {# 初始化命名空间变量 #}

    {% for c in get_column_schema_from_query(sql) -%}
    {% if c.name not in partition_by_cols -%}
        {{- "," if ns.needs_comma -}}  {# 根据命名空间变量判断逗号 #}
        {{ c.name }} {{ c.dtype }}
        {% if primary_keys and c.name in primary_keys %}not null{% endif %}
        {% if model_columns and c.name in model_columns %}  {# 从模型配置中读取约束 #}
           {% for constraint in model_columns[c.name].constraints %}
               {% if constraint.type == 'not_null' %}
                   {% if not primary_keys or c.name not in primary_keys %}
                      not null   {# 避免重复增加 not null #}
                   {% endif %}
               {% endif %}
           {% endfor %}
           {{ "COMMENT" }} {{ quote_and_escape(model_columns[c.name].description) }}
        {%- endif %}
        {% set ns.needs_comma = true %}  {# 标记后续列需要逗号 #}
    {%- endif %}
    {% endfor %}
{%- endmacro %}

{% macro quote_and_escape(input_string) %}
    {% set escaped_string = input_string | replace("'", "\\'") %}
    '{{ escaped_string }}'
{% endmacro %}

-- Compared to get_table_columns_and_constraints, only the surrounding brackets are deleted
{% macro get_table_columns_and_constraints_without_brackets(partition_config=None) -%}
    {# loop through user_provided_columns to create DDL with data types and constraints #}
    {%- set raw_column_constraints = adapter.mc_render_raw_columns_constraints(raw_columns=model['columns'], partition_config=partition_config) -%}
    {%- set raw_model_constraints = adapter.render_raw_model_constraints(raw_constraints=model['constraints']) -%}
    {% for c in raw_column_constraints -%}
      {{ c }}{{ "," if not loop.last or raw_model_constraints }}
    {% endfor %}
    {% for c in raw_model_constraints -%}
        {{ c }}{{ "," if not loop.last }}
    {% endfor -%}
{%- endmacro %}
