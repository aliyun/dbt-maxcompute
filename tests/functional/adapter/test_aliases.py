import pytest
from dbt.tests.adapter.aliases import fixtures
from dbt.tests.adapter.aliases.test_aliases import (
    BaseAliases,
    BaseAliasErrors,
    BaseSameAliasDifferentSchemas,
    BaseSameAliasDifferentDatabases,
)

# macros #
MACROS__CAST_SQL = """
{% macro string_literal(s) -%}
  {{ adapter.dispatch('string_literal', macro_namespace='test')(s) }}
{%- endmacro %}

{% macro default__string_literal(s) %}
    '{{ s }}'
{% endmacro %}

"""


class TestAliasesMaxCompute(BaseAliases):
    @pytest.fixture(scope="class")
    def macros(self):
        return {
            "cast.sql": MACROS__CAST_SQL,
            "expect_value.sql": fixtures.MACROS__EXPECT_VALUE_SQL,
        }

    pass


class TestAliasErrorsMaxCompute(BaseAliasErrors):
    @pytest.fixture(scope="class")
    def macros(self):
        return {
            "cast.sql": MACROS__CAST_SQL,
            "expect_value.sql": fixtures.MACROS__EXPECT_VALUE_SQL,
        }

    pass


class TestSameAliasDifferentSchemasMaxCompute(BaseSameAliasDifferentSchemas):
    @pytest.fixture(scope="class")
    def macros(self):
        return {
            "cast.sql": MACROS__CAST_SQL,
            "expect_value.sql": fixtures.MACROS__EXPECT_VALUE_SQL,
        }

    pass


@pytest.mark.skip(
    reason="The unstable case is not a problem with dbt-adapter, needs to be solved by server."
)
class TestSameAliasDifferentDatabasesMaxCompute(BaseSameAliasDifferentDatabases):
    @pytest.fixture(scope="class")
    def macros(self):
        return {
            "cast.sql": MACROS__CAST_SQL,
            "expect_value.sql": fixtures.MACROS__EXPECT_VALUE_SQL,
        }

    pass
