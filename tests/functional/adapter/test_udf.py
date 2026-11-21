import io
import zipfile
from pathlib import Path

import pytest

from dbt.adapters.contracts.relation import RelationType
from dbt.tests.util import get_connection, run_dbt

LIBRARY_RESOURCE_NAME = "dbt_udf_test_library_cp311.zip"


class TestMaxComputePythonFunctions:
    @pytest.fixture(scope="class")
    def project_config_update(self):
        # dbt Core requires an effective runtime_version for every Python
        # function. Define it once for the project so individual functions can
        # omit the repetitive CPython 3.11 setting.
        return {"functions": {"+runtime_version": "3.11"}}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "direct_call.sql": """
{{ config(materialized='table') }}
select {{ function('double_value') }}(cast(21 as bigint)) as value
""",
            "raw_name_call.sql": """
{{ config(materialized='table') }}
select {{ target.schema }}.double_value(cast(21 as bigint)) as value
""",
            "ephemeral_call.sql": """
{{ config(materialized='ephemeral') }}
select {{ function('double_value') }}(cast(21 as bigint)) as value
""",
            "via_ephemeral.sql": """
{{ config(materialized='table') }}
select * from {{ ref('ephemeral_call') }}
""",
            "runtime_view.sql": """
{{ config(materialized='view') }}
select {{ function('double_value') }}(cast(21 as bigint)) as value
""",
            "via_view.sql": """
{{ config(materialized='table') }}
select * from {{ ref('runtime_view') }}
""",
            "explicit_legacy.sql": """
{{ config(materialized='table', sql_hints={'odps.sql.python.version': 'cp37'}) }}
select {{ function('legacy_mapping') }}(cast(42 as bigint)) as value
""",
        }

    @pytest.fixture(scope="class")
    def functions(self):
        return {
            "double_value.py": """
def main(value):
    match value:
        case None:
            return None
        case _:
            return value * 2
""",
            "double_value.yml": """
functions:
  - name: double_value
    config:
      entry_point: main
      runtime_version: "3.11"
    arguments:
      - name: value
        data_type: bigint
    returns:
      data_type: bigint
""",
            "sum_values.py": """
class SumValues:
    def new_buffer(self):
        return [0]

    def iterate(self, buffer, value):
        if value is not None:
            buffer[0] += value

    def merge(self, buffer, partial):
        buffer[0] += partial[0]

    def terminate(self, buffer):
        return buffer[0]
""",
            "sum_values.yml": """
functions:
  - name: sum_values
    config:
      type: aggregate
      entry_point: SumValues
      runtime_version: cp311
    arguments:
      - name: value
        data_type: bigint
    returns:
      data_type: bigint
""",
            "sum_array.py": """
def main(values):
    return sum(values) if values else 0
""",
            "sum_array.yml": """
functions:
  - name: sum_array
    config:
      entry_point: main
    arguments:
      - name: values
        data_type: array<bigint>
    returns:
      data_type: bigint
""",
            "triple_with_library.py": """
from dbt_udf_test_library import triple


def main(value):
    return triple(value)
""",
            "triple_with_library.yml": f"""
functions:
  - name: triple_with_library
    config:
      entry_point: main
      runtime_version: cp311
      maxcompute:
        python_libraries:
          - {LIBRARY_RESOURCE_NAME}
    arguments:
      - name: value
        data_type: bigint
    returns:
      data_type: bigint
""",
            "legacy_mapping.py": """
import collections

def main(value):
    return value if isinstance({}, collections.Mapping) else 0
""",
            "legacy_mapping.yml": """
functions:
  - name: legacy_mapping
    config:
      entry_point: main
      runtime_version: cp37
    arguments:
      - name: value
        data_type: bigint
    returns:
      data_type: bigint
""",
        }

    @staticmethod
    def create_python_library_resource(project):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, mode="w") as package:
            package.writestr(
                "dbt_udf_test_library.py",
                "def triple(value):\n    return value * 3\n",
            )
        archive.seek(0)

        with get_connection(project.adapter):
            project.adapter.get_odps_client().create_resource(
                LIBRARY_RESOURCE_NAME,
                "archive",
                project=project.database,
                schema=project.test_schema,
                fileobj=archive,
            )

    def test_scalar_and_aggregate_python_functions(self, project):
        self.create_python_library_resource(project)
        results = run_dbt(["build", "--select", "resource_type:function"])
        assert len(results) == 5

        scalar = run_dbt(
            ["show", "--inline", "select {{ function('double_value') }}(21) as value"]
        )
        assert scalar.results[0].agate_table.rows[0]["value"] == 42

        null_scalar = run_dbt(
            [
                "show",
                "--inline",
                "select {{ function('double_value') }}(cast(null as bigint)) as value",
            ]
        )
        assert null_scalar.results[0].agate_table.rows[0]["value"] is None

        array_scalar = run_dbt(
            [
                "show",
                "--inline",
                """
select {{ function('sum_array') }}(
    array(cast(1 as bigint), cast(2 as bigint), cast(3 as bigint))
) as value
""",
            ]
        )
        assert array_scalar.results[0].agate_table.rows[0]["value"] == 6

        library_scalar = run_dbt(
            [
                "show",
                "--inline",
                "select {{ function('triple_with_library') }}(7) as value",
            ]
        )
        assert library_scalar.results[0].agate_table.rows[0]["value"] == 21

        aggregate = run_dbt(
            [
                "show",
                "--inline",
                """
with digits as (
    select cast(0 as bigint) as digit union all
    select cast(1 as bigint) union all
    select cast(2 as bigint) union all
    select cast(3 as bigint) union all
    select cast(4 as bigint) union all
    select cast(5 as bigint) union all
    select cast(6 as bigint) union all
    select cast(7 as bigint) union all
    select cast(8 as bigint) union all
    select cast(9 as bigint)
)
select {{ function('sum_values') }}(
    d0.digit
    + 10 * d1.digit
    + 100 * d2.digit
    + 1000 * d3.digit
    + 10000 * d4.digit
    + 1
) as value
from digits d0
cross join digits d1
cross join digits d2
cross join digits d3
cross join digits d4
""",
            ]
        )
        assert aggregate.results[0].agate_table.rows[0]["value"] == 5_000_050_000

        run_dbt(["run"])
        for model in (
            "direct_call",
            "raw_name_call",
            "via_ephemeral",
            "via_view",
            "explicit_legacy",
        ):
            result = project.run_sql(f"select value from {{schema}}.{model}", fetch="one")
            assert result[0] == 42

        function_path = Path(project.project_root) / "functions" / "double_value.py"
        function_path.write_text("def main(value):\n    return value * 3\n", encoding="utf-8")
        updated = run_dbt(["build", "--select", "double_value"])
        assert len(updated) == 1

        updated_scalar = run_dbt(
            ["show", "--inline", "select {{ function('double_value') }}(21) as value"]
        )
        assert updated_scalar.results[0].agate_table.rows[0]["value"] == 63

        with get_connection(project.adapter):
            adapter = project.adapter
            odps_client = adapter.get_odps_client()
            function = odps_client.get_function(
                "double_value", project=project.database, schema=project.test_schema
            )
            function.reload()
            managed_resource_names = [resource.name for resource in function.resources]
            relation = adapter.Relation.create(
                database=project.database,
                schema=project.test_schema,
                identifier="double_value",
                type=RelationType.Function,
            )
            adapter.drop_relation(relation)
            assert not odps_client.exist_function(
                "double_value", project=project.database, schema=project.test_schema
            )
            assert all(
                not odps_client.exist_resource(
                    resource_name,
                    project=project.database,
                    schema=project.test_schema,
                )
                for resource_name in managed_resource_names
            )

        rebuilt = run_dbt(["build", "--select", "double_value"])
        assert len(rebuilt) == 1


class TestLegacyPythonRuntime:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "legacy_call.sql": """
{{ config(materialized='table', sql_hints={'odps.sql.python.version': 'cp27'}) }}
select {{ target.schema }}.legacy_runtime(cast(21 as bigint)) as value
""",
            "explicit_runtime_with_legacy_hook.sql": """
{{ config(
    materialized='table',
    sql_hints={'odps.sql.python.version': 'cp311'},
    pre_hook='set odps.sql.python.version=cp27; select ' ~ target.schema ~ '.legacy_runtime(cast(21 as bigint))'
) }}
select cast(1 as bigint) as value
""",
        }

    def test_explicit_runtime_preserves_legacy_python_functions_and_hooks(self, project):
        source = b"""from odps.udf import annotate

@annotate('bigint->bigint')
class LegacyRuntime(object):
    def evaluate(self, value):
        return long(value) * 2
"""
        with get_connection(project.adapter):
            client = project.adapter.get_odps_client()
            resource = client.create_resource(
                "legacy_runtime.py",
                "py",
                schema=project.test_schema,
                fileobj=io.BytesIO(source),
            )
            try:
                client.create_function(
                    "legacy_runtime",
                    class_type="legacy_runtime.LegacyRuntime",
                    resources=[resource],
                    schema=project.test_schema,
                )
                run_dbt(["run"])
                result = project.run_sql("select value from {schema}.legacy_call", fetch="one")
                assert result[0] == 42
            finally:
                if client.exist_function("legacy_runtime", schema=project.test_schema):
                    client.delete_function("legacy_runtime", schema=project.test_schema)
                client.delete_resource("legacy_runtime.py", schema=project.test_schema)
