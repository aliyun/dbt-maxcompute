import json
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional

import pytest
from dbt.cli.main import dbtRunner
from dbt.clients.jinja import MacroGenerator
from dbt_common.exceptions import CompilationError, DbtRuntimeError, MacroReturn
from odps.udf import BaseUDAF

from dbt.adapters.maxcompute.python_udfs import (
    MANAGED_RESOURCE_COMMENT_PREFIX,
    PYTHON_UDF_HANDLER,
    MaxComputePythonUDFManager,
    MaxComputePythonUDFSpec,
    build_python_udf_deployment,
)


def make_spec(**updates):
    values = {
        "function_name": "price_for_xlarge",
        "project": "analytics",
        "schema": "udfs",
        "compiled_code": "def main(price):\n    return price * 2\n",
        "argument_types": ("double",),
        "return_type": "double",
        "entry_point": "main",
        "runtime_version": "3.11",
    }
    values.update(updates)
    return MaxComputePythonUDFSpec(**values)


def load_handler(spec):
    deployment = build_python_udf_deployment(spec)
    namespace = {}
    exec(compile(deployment.source, deployment.resource_name, "exec"), namespace)
    return deployment, namespace[PYTHON_UDF_HANDLER]


def test_builds_scalar_wrapper_for_standard_dbt_function():
    deployment, handler = load_handler(make_spec())

    assert deployment.signature == "double->double"
    assert deployment.class_type.endswith(f".{PYTHON_UDF_HANDLER}")
    assert handler().evaluate(12.5) == 25


def test_scalar_class_entry_point_is_supported():
    spec = make_spec(
        compiled_code=(
            "class Price:\n"
            "    def __init__(self):\n"
            "        self.multiplier = 3\n"
            "    def evaluate(self, price):\n"
            "        return price * self.multiplier\n"
        ),
        entry_point="Price",
    )

    _, handler = load_handler(spec)

    assert handler().evaluate(4) == 12


def test_type_aliases_help_bigquery_migrations():
    deployment = build_python_udf_deployment(
        make_spec(
            compiled_code="def main(left, right):\n    return left + right\n",
            argument_types=("INT64", "FLOAT64"),
            return_type="NUMERIC",
        )
    )

    assert deployment.signature == "bigint,double->decimal"


def test_complex_types_are_preserved_in_annotation():
    deployment = build_python_udf_deployment(
        make_spec(
            compiled_code="def main(value):\n    return value\n",
            argument_types=("ARRAY<INT64>",),
            return_type="STRUCT<int64:STRING, y:FLOAT64>",
        )
    )

    assert deployment.signature == ("array<bigint>->struct<int64:string, y:double>")


def test_python_library_setup_follows_future_imports():
    deployment = build_python_udf_deployment(
        make_spec(
            compiled_code=(
                '"""module docs"""\n'
                "from __future__ import annotations\n"
                "def main(value):\n"
                "    return value\n"
            ),
            python_libraries=("numpy-cp311.zip",),
        )
    )

    assert deployment.source.index("from __future__ import annotations") < (
        deployment.source.index("_dbt_sys.path.insert")
    )


@pytest.mark.parametrize("runtime", ["3.12", "python-3.12", ""])
def test_rejects_unsupported_python_runtime(runtime):
    with pytest.raises(DbtRuntimeError, match="runtime_version=.*unsupported"):
        build_python_udf_deployment(make_spec(runtime_version=runtime))


@pytest.mark.parametrize("runtime", ["3.11", "cp311", "python-3.11"])
def test_accepts_python311_runtime_aliases(runtime):
    deployment = build_python_udf_deployment(make_spec(runtime_version=runtime))

    assert deployment.runtime_version == "cp311"


def test_python311_accepts_modern_syntax():
    deployment, handler = load_handler(
        make_spec(
            compiled_code=(
                "def main(value):\n" "    if (result := value * 2):\n" "        return result\n"
            )
        )
    )

    assert deployment.runtime_version == "cp311"
    assert handler().evaluate(2) == 4


def test_python37_compatibility_mode_rejects_newer_syntax():
    with pytest.raises(DbtRuntimeError, match="not valid.*CPython 3.7.3"):
        build_python_udf_deployment(
            make_spec(
                runtime_version="cp37",
                compiled_code=(
                    "def main(value):\n"
                    "    if (result := value * 2):\n"
                    "        return result\n"
                ),
            )
        )


def test_rejects_missing_entry_point():
    with pytest.raises(DbtRuntimeError, match="was not found"):
        build_python_udf_deployment(make_spec(entry_point="missing"))


def test_rejects_entry_point_that_conflicts_with_generated_symbols():
    with pytest.raises(DbtRuntimeError, match="conflicts with a generated"):
        build_python_udf_deployment(
            make_spec(
                compiled_code="def annotate(value):\n    return value\n",
                entry_point="annotate",
            )
        )


def test_rejects_scalar_arity_mismatch():
    with pytest.raises(DbtRuntimeError, match="YAML declares 1"):
        build_python_udf_deployment(
            make_spec(compiled_code="def main(left, right):\n    return left + right\n")
        )


def test_rejects_dynamic_dbt_packages():
    with pytest.raises(DbtRuntimeError, match="cannot be installed dynamically"):
        build_python_udf_deployment(make_spec(packages=("numpy",)))


def test_builds_native_maxcompute_udaf():
    spec = make_spec(
        function_name="sum_values",
        compiled_code=(
            "class SumValues:\n"
            "    def new_buffer(self):\n"
            "        return [0]\n"
            "    def iterate(self, buffer, value):\n"
            "        buffer[0] += value\n"
            "    def merge(self, buffer, partial):\n"
            "        buffer[0] += partial[0]\n"
            "    def terminate(self, buffer):\n"
            "        return buffer[0]\n"
        ),
        entry_point="SumValues",
        function_type="aggregate",
        argument_types=("bigint",),
        return_type="bigint",
    )

    deployment, handler = load_handler(spec)
    instance = handler()
    buffer = instance.new_buffer()
    instance.iterate(buffer, 2)
    instance.iterate(buffer, 3)

    assert deployment.signature == "bigint->bigint"
    assert f"class {PYTHON_UDF_HANDLER}(BaseUDAF):" in deployment.source
    assert issubclass(handler, BaseUDAF)
    assert instance.terminate(buffer) == 5


def test_rejects_non_native_aggregate_contract():
    with pytest.raises(DbtRuntimeError, match="missing required BaseUDAF methods"):
        build_python_udf_deployment(
            make_spec(
                compiled_code=(
                    "class SumValues:\n"
                    "    def accumulate(self, value):\n"
                    "        pass\n"
                    "    def finish(self):\n"
                    "        return 0\n"
                ),
                entry_point="SumValues",
                function_type="aggregate",
            )
        )


def test_rejects_aggregate_iterate_arity_mismatch():
    with pytest.raises(DbtRuntimeError, match="YAML declares 2"):
        build_python_udf_deployment(
            make_spec(
                compiled_code=(
                    "class SumValues:\n"
                    "    def new_buffer(self):\n"
                    "        return [0]\n"
                    "    def iterate(self, buffer, left, right):\n"
                    "        buffer[0] += left + right\n"
                    "    def merge(self, buffer, partial):\n"
                    "        buffer[0] += partial[0]\n"
                    "    def terminate(self, buffer):\n"
                    "        return buffer[0]\n"
                ),
                entry_point="SumValues",
                function_type="aggregate",
            )
        )


def test_rejects_table_functions_until_dbt_contract_exists():
    with pytest.raises(DbtRuntimeError, match="supported Python function types"):
        build_python_udf_deployment(make_spec(function_type="table"))


class FakeResource:
    def __init__(self, name, comment=None, source=None):
        self.name = name
        self.comment = comment
        self.source = source

    def reload(self):
        return self


class FakeFunction:
    def __init__(self, name, class_type, resources, update_failure=None):
        self.name = name
        self._persisted_class_type = class_type
        self._persisted_resources = list(resources)
        self.class_type = class_type
        self.resources = list(resources)
        self.update_failure = update_failure

    def reload(self):
        self.class_type = self._persisted_class_type
        self.resources = list(self._persisted_resources)
        return self

    def update(self):
        if self.update_failure == "before":
            raise RuntimeError("update failed before persistence")
        self._persisted_class_type = self.class_type
        self._persisted_resources = list(self.resources)
        if self.update_failure == "after":
            raise RuntimeError("response failed after persistence")


class FakeODPS:
    def __init__(self):
        self.resources: Dict[str, FakeResource] = {}
        self.functions: Dict[str, FakeFunction] = {}
        self.deleted_resources: List[str] = []
        self.update_failure: Optional[str] = None

    def exist_resource(self, name, project=None, schema=None):
        return name in self.resources

    def get_resource(self, name, project=None, schema=None):
        return self.resources[name]

    def create_resource(
        self,
        name,
        resource_type,
        project=None,
        schema=None,
        fileobj=None,
        comment=None,
    ):
        assert resource_type == "py"
        resource = FakeResource(name, comment, fileobj.read().decode("utf-8"))
        self.resources[name] = resource
        return resource

    def delete_resource(self, name, project=None, schema=None):
        self.deleted_resources.append(name)
        self.resources.pop(name, None)

    def list_resources(self, project=None, prefix=None, owner=None, schema=None):
        return (
            resource
            for name, resource in self.resources.items()
            if prefix is None or name.startswith(prefix)
        )

    def exist_function(self, name, project=None, schema=None):
        return name in self.functions

    def get_function(self, name, project=None, schema=None):
        return self.functions[name]

    def create_function(self, name, project=None, schema=None, class_type=None, resources=None):
        function = FakeFunction(name, class_type, resources, self.update_failure)
        self.functions[name] = function
        return function

    def delete_function(self, name, project=None, schema=None):
        self.functions.pop(name, None)


def persisted_resource_names(function: FakeFunction) -> Iterable[str]:
    return [resource.name for resource in function._persisted_resources]


def test_manager_creates_function_and_reuses_content_addressed_resource():
    odps = FakeODPS()
    manager = MaxComputePythonUDFManager(odps)
    spec = make_spec()

    first = manager.deploy(spec)
    second = manager.deploy(spec)

    assert first["action"] == "created"
    assert second["action"] == "updated"
    assert list(odps.resources) == [first["resource_name"]]
    assert persisted_resource_names(odps.functions[spec.function_name]) == [first["resource_name"]]


def test_manager_updates_function_before_deleting_old_managed_resource():
    odps = FakeODPS()
    manager = MaxComputePythonUDFManager(odps)
    first = manager.deploy(make_spec())

    second_spec = make_spec(compiled_code="def main(price):\n    return price * 3\n")
    second = manager.deploy(second_spec)

    assert second["resource_name"] != first["resource_name"]
    assert first["resource_name"] in odps.deleted_resources
    assert persisted_resource_names(odps.functions[second_spec.function_name]) == [
        second["resource_name"]
    ]


def test_manager_attaches_but_never_deletes_user_resources():
    odps = FakeODPS()
    odps.resources["lookup.txt"] = FakeResource("lookup.txt", comment="user owned")
    odps.resources["numpy.zip"] = FakeResource("numpy.zip", comment="user owned")
    manager = MaxComputePythonUDFManager(odps)
    spec = make_spec(resources=("lookup.txt",), python_libraries=("numpy.zip",))

    result = manager.deploy(spec)
    manager.drop(spec.function_name, spec.project, spec.schema)

    assert "lookup.txt" in odps.resources
    assert "numpy.zip" in odps.resources
    assert result["resource_name"] not in odps.resources


def test_manager_drop_cleans_owned_orphan_when_function_is_already_missing():
    odps = FakeODPS()
    manager = MaxComputePythonUDFManager(odps)
    spec = make_spec()
    result = manager.deploy(spec)
    odps.functions.pop(spec.function_name)

    manager.drop(spec.function_name, spec.project, spec.schema)

    assert result["resource_name"] not in odps.resources


def test_manager_preserves_old_function_when_update_fails():
    odps = FakeODPS()
    manager = MaxComputePythonUDFManager(odps)
    first = manager.deploy(make_spec())
    odps.functions["price_for_xlarge"].update_failure = "before"

    with pytest.raises(RuntimeError, match="before persistence"):
        manager.deploy(make_spec(compiled_code="def main(price):\n    return price * 4\n"))

    assert persisted_resource_names(odps.functions["price_for_xlarge"]) == [first["resource_name"]]
    assert list(odps.resources) == [first["resource_name"]]


def test_manager_does_not_delete_new_resource_after_ambiguous_update_failure():
    odps = FakeODPS()
    manager = MaxComputePythonUDFManager(odps)
    first = manager.deploy(make_spec())
    odps.functions["price_for_xlarge"].update_failure = "after"
    updated_spec = make_spec(compiled_code="def main(price):\n    return price * 5\n")
    updated_deployment = build_python_udf_deployment(updated_spec)

    with pytest.raises(RuntimeError, match="after persistence"):
        manager.deploy(updated_spec)

    assert updated_deployment.resource_name in odps.resources
    assert first["resource_name"] in odps.resources
    assert persisted_resource_names(odps.functions["price_for_xlarge"]) == [
        updated_deployment.resource_name
    ]

    odps.functions["price_for_xlarge"].update_failure = None
    manager.deploy(updated_spec)

    assert first["resource_name"] not in odps.resources
    assert list(odps.resources) == [updated_deployment.resource_name]


def test_manager_refuses_to_reuse_unowned_hash_collision():
    odps = FakeODPS()
    deployment = build_python_udf_deployment(make_spec())
    odps.resources[deployment.resource_name] = FakeResource(
        deployment.resource_name, comment="not managed by dbt"
    )

    with pytest.raises(DbtRuntimeError, match="refusing to overwrite"):
        MaxComputePythonUDFManager(odps).deploy(make_spec())


def test_managed_resource_comment_is_scoped_to_function_identity():
    deployment = build_python_udf_deployment(make_spec())

    assert deployment.resource_comment == (
        f"{MANAGED_RESOURCE_COMMENT_PREFIX}analytics.udfs.price_for_xlarge"
    )


def test_dbt_parse_discovers_scalar_and_aggregate_python_functions(tmp_path: Path):
    project_dir = tmp_path / "project"
    profiles_dir = tmp_path / "profiles"
    functions_dir = project_dir / "functions"
    functions_dir.mkdir(parents=True)
    profiles_dir.mkdir()

    (project_dir / "dbt_project.yml").write_text(
        """
name: python_udf_parse_test
version: 1.0.0
config-version: 2
profile: python_udf_parse_test
function-paths: [functions]
""",
        encoding="utf-8",
    )
    (profiles_dir / "profiles.yml").write_text(
        """
python_udf_parse_test:
  target: dev
  outputs:
    dev:
      type: maxcompute
      project: test_project
      schema: default
      endpoint: http://example.invalid/api
      access_key_id: test
      access_key_secret: test
""",
        encoding="utf-8",
    )
    (functions_dir / "double_value.py").write_text(
        "def main(value):\n    return value * 2\n", encoding="utf-8"
    )
    (functions_dir / "sum_values.py").write_text(
        (
            "class SumValues:\n"
            "    def new_buffer(self):\n"
            "        return [0]\n"
            "    def iterate(self, buffer, value):\n"
            "        buffer[0] += value\n"
            "    def merge(self, buffer, partial):\n"
            "        buffer[0] += partial[0]\n"
            "    def terminate(self, buffer):\n"
            "        return buffer[0]\n"
        ),
        encoding="utf-8",
    )
    (functions_dir / "functions.yml").write_text(
        """
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
  - name: sum_values
    config:
      type: aggregate
      entry_point: SumValues
      runtime_version: cp311
      maxcompute:
        resources: []
        python_libraries: []
    arguments:
      - name: value
        data_type: bigint
    returns:
      data_type: bigint
""",
        encoding="utf-8",
    )

    parse_result = dbtRunner().invoke(
        [
            "parse",
            "--project-dir",
            str(project_dir),
            "--profiles-dir",
            str(profiles_dir),
            "--no-partial-parse",
        ]
    )

    assert parse_result.success, parse_result.exception
    manifest = json.loads((project_dir / "target" / "manifest.json").read_text())
    functions = manifest["functions"]
    assert set(functions) == {
        "function.python_udf_parse_test.double_value",
        "function.python_udf_parse_test.sum_values",
    }
    assert functions["function.python_udf_parse_test.sum_values"]["config"]["type"] == (
        "aggregate"
    )


def _python_function_macro_context(model, client):
    path = (
        Path(__file__).resolve().parents[2]
        / "dbt/include/maxcompute/macros/materializations/functions/python.sql"
    )
    calls = []

    def deploy(*args):
        calls.append(args)
        return MaxComputePythonUDFManager(client).deploy(make_spec())

    def error(message):
        raise CompilationError(message)

    def return_value(value):
        raise MacroReturn(value)

    context = {
        "model": model,
        "config": {"entry_point": "main", "runtime_version": "3.11"},
        "adapter": SimpleNamespace(create_or_update_python_udf=deploy),
        "exceptions": SimpleNamespace(raise_compiler_error=error),
        "return": return_value,
    }
    for name in ("deploy_python_function", "scalar_function_python", "aggregate_function_python"):
        macro_name = "maxcompute__" + name
        node = SimpleNamespace(
            name=macro_name,
            macro_sql=path.read_text(),
            unique_id="macro.dbt_maxcompute." + macro_name,
            original_file_path=str(path),
        )
        context[macro_name] = MacroGenerator(node, context)
    return context, calls


@pytest.mark.parametrize("function_type", ["scalar", "aggregate"])
@pytest.mark.parametrize("existing", [False, True])
def test_overloads_fail_before_any_function_or_resource_write(function_type, existing):
    client = FakeODPS()
    if existing:
        MaxComputePythonUDFManager(client).deploy(make_spec())
    resources_before = dict(client.resources)
    functions_before = dict(client.functions)
    context, calls = _python_function_macro_context(
        {"overloads": [{"defined_in": "another_signature"}]}, client
    )

    with pytest.raises(CompilationError, match="does not support function overloads"):
        context[f"maxcompute__{function_type}_function_python"]("target")

    assert calls == [], "even the root function must not be uploaded before rejection"
    assert client.resources == resources_before
    assert client.functions == functions_before
    assert client.deleted_resources == []


@pytest.mark.parametrize("overload_config", [{}, {"overloads": []}])
def test_single_signature_functions_still_deploy_with_old_and_new_core(overload_config):
    client = FakeODPS()
    model = dict(
        overload_config,
        arguments=[{"data_type": "double"}],
        compiled_code="def main(value): return value * 2",
        returns={"data_type": "double"},
    )
    context, calls = _python_function_macro_context(model, client)

    result = context["maxcompute__scalar_function_python"](
        SimpleNamespace(render=lambda: "target")
    )

    assert len(calls) == 1
    assert "created" in result
    assert len(client.functions) == 1
