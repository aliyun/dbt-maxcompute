{% macro maxcompute__get_merge_sql(target, source, unique_key, dest_columns, incremental_predicates=none) -%}
    {%- set predicates = [] if incremental_predicates is none else [] + incremental_predicates -%}
    {%- set dest_cols_names = get_quoted_list(dest_columns | map(attribute="name")) -%}
    {%- set dest_cols_csv = get_quoted_csv(dest_columns | map(attribute="name")) -%}
    {%- set merge_update_columns = config.get('merge_update_columns') -%}
    {%- set merge_exclude_columns = config.get('merge_exclude_columns') -%}
    {#- For non-auto partitioned targets, default-exclude partition fields from UPDATE: -#}
    {#- updating a partition column moves the row across partitions, which is rarely intended -#}
    {#- and forces extra dynamic-partition work. Users who actually want this can still set    -#}
    {#- merge_update_columns explicitly to override.                                            -#}
    {%- if not merge_update_columns -%}
        {%- set partition_config = adapter.parse_partition_by(config.get('partition_by', none)) -%}
        {%- if partition_config is not none and not partition_config.auto_partition() and partition_config.fields -%}
            {%- set merge_exclude_columns = (merge_exclude_columns or []) + partition_config.fields -%}
        {%- endif -%}
    {%- endif -%}
    {%- set update_columns = get_merge_update_columns(merge_update_columns, merge_exclude_columns, dest_columns) -%}
    {%- set sql_header = config.get('sql_header', none) -%}

    {{ sql_header if sql_header is not none }}
    {% if unique_key %}
        {% if unique_key is sequence and unique_key is not mapping and unique_key is not string %}
            {% for key in unique_key %}
                {% set this_key_match %}
                    DBT_INTERNAL_SOURCE.{{ key }} = DBT_INTERNAL_DEST.{{ key }}
                {% endset %}
                {% do predicates.append(this_key_match) %}
            {% endfor %}
        {% else %}
            {% set unique_key_match %}
                DBT_INTERNAL_SOURCE.{{ unique_key }} = DBT_INTERNAL_DEST.{{ unique_key }}
            {% endset %}
            {% do predicates.append(unique_key_match) %}
        {% endif %}

        merge into {{ target }} as DBT_INTERNAL_DEST
            using {{ source }} as DBT_INTERNAL_SOURCE
            on {{"(" ~ predicates | join(") and (") ~ ")"}}

        when matched then update set
            {% for column_name in update_columns -%}
                DBT_INTERNAL_DEST.{{ column_name }} = DBT_INTERNAL_SOURCE.{{ column_name }}
                {%- if not loop.last %}, {%- endif %}
            {%- endfor %}

        when not matched then insert
            ({{ dest_cols_csv }})
        values (
        {% for column in dest_cols_names %}
            DBT_INTERNAL_SOURCE.{{ column }} {{- ',' if not loop.last -}}
        {% endfor %});

    {% else %}
        INSERT INTO {{ target }} ({{ dest_cols_csv }})
        SELECT {{ dest_cols_csv }}
        FROM {{ source }}
    {% endif %}
{% endmacro %}


{% macro maxcompute__get_delete_insert_merge_sql(target, source, unique_key, dest_columns, incremental_predicates) -%}

    {%- set dest_cols_csv = get_quoted_csv(dest_columns | map(attribute="name")) -%}
    {%- set partition_config = adapter.parse_partition_by(config.get('partition_by', none)) -%}
    {#- non-auto partition needs an explicit PARTITION clause on INSERT; auto- -#}
    {#- partition targets derive the partition value server-side from a data  -#}
    {#- column and dest_columns already excludes the generated column.        -#}
    {%- set use_partition_clause = partition_config is not none and not partition_config.auto_partition() and partition_config.fields -%}

    {% if unique_key %}
        {% if unique_key is sequence and unique_key is not string %}
            {#- MaxCompute DELETE does not support the PostgreSQL `using <src>`  -#}
            {#- form. Use `(k1, k2, ...) in (select k1, k2, ... from src)`.     -#}
            {%- set key_csv = unique_key | join(', ') -%}
            delete from {{ target }}
            where ({{ key_csv }}) in (
                select {{ key_csv }} from {{ source }}
            )
            {%- if incremental_predicates %}
                {% for predicate in incremental_predicates %}
                    and {{ predicate }}
                {% endfor %}
            {%- endif -%};
        {% else %}
            delete from {{ target }}
            where (
                {{ unique_key }}) in (
                select ({{ unique_key }})
                from {{ source }}
            )
            {%- if incremental_predicates %}
                {% for predicate in incremental_predicates %}
                    and {{ predicate }}
                {% endfor %}
            {%- endif -%};

        {% endif %}
    {% endif %}

    {% if use_partition_clause %}
        {%- set partition_fields = partition_config.fields -%}
        {%- set data_columns = dest_columns | rejectattr('name', 'in', partition_fields) | list -%}
        {%- set data_cols_csv = get_quoted_csv(data_columns | map(attribute='name')) -%}
        {%- set partition_cols_csv = get_quoted_csv(partition_fields) -%}
    {#- MaxCompute dynamic-partition INSERT: no data column list; SELECT must -#}
    {#- emit data cols then partition cols in the same order as the table.    -#}
    insert into {{ target }} partition ({{ partition_cols_csv }})
    select {{ data_cols_csv }}, {{ partition_cols_csv }}
    from {{ source }}
    {% else %}
    insert into {{ target }} ({{ dest_cols_csv }})
    (
        select {{ dest_cols_csv }}
        from {{ source }}
    )
    {% endif %}
{%- endmacro %}


{% macro maxcompute__get_incremental_append_sql(arg_dict) -%}
    {%- set target = arg_dict["target_relation"] -%}
    {%- set source = arg_dict["temp_relation"] -%}
    {%- set dest_columns = arg_dict["dest_columns"] -%}
    {%- set dest_cols_csv = get_quoted_csv(dest_columns | map(attribute="name")) -%}
    {%- set partition_config = adapter.parse_partition_by(config.get('partition_by', none)) -%}
    {#- non-auto partition needs an explicit PARTITION clause; auto-partition  -#}
    {#- derives the value server-side from a data column and dest_columns      -#}
    {#- already excludes the generated column.                                 -#}
    {%- set use_partition_clause = partition_config is not none and not partition_config.auto_partition() and partition_config.fields -%}

    {% if use_partition_clause %}
        {%- set partition_fields = partition_config.fields -%}
        {%- set data_columns = dest_columns | rejectattr('name', 'in', partition_fields) | list -%}
        {%- set data_cols_csv = get_quoted_csv(data_columns | map(attribute='name')) -%}
        {%- set partition_cols_csv = get_quoted_csv(partition_fields) -%}
    insert into {{ target }} partition ({{ partition_cols_csv }})
    select {{ data_cols_csv }}, {{ partition_cols_csv }}
    from {{ source }}
    {% else %}
    insert into {{ target }} ({{ dest_cols_csv }})
    (
        select {{ dest_cols_csv }}
        from {{ source }}
    )
    {% endif %}
{%- endmacro %}


{% macro maxcompute__get_insert_overwrite_merge_sql(target, source, dest_columns, predicates, include_sql_header) -%}
    {#-- The only time include_sql_header is True: --#}
    {#-- BigQuery + insert_overwrite strategy + "static" partitions config --#}
    {#-- We should consider including the sql header at the materialization level instead --#}

    {%- set predicates = [] if predicates is none else [] + predicates -%}
    {%- set dest_cols_names = get_quoted_list(dest_columns | map(attribute="name")) -%}
    {%- set dest_cols_csv = get_quoted_csv(dest_columns | map(attribute="name")) -%}
    {%- set sql_header = config.get('sql_header', none) -%}

    {{ sql_header if sql_header is not none and include_sql_header }}

    {% call statement("main") %}
    {% if predicates %}
    DELETE FROM {{ target }} where True
      AND {{ predicates | join(' AND ') }};
    {% else %}
    TRUNCATE TABLE {{ target }};
    {% endif %}
    {% endcall %}

    INSERT INTO {{ target }} ({{ dest_cols_csv }})
    SELECT {{ dest_cols_csv }}
    FROM {{ source }}
{% endmacro %}
