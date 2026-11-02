"""Live regression for Core 1.12 additions and overload rejection before writes."""

from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.version import Version
from dbt.tests.util import get_connection, relation_from_name, run_dbt, write_file

pytestmark = [
    pytest.mark.integration_smoke,
    pytest.mark.skipif(
        Version(version("dbt-core")) < Version("1.12"), reason="requires dbt Core 1.12"
    ),
]


class TestCore112Features:
    @pytest.fixture(scope="class")
    def seeds(self):
        return {"empty_probe.csv": "id,name\n1,one\n2,two\n"}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "versioned_v1.sql": """
{{ config(materialized='table', latest_version_pointer={'enabled': true}) }}
select cast(7 as bigint) as id
""",
            "schema.yml": """
version: 2
models:
  - name: versioned
    latest_version: 1
    versions:
      - v: 1
        defined_in: versioned_v1
""",
        }

    def test_run_operation_sql(self, project):
        run_dbt(["run-operation", "--sql", "select 1 as value"])

    def test_seed_empty(self, project):
        run_dbt(["seed", "--empty"])
        relation = relation_from_name(project.adapter, "empty_probe")
        assert project.run_sql(f"select count(*) from {relation}", fetch="one")[0] == 0

    def test_latest_version_pointer(self, project):
        run_dbt(["run"])
        pointer = relation_from_name(project.adapter, "versioned")
        assert project.adapter.get_odps_table_by_relation(pointer).is_virtual_view
        assert project.run_sql(f"select id from {pointer}", fetch="one")[0] == 7


FUNCTION_YAML = """
functions:
  - name: guarded_double
    arguments:
      - name: value
        data_type: bigint
    returns:
      data_type: bigint
"""
OVERLOAD_YAML = (
    FUNCTION_YAML
    + """
    overloads:
      - defined_in: guarded_double_float
        arguments:
          - name: value
            data_type: double
        returns:
          data_type: double
"""
)


class TestFunctionOverloadProtection:
    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {"functions": {"+runtime_version": "3.11", "+entry_point": "main"}}

    @pytest.fixture(scope="class")
    def functions(self):
        return {
            "guarded_double.py": "def main(value): return value * 2\n",
            "functions.yml": FUNCTION_YAML,
        }

    @pytest.mark.parametrize("existing", [False, True])
    def test_rejects_before_creating_or_updating_function(self, project, existing):
        # Reset the class-scoped project before the second parameter runs.
        directory = Path(project.project_root) / "functions"
        (directory / "guarded_double_float.py").unlink(missing_ok=True)
        write_file(FUNCTION_YAML, directory, "functions.yml")
        write_file("def main(value): return value * 2\n", directory, "guarded_double.py")
        if existing:
            run_dbt(["build", "--select", "resource_type:function", "--no-partial-parse"])

        with get_connection(project.adapter):
            client = project.adapter.get_odps_client()
            kwargs = {"project": project.database, "schema": project.test_schema}
            before_resources = {resource.name for resource in client.list_resources(**kwargs)}
            before_class = (
                client.get_function("guarded_double", **kwargs).class_type if existing else None
            )

        # Changing the root body detects a guard that runs too late (after root deployment).
        write_file("def main(value): return value * 3\n", directory, "guarded_double.py")
        write_file("def main(value): return value * 4\n", directory, "guarded_double_float.py")
        write_file(OVERLOAD_YAML, directory, "functions.yml")
        results = run_dbt(
            ["build", "--select", "resource_type:function", "--no-partial-parse"],
            expect_pass=False,
        )
        assert len(results) == 1
        assert "does not support function overloads" in results[0].message

        with get_connection(project.adapter):
            client = project.adapter.get_odps_client()
            assert {
                resource.name for resource in client.list_resources(**kwargs)
            } == before_resources
            assert client.exist_function("guarded_double", **kwargs) == existing
            if existing:
                assert client.get_function("guarded_double", **kwargs).class_type == before_class
        if existing:
            result = project.run_sql(
                "select {schema}.guarded_double(cast(21 as bigint))", fetch="one"
            )
            assert result[0] == 42
