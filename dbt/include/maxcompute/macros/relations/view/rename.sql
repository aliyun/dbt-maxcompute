{%- macro maxcompute__get_rename_view_sql(relation, new_name) -%}
    alter view {{ relation }} rename to {{ quote_ref(new_name) }}
{%- endmacro -%}
