{%- macro maxcompute__get_rename_table_sql(relation, new_name) -%}
    alter table {{ relation }} rename to {{ quote_ref(new_name) }}
{%- endmacro -%}
