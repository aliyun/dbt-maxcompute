{% macro maxcompute__create_table_as(temporary, relation, sql, language='sql') -%}
    {%- if language == 'python' -%}
        {%- set submission_method = config.get('submission_method', 'maxframe') -%}
        {%- if submission_method != 'maxframe' -%}
            {% do exceptions.raise_compiler_error(
                "maxcompute__create_table_as received unsupported Python submission method '%s'" % submission_method
            ) %}
        {%- endif -%}
        {%- if config.get('cluster_by') -%}
            {% do exceptions.raise_compiler_error(
                "MaxFrame Python models do not support cluster_by because "
                "MaxCompute has no equivalent BigQuery clustering contract"
            ) %}
        {%- endif -%}
        {%- set partition_config = adapter.parse_partition_by(config.get('partition_by', none)) -%}
        {%- if partition_config is not none and partition_config.auto_partition() -%}
            {% do exceptions.raise_compiler_error(
                "Automatic time partitioning for MaxFrame Python models must be "
                "created through the MaxCompute table or incremental materialization"
            ) %}
        {%- endif -%}
        {%- set contract_config = config.get('contract') -%}
        {%- if contract_config.enforced -%}
            {% do exceptions.raise_compiler_error(
                "MaxFrame Python table models do not support enforced contracts yet"
            ) %}
        {%- endif -%}
{{ maxframe_write_table(
    sql,
    relation,
    partition_config=partition_config,
    lifecycle=config.get('lifecycle', none),
    tblproperties=config.get('tblproperties', none),
    primary_keys=config.get('primary_keys', none),
    is_transactional=config.get('transactional') or config.get('delta')
) }}
    {%- elif language == 'sql' -%}
    {%- set is_transactional = config.get('transactional') or config.get('delta') -%}
    {%- set primary_keys = config.get('primary_keys') -%}
    {%- set delta_table_bucket_num = config.get('delta_table_bucket_num', 16)-%}
    {%- set raw_partition_by = config.get('partition_by', none) -%}
    {%- set lifecycle = config.get('lifecycle', none) -%}
    {%- set tblproperties = config.get('tblproperties', none) -%}
    {%- set partition_config = adapter.parse_partition_by(raw_partition_by) -%}
    {{ create_table_as_internal(temporary, relation, sql, is_transactional, primary_keys, delta_table_bucket_num, partition_config, lifecycle, tblproperties) }}
    {%- else -%}
        {% do exceptions.raise_compiler_error(
            "maxcompute__create_table_as received unsupported language '%s'" % language
        ) %}
    {%- endif -%}
{%- endmacro %}


{% macro maxframe_write_table(
    compiled_code,
    target_relation,
    partition_config=none,
    lifecycle=none,
    tblproperties=none,
    primary_keys=none,
    is_transactional=false,
    add_sentinel=false
) -%}
import maxframe.dataframe as md
from maxframe.dataframe.core import DATAFRAME_TYPE

_dbt_maxframe_target_relation = {{ (target_relation.without_quote() | string) | tojson }}

{{ compiled_code }}

def _dbt_maxframe_load_relation(relation_name):
    # dbt renders MaxCompute relations with backticks, while PyODPS expects
    # unquoted project.schema.table components.
    # Partition values are part of a dbt relation's row shape. MaxFrame omits
    # them by default, which makes a downstream Python model unable to group,
    # filter, or join on a partition produced by an upstream model.
    unquoted_relation = relation_name.replace("`", "")
    relation_table = odps_entry.get_table(unquoted_relation)
    relation_table.reload()
    return md.read_odps_table(
        relation_table,
        odps_entry=odps_entry,
        append_partitions=bool(relation_table.table_schema.partitions),
    )

dbt = dbtObj(_dbt_maxframe_load_relation)
{% set _dbt_microbatch_event_time = config.get('event_time', none) %}
{% set _dbt_microbatch_start = config.get('__dbt_internal_microbatch_event_time_start', none) %}
{% set _dbt_microbatch_end = config.get('__dbt_internal_microbatch_event_time_end', none) %}
{% if _dbt_microbatch_event_time and _dbt_microbatch_start and _dbt_microbatch_end %}
import pandas as _dbt_pandas

{% endif %}

def _dbt_maxframe_build_dataframe(session):
    dataframe = model(dbt, session)
    if not isinstance(dataframe, DATAFRAME_TYPE):
        raise TypeError(
            f"{type(dataframe)} is not a supported type for dbt MaxFrame materialization; "
            "model() must return a MaxFrame DataFrame"
        )
{% if _dbt_microbatch_event_time and _dbt_microbatch_start and _dbt_microbatch_end %}
    event_time_start = _dbt_pandas.Timestamp(
        {{ _dbt_microbatch_start.isoformat() | tojson }}
    ).tz_localize(None)
    event_time_end = _dbt_pandas.Timestamp(
        {{ _dbt_microbatch_end.isoformat() | tojson }}
    ).tz_localize(None)
    event_time_series = dataframe[
        {{ _dbt_microbatch_event_time | tojson }}
    ].astype("datetime64[ns]")
    dataframe = dataframe[
        (event_time_series >= event_time_start)
        & (event_time_series < event_time_end)
    ]
{% endif %}
    return dataframe

df = _dbt_maxframe_build_dataframe(maxframe_session)

_dbt_maxframe_table_properties = dict({{ tblproperties or {} }})
{% if is_transactional %}
_dbt_maxframe_table_properties["transactional"] = "true"
{% endif %}
{% if add_sentinel %}
df = _dbt_maxframe_with_sentinel(df, _dbt_maxframe_target_relation)
{% endif %}

_dbt_maxframe_sink = md.to_odps_table(
    df,
    _dbt_maxframe_target_relation,
    overwrite=True,
    index=False,
    {%- if partition_config is not none and partition_config.fields %}
    partition_col={{ partition_config.fields }},
    {%- endif %}
    {%- if lifecycle is not none %}
    lifecycle={{ lifecycle }},
    {%- endif %}
    {%- if tblproperties or is_transactional %}
    table_properties=_dbt_maxframe_table_properties,
    {%- endif %}
    {%- if primary_keys %}
    primary_key={{ primary_keys }},
    {%- endif %}
)
_dbt_maxframe_execute(_dbt_maxframe_sink)
{%- endmacro %}


{% macro maxframe_select_without_sentinel(stage_relation) -%}
    {%- set sentinel_column = '__dbt_maxframe_sentinel' -%}
    {%- set stage_columns = adapter.get_columns_in_relation(stage_relation) -%}
    {%- set data_columns = [] -%}
    {%- set found_sentinel = namespace(value=false) -%}
    {%- for column in stage_columns -%}
        {%- if column.name | lower == sentinel_column -%}
            {%- set found_sentinel.value = true -%}
        {%- else -%}
            {%- do data_columns.append(adapter.quote(column.name)) -%}
        {%- endif -%}
    {%- endfor -%}
    {%- if not found_sentinel.value -%}
        {% do exceptions.raise_compiler_error(
            "MaxFrame staging relation is missing the internal sentinel column"
        ) %}
    {%- endif -%}
    select {{ data_columns | join(', ') }}
    from {{ stage_relation }}
    where {{ adapter.quote(sentinel_column) }} = false
{%- endmacro %}


{% macro create_table_as_internal(temporary, relation, sql, is_transactional, primary_keys=none, delta_table_bucket_num=16, partition_config=none, lifecycle=none, tblproperties=none) -%}
    {%- set sql_hints = config.get('sql_hints', none) -%}
    {%- set sql_header = merge_sql_hints_and_header(sql_hints, config.get('sql_header', none)) -%}

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
                {{ get_table_columns(sql, primary_keys, partition_config, sql_header) }}
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
            {% set extra_props = {} %}
            {% if tblproperties %}
                {% do extra_props.update(tblproperties) %}
            {% endif %}
            {% if is_transactional %}
                {% do extra_props.update({"transactional": "true"}) %}
                {% if is_delta %}
                    {% do extra_props.update({"write.bucket.num": delta_table_bucket_num}) %}
                {% endif %}
            {% endif %}
            {% if extra_props %}
                tblproperties(
                    {% for key, value in extra_props.items() %}
                        "{{ key }}"="{{ value }}"{{ "," if not loop.last }}
                    {% endfor %}
                )
            {% endif %}
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
    {% for c in get_column_schema_from_query(sql, sql_header) -%}
        `{{ c.name }}`{{ "," if not loop.last }}
    {% endfor %}
    )(
        {{ sql }}
    );
{%- endmacro %}


{% macro get_table_columns(sql, primary_keys=none, partition_config=None, sql_header=None) -%}
    {% set model_columns = model.columns %}
    {% set partition_by_cols = [] if (partition_config is none or partition_config.auto_partition()) else partition_config.fields %}
    {% set ns = namespace(needs_comma=false) %}  {# 初始化命名空间变量 #}

    {% for c in get_column_schema_from_query(sql, sql_header) -%}
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
           {#-- 注释只在用户打开 persist_docs.columns 时进 DDL。dbt 的契约是"描述要不要
                落到仓库"由 persist_docs 决定；以前无条件写入，会让没开开关的表也带上
                注释，上游 docs generate 用例因此一直对不上。 --#}
           {% set column_comment = model_columns[c.name].description %}
           {% if config.persist_column_docs() and column_comment %}
              COMMENT {{ adapter.quote_string_literal(column_comment) }}
           {% endif %}
        {%- endif %}
        {% set ns.needs_comma = true %}  {# 标记后续列需要逗号 #}
    {%- endif %}
    {% endfor %}
{%- endmacro %}

{% macro quote_and_escape(input_string) %}
    {#-- 转义规则只留一份：由适配器实现（含反斜杠本身），这里只做兼容转发。 --#}
    {{ adapter.quote_string_literal(input_string) }}
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

{% macro merge_sql_hints_and_header(sql_hints=None, sql_header=None) -%}
    {%- set parts = [] -%}
    {%- if sql_hints -%}
        {%- for key, value in sql_hints.items() -%}
            {%- do parts.append('set ' ~ key ~ '=' ~ value ~ ';') -%}
        {%- endfor -%}
    {%- endif -%}
    {%- if sql_header -%}
        {%- do parts.append(sql_header) -%}
    {%- endif -%}
    {{- parts | join('\n') | trim -}}
{%- endmacro -%}
