from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from dbt.cli.main import dbtRunner
from dbt_common.exceptions import DbtRuntimeError
from odps import options as odps_options
from odps.config import option_context as odps_option_context
from odps.errors import ODPSError
from odps.types import Column, OdpsSchema

from dbt.adapters.maxcompute.credentials import MaxComputeCredentials
from dbt.adapters.maxcompute.impl import MaxComputeAdapter
from dbt.adapters.maxcompute.python_submissions import (
    MaxFramePythonJobHelper,
    MaxFrameSubmissionResult,
    _WARNED_MAXFRAME_PYTHON_VERSIONS,
    _load_maxframe_runtime,
)
from dbt.adapters.maxcompute.relation_configs._partition import PartitionConfig


class FakeSession:
    def __init__(self, session_id="maxframe-session-123"):
        self.session_id = session_id
        self.destroyed = False
        self.executed_with = None

    def get_logview_address(self):
        return "https://logview.example/maxframe-session-123"

    def destroy(self):
        self.destroyed = True


class FakeMaxFrame:
    def __init__(self, session):
        self.session = session
        self.new_session_kwargs = None

    def new_session(self, **kwargs):
        self.new_session_kwargs = kwargs
        return self.session


class SequencedMaxFrame:
    def __init__(self, *sessions):
        self.sessions = list(sessions)
        self.new_session_kwargs = []

    def new_session(self, **kwargs):
        self.new_session_kwargs.append(kwargs)
        return self.sessions.pop(0)


def make_credentials():
    credentials = MagicMock()
    credentials.schema = "analytics"
    credentials.timezone = "Asia/Shanghai"
    credentials.tunnel_endpoint = "https://dt.example/api"
    credentials.maxframe_quota_name = "profile_quota"
    credentials.maxframe_retries = None
    credentials.maxframe_python_version_check = "warn"
    credentials.maxframe_pythonpack_production = True
    credentials.odps.return_value = MagicMock()
    return credentials


def make_parsed_model(**config):
    model_config = {
        "packages": [],
        "timeout": 600,
        "sql_hints": {
            "odps.sql.allow.fullscan": "false",
            "dbt.execution_mode": "offline",
        },
        **config,
    }
    return {
        "unique_id": "model.test.python_model",
        "original_file_path": "models/python_model.py",
        "config": model_config,
    }


def test_chain_credentials_survive_nested_pyodps_option_context():
    credentials = MaxComputeCredentials(
        database="test_project",
        schema="analytics",
        endpoint="https://service.example.invalid/api",
        auth_type="chain",
    )
    with patch("dbt.adapters.maxcompute.credentials.ODPS") as odps_class:
        credentials.odps()
    account = odps_class.call_args.kwargs["account"]

    # MaxFrame opens nested PyODPS option contexts after dbt exposes the
    # session's ODPS entry through global options. The provider chain contains
    # a refresh lock, so this exercises the exact deepcopy boundary from #27.
    with odps_option_context():
        odps_options.account = account
        with odps_option_context():
            assert odps_options.account is account


@contextmanager
def capturing_option_context(captured, options):
    captured.update(options)
    yield SimpleNamespace()


def test_submit_executes_compiled_code_and_cleans_up_session():
    session = FakeSession()
    maxframe = FakeMaxFrame(session)
    captured_options = {}
    credentials = make_credentials()
    helper = MaxFramePythonJobHelper(make_parsed_model(), credentials)

    option_context = lambda options: capturing_option_context(  # noqa: E731
        captured_options, options
    )
    compiled_code = "maxframe_session.executed_with = odps_entry"

    with patch(
        "dbt.adapters.maxcompute.python_submissions._load_maxframe_runtime",
        return_value=(maxframe, option_context),
    ):
        result = helper.submit(compiled_code)

    odps_entry = credentials.odps.return_value
    odps_entry.to_global.assert_called_once_with(overwritable=True)
    assert odps_entry.tunnel_endpoint == "https://dt.example/api"
    assert session.executed_with is odps_entry
    assert session.destroyed
    assert maxframe.new_session_kwargs == {
        "odps_entry": odps_entry,
        "default": False,
        "timeout": 600,
    }
    assert captured_options["session.default_schema"] == "analytics"
    assert captured_options["session.quota_name"] == "profile_quota"
    assert captured_options["local_timezone"] == "Asia/Shanghai"
    assert captured_options["pythonpack.task.settings"] == {"odps.pythonpack.production": "true"}
    assert captured_options["sql.settings"]["odps.sql.allow.fullscan"] == "false"
    assert "dbt.execution_mode" not in captured_options["sql.settings"]
    assert result == MaxFrameSubmissionResult(
        run_id="maxframe-session-123",
        compiled_code=compiled_code,
        logview_available=True,
    )


def test_submit_drops_intermediate_table_and_session_after_failure():
    session = FakeSession()
    maxframe = FakeMaxFrame(session)
    credentials = make_credentials()
    helper = MaxFramePythonJobHelper(make_parsed_model(), credentials)

    @contextmanager
    def option_context(_):
        yield SimpleNamespace()

    compiled_code = """
_dbt_maxframe_target_relation = "project.schema.model__dbt_tmp"
raise ValueError("model exploded")
"""

    with patch(
        "dbt.adapters.maxcompute.python_submissions._load_maxframe_runtime",
        return_value=(maxframe, option_context),
    ):
        with pytest.raises(DbtRuntimeError, match="model exploded"):
            helper.submit(compiled_code)

    credentials.odps.return_value.delete_table.assert_called_once_with(
        "project.schema.model__dbt_tmp", if_exists=True
    )
    assert session.destroyed


def test_cleans_up_session_scoped_maxframe_tables_and_functions():
    odps_entry = MagicMock()
    odps_entry.list_tables.return_value = [
        SimpleNamespace(name="tmp_mf_session_1_result"),
        SimpleNamespace(name="tmp_mf_other_session_result"),
        SimpleNamespace(name="user_table"),
    ]
    odps_entry.list_functions.return_value = [
        SimpleNamespace(name="mf_udf_session_1_user_udf_123"),
        SimpleNamespace(name="mf_udf_other_session_user_udf_456"),
        SimpleNamespace(name="user_function"),
    ]

    MaxFramePythonJobHelper._cleanup_maxframe_session_artifacts(
        "session_1", odps_entry, "analytics"
    )

    odps_entry.delete_table.assert_called_once_with(
        "tmp_mf_session_1_result", schema="analytics", if_exists=True
    )
    odps_entry.delete_function.assert_called_once_with(
        "mf_udf_session_1_user_udf_123", schema="analytics"
    )


def test_submit_retries_intermediate_cleanup_after_transient_failure():
    session = FakeSession()
    maxframe = FakeMaxFrame(session)
    credentials = make_credentials()
    credentials.odps.return_value.delete_table.side_effect = [
        ConnectionResetError("transient reset"),
        None,
    ]
    helper = MaxFramePythonJobHelper(make_parsed_model(), credentials)

    @contextmanager
    def option_context(_):
        yield SimpleNamespace()

    compiled_code = """
_dbt_maxframe_target_relation = "project.schema.model__dbt_tmp"
raise ValueError("model exploded")
"""

    with (
        patch(
            "dbt.adapters.maxcompute.python_submissions._load_maxframe_runtime",
            return_value=(maxframe, option_context),
        ),
        patch("dbt.adapters.maxcompute.python_submissions.time.sleep") as sleep,
        pytest.raises(DbtRuntimeError, match="model exploded"),
    ):
        helper.submit(compiled_code)

    assert credentials.odps.return_value.delete_table.call_count == 2
    sleep.assert_called_once_with(0.5)
    assert session.destroyed


def test_model_level_quota_overrides_profile_quota():
    credentials = make_credentials()
    helper = MaxFramePythonJobHelper(
        make_parsed_model(maxframe_quota_name="model_quota"), credentials
    )

    assert helper._maxframe_options()["session.quota_name"] == "model_quota"


def test_model_schema_overrides_profile_default_schema():
    helper = MaxFramePythonJobHelper(make_parsed_model(), make_credentials())
    helper._parsed_model["schema"] = "generated_model_schema"

    assert helper._maxframe_options()["session.default_schema"] == "generated_model_schema"


def test_model_forwards_managed_runtime_image_to_maxframe_sql_settings():
    helper = MaxFramePythonJobHelper(
        make_parsed_model(sql_hints={"odps.session.image": "sklearn"}),
        make_credentials(),
    )

    assert helper._maxframe_options()["sql.settings"]["odps.session.image"] == "sklearn"


def test_model_can_disable_pythonpack_production_cache():
    helper = MaxFramePythonJobHelper(
        make_parsed_model(maxframe_pythonpack_production=False), make_credentials()
    )

    assert helper._maxframe_options()["pythonpack.task.settings"] == {
        "odps.pythonpack.production": "false"
    }


@pytest.mark.parametrize("value", [None, True, "true", "yes", "1"])
def test_pythonpack_production_cache_is_enabled_by_default(value):
    credentials = make_credentials()
    credentials.maxframe_pythonpack_production = value
    helper = MaxFramePythonJobHelper(make_parsed_model(), credentials)

    assert helper._maxframe_pythonpack_production() is True


@pytest.mark.parametrize("value", [False, "false", "no", "0"])
def test_pythonpack_production_cache_accepts_false_values(value):
    helper = MaxFramePythonJobHelper(
        make_parsed_model(maxframe_pythonpack_production=value), make_credentials()
    )

    assert helper._maxframe_pythonpack_production() is False


def test_invalid_pythonpack_production_cache_setting_fails():
    helper = MaxFramePythonJobHelper(
        make_parsed_model(maxframe_pythonpack_production="sometimes"), make_credentials()
    )

    with pytest.raises(DbtRuntimeError, match="must be a boolean"):
        helper._maxframe_pythonpack_production()


def test_maxframe_retries_default_and_profile_override():
    credentials = make_credentials()
    helper = MaxFramePythonJobHelper(make_parsed_model(), credentials)
    assert helper._maxframe_retries() == 2

    credentials.maxframe_retries = 4
    assert helper._maxframe_retries() == 4


def test_maxframe_timeout_default_and_model_override():
    assert (
        MaxFramePythonJobHelper(
            make_parsed_model(timeout=None), make_credentials()
        )._maxframe_timeout()
        == 600
    )
    assert (
        MaxFramePythonJobHelper(
            make_parsed_model(timeout=3600), make_credentials()
        )._maxframe_timeout()
        == 3600
    )


def test_submit_retries_dag_transport_failure_with_new_session():
    first_session = FakeSession("first-session")
    retry_session = FakeSession("retry-session")
    maxframe = SequencedMaxFrame(first_session, retry_session)
    credentials = make_credentials()
    credentials.odps.return_value.retry_attempts = 0
    credentials.odps.return_value.list_tables.side_effect = [
        [SimpleNamespace(name="tmp_mf_first-session_intermediate")],
        [],
    ]
    credentials.odps.return_value.list_functions.side_effect = [
        [SimpleNamespace(name="mf_udf_first-session_row_udf")],
        [],
    ]
    helper = MaxFramePythonJobHelper(make_parsed_model(maxframe_retries=1), credentials)

    @contextmanager
    def option_context(_):
        yield SimpleNamespace()

    compiled_code = """
class RetryOnceTileable:
    def execute(self, session):
        odps_entry.retry_attempts += 1
        if odps_entry.retry_attempts == 1:
            raise ConnectionResetError("transient reset")
        maxframe_session.executed_with = session

_dbt_maxframe_execute(RetryOnceTileable())
"""

    with patch(
        "dbt.adapters.maxcompute.python_submissions._load_maxframe_runtime",
        return_value=(maxframe, option_context),
    ):
        result = helper.submit(compiled_code)

    assert result.run_id == "retry-session"
    assert first_session.destroyed
    assert retry_session.destroyed
    assert retry_session.executed_with is retry_session
    assert len(maxframe.new_session_kwargs) == 2
    credentials.odps.return_value.delete_table.assert_called_once_with(
        "tmp_mf_first-session_intermediate", schema="analytics", if_exists=True
    )
    credentials.odps.return_value.delete_function.assert_called_once_with(
        "mf_udf_first-session_row_udf", schema="analytics"
    )


class FakeTornadoHTTPClientError(Exception):
    __module__ = "tornado.httpclient"

    def __init__(self, code):
        self.code = code


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ConnectionResetError("reset"), True),
        (FakeTornadoHTTPClientError(500), True),
        (FakeTornadoHTTPClientError(429), True),
        (FakeTornadoHTTPClientError(400), False),
        (ODPSError("server unavailable", status_code=500), True),
        (ODPSError("bad request", status_code=400), False),
        (ValueError("invalid model"), False),
    ],
)
def test_transient_maxframe_error_classification(error, expected):
    assert MaxFramePythonJobHelper._is_transient_maxframe_error(error) is expected


def test_adds_type_preserving_sentinel_for_empty_safe_sink():
    dataframe = MagicMock()
    tagged_dataframe = MagicMock()
    dataframe.assign.return_value = tagged_dataframe
    odps_entry = MagicMock()
    writer = odps_entry.create_table.return_value.open_writer.return_value.__enter__.return_value
    temporary_relations = []
    inferred_schema = OdpsSchema(columns=[Column("id", "bigint")])
    sentinel_dataframe = MagicMock()
    combined_dataframe = MagicMock()

    fake_dataframe_module = ModuleType("maxframe.dataframe")
    fake_dataframe_module.read_odps_table = MagicMock(return_value=sentinel_dataframe)
    fake_dataframe_module.concat = MagicMock(return_value=combined_dataframe)
    fake_maxframe_module = ModuleType("maxframe")
    fake_maxframe_module.dataframe = fake_dataframe_module
    fake_odpsio_module = ModuleType("maxframe.io.odpsio")
    fake_odpsio_module.pandas_to_odps_schema = MagicMock(return_value=(inferred_schema, None))
    fake_io_module = ModuleType("maxframe.io")
    fake_io_module.odpsio = fake_odpsio_module

    with patch.dict(
        "sys.modules",
        {
            "maxframe": fake_maxframe_module,
            "maxframe.dataframe": fake_dataframe_module,
            "maxframe.io": fake_io_module,
            "maxframe.io.odpsio": fake_odpsio_module,
        },
    ):
        result = MaxFramePythonJobHelper._with_sentinel(
            dataframe,
            "project.analytics.model__dbt_stage",
            odps_entry,
            temporary_relations,
        )

    assert result is combined_dataframe
    assert len(temporary_relations) == 1
    assert temporary_relations[0].startswith("project.analytics.__dbt_mf_sentinel_")
    created_schema = odps_entry.create_table.call_args.args[1]
    assert [column.name for column in created_schema.columns] == [
        "id",
        "__dbt_maxframe_sentinel",
    ]
    writer.write.assert_called_once_with([[None, True]])
    dataframe.assign.assert_called_once_with(__dbt_maxframe_sentinel=False)
    fake_dataframe_module.read_odps_table.assert_called_once_with(
        temporary_relations[0], odps_entry=odps_entry
    )
    fake_dataframe_module.concat.assert_called_once_with(
        [tagged_dataframe, sentinel_dataframe], ignore_index=True
    )


def test_rejects_model_column_that_conflicts_with_sentinel():
    dataframe = MagicMock()
    inferred_schema = OdpsSchema(columns=[Column("__DBT_MAXFRAME_SENTINEL", "boolean")])
    fake_dataframe_module = ModuleType("maxframe.dataframe")
    fake_maxframe_module = ModuleType("maxframe")
    fake_maxframe_module.dataframe = fake_dataframe_module
    fake_odpsio_module = ModuleType("maxframe.io.odpsio")
    fake_odpsio_module.pandas_to_odps_schema = MagicMock(return_value=(inferred_schema, None))
    fake_io_module = ModuleType("maxframe.io")
    fake_io_module.odpsio = fake_odpsio_module

    with (
        patch.dict(
            "sys.modules",
            {
                "maxframe": fake_maxframe_module,
                "maxframe.dataframe": fake_dataframe_module,
                "maxframe.io": fake_io_module,
                "maxframe.io.odpsio": fake_odpsio_module,
            },
        ),
        pytest.raises(DbtRuntimeError, match="reserve the column name"),
    ):
        MaxFramePythonJobHelper._with_sentinel(
            dataframe,
            "project.analytics.model__dbt_stage",
            MagicMock(),
            [],
        )


@pytest.mark.parametrize("retries", [-1, "invalid"])
def test_invalid_maxframe_retries_fail_with_actionable_error(retries):
    helper = MaxFramePythonJobHelper(
        make_parsed_model(maxframe_retries=retries), make_credentials()
    )

    with pytest.raises(DbtRuntimeError, match="non-negative integer"):
        helper._maxframe_retries()


@pytest.mark.parametrize("timeout", [0, -1, "invalid"])
def test_invalid_maxframe_timeout_fails_with_actionable_error(timeout):
    helper = MaxFramePythonJobHelper(make_parsed_model(timeout=timeout), make_credentials())

    with pytest.raises(DbtRuntimeError, match="positive integer"):
        helper._maxframe_timeout()


def test_packages_fail_with_actionable_error():
    with pytest.raises(DbtRuntimeError, match="does not support model-level `packages`"):
        MaxFramePythonJobHelper(
            make_parsed_model(packages=["scikit-learn==1.7.0"]), make_credentials()
        )


def test_missing_maxframe_has_install_hint():
    with patch.dict("sys.modules", {"maxframe": None}):
        with pytest.raises(DbtRuntimeError, match=r"dbt-maxcompute\[maxframe\]"):
            _load_maxframe_runtime()


@pytest.mark.parametrize("value", [None, "warn", "WARN"])
def test_python_version_check_defaults_to_warning(value):
    credentials = make_credentials()
    credentials.maxframe_python_version_check = value
    helper = MaxFramePythonJobHelper(make_parsed_model(), credentials)

    assert helper._maxframe_python_version_check() == "warn"


def test_non_311_python_warns_once_and_allows_submission():
    helper = MaxFramePythonJobHelper(make_parsed_model(), make_credentials())
    version = SimpleNamespace(major=3, minor=12)
    _WARNED_MAXFRAME_PYTHON_VERSIONS.clear()

    with patch("dbt.adapters.maxcompute.python_submissions.logger.warning") as warning:
        helper._check_local_maxframe_python_version(version)
        helper._check_local_maxframe_python_version(version)

    warning.assert_called_once()
    assert "job submission remain enabled" in warning.call_args.args[0]


def test_non_311_python_can_be_rejected_by_explicit_strict_mode():
    helper = MaxFramePythonJobHelper(
        make_parsed_model(maxframe_python_version_check="error"), make_credentials()
    )

    with pytest.raises(DbtRuntimeError, match="Python 3.12"):
        helper._check_local_maxframe_python_version(SimpleNamespace(major=3, minor=12))


def test_non_311_python_check_can_be_disabled():
    helper = MaxFramePythonJobHelper(
        make_parsed_model(maxframe_python_version_check="off"), make_credentials()
    )

    with patch("dbt.adapters.maxcompute.python_submissions.logger.warning") as warning:
        helper._check_local_maxframe_python_version(SimpleNamespace(major=3, minor=12))

    warning.assert_not_called()


def test_python_311_never_warns_or_fails():
    helper = MaxFramePythonJobHelper(
        make_parsed_model(maxframe_python_version_check="error"), make_credentials()
    )

    with patch("dbt.adapters.maxcompute.python_submissions.logger.warning") as warning:
        helper._check_local_maxframe_python_version(SimpleNamespace(major=3, minor=11))

    warning.assert_not_called()


def test_invalid_python_version_check_fails_with_actionable_error():
    helper = MaxFramePythonJobHelper(
        make_parsed_model(maxframe_python_version_check="sometimes"), make_credentials()
    )

    with pytest.raises(DbtRuntimeError, match="warn, error, off"):
        helper._maxframe_python_version_check()


def test_adapter_response_contains_session_without_logview_token():
    result = MaxFrameSubmissionResult(
        run_id="session-1",
        compiled_code="print('hello')",
        logview_available=True,
    )

    response = MaxComputeAdapter.generate_python_submission_response(None, result)

    assert response.query_id == "session-1"
    assert response.code == "print('hello')"
    assert response._message == "OK (MaxFrame session: session-1)"
    assert "https://logview.example/session-1" not in response._message


def test_adapter_response_contains_session_when_logview_is_unavailable():
    result = MaxFrameSubmissionResult(
        run_id="session-without-logview",
        compiled_code="print('hello')",
        logview_available=False,
    )

    response = MaxComputeAdapter.generate_python_submission_response(None, result)

    assert response._message == "OK (MaxFrame session: session-without-logview)"


@pytest.mark.parametrize(
    ("raw_config", "expected_fields", "expected_types"),
    [
        (
            {"fields": "pt,region", "data_types": "string,string"},
            ["pt", "region"],
            ["string", "string"],
        ),
        (
            {"field": "event_time", "data_type": "timestamp"},
            ["event_time"],
            ["timestamp"],
        ),
        (
            {"fields": ["pt"], "data_types": ["string"]},
            ["pt"],
            ["string"],
        ),
    ],
)
def test_partition_config_accepts_maxcompute_and_bigquery_shapes(
    raw_config, expected_fields, expected_types
):
    config = PartitionConfig.parse(raw_config)

    assert config.fields == expected_fields
    assert config.data_types == expected_types


def test_partition_config_rejects_conflicting_aliases():
    with pytest.raises(DbtRuntimeError, match="use either `field` or `fields`"):
        PartitionConfig.parse({"field": "pt", "fields": "other"})


def test_dbt_parse_accepts_maxframe_python_model(tmp_path: Path):
    project_dir = tmp_path / "project"
    profiles_dir = tmp_path / "profiles"
    models_dir = project_dir / "models"
    models_dir.mkdir(parents=True)
    profiles_dir.mkdir()

    (project_dir / "dbt_project.yml").write_text(
        """
name: maxframe_parse_test
version: 1.0.0
config-version: 2
profile: maxframe_parse_test
model-paths: [models]
""",
        encoding="utf-8",
    )
    (profiles_dir / "profiles.yml").write_text(
        """
maxframe_parse_test:
  target: dev
  outputs:
    dev:
      type: maxcompute
      project: test_project
      schema: default
      endpoint: http://example.invalid/api
      access_key_id: test
      access_key_secret: test
      submission_method: maxframe
      maxframe_quota_name: test_quota
      maxframe_retries: 1
      maxframe_python_version_check: error
      maxframe_pythonpack_production: false
""",
        encoding="utf-8",
    )
    (models_dir / "upstream.sql").write_text("select 1 as id\n", encoding="utf-8")
    (models_dir / "python_model.py").write_text(
        """
def model(dbt, session):
    dbt.config(
        materialized="table",
        submission_method="maxframe",
        timeout=600,
    )
    return dbt.ref("upstream")
""",
        encoding="utf-8",
    )
    (models_dir / "incremental_python_model.py").write_text(
        """
def model(dbt, session):
    dbt.config(
        materialized="incremental",
        submission_method="maxframe",
        incremental_strategy="merge",
        unique_key="id",
        partition_by={"field": "pt", "data_type": "string"},
    )
    return dbt.ref("upstream")
""",
        encoding="utf-8",
    )
    (models_dir / "microbatch_python_model.py").write_text(
        """
def model(dbt, session):
    dbt.config(
        materialized="incremental",
        submission_method="maxframe",
        incremental_strategy="microbatch",
        unique_key="id",
        event_time="event_time",
        batch_size="day",
        begin="2026-08-06",
        partition_by={
            "field": "event_time",
            "data_type": "timestamp",
            "granularity": "day",
            "generate_column_name": "ds",
        },
    )
    return dbt.ref("upstream")
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


@pytest.mark.parametrize("error", ["KeyboardInterrupt", "RuntimeError"])
def test_failed_model_stops_session_before_dropping_output(error):
    events = []
    session = FakeSession()
    session.destroy = lambda: events.append("destroy")
    credentials = make_credentials()
    credentials.odps.return_value.delete_table.side_effect = (
        lambda *args, **kwargs: events.append("drop")
    )
    helper = MaxFramePythonJobHelper(make_parsed_model(maxframe_retries=0), credentials)
    compiled_code = (
        "_dbt_maxframe_target_relation = 'analytics.intermediate'\n"
        f"raise {error}('interrupted')"
    )
    expected_error = KeyboardInterrupt if error == "KeyboardInterrupt" else DbtRuntimeError
    with patch(
        "dbt.adapters.maxcompute.python_submissions._load_maxframe_runtime",
        return_value=(FakeMaxFrame(session), lambda options: capturing_option_context({}, options)),
    ), pytest.raises(expected_error):
        helper.submit(compiled_code)
    assert events == ["destroy", "drop"]


@pytest.mark.parametrize("error", ["KeyboardInterrupt", "RuntimeError"])
def test_destroy_failure_preserves_tables_and_original_error(error):
    session = FakeSession()
    session.destroy = MagicMock(side_effect=RuntimeError("destroy unavailable"))
    credentials = make_credentials()
    helper = MaxFramePythonJobHelper(make_parsed_model(maxframe_retries=0), credentials)
    compiled_code = (
        "_dbt_maxframe_target_relation = 'analytics.intermediate'\n"
        f"raise {error}('original model failure')"
    )
    expected_error = KeyboardInterrupt if error == "KeyboardInterrupt" else DbtRuntimeError
    with patch(
        "dbt.adapters.maxcompute.python_submissions._load_maxframe_runtime",
        return_value=(FakeMaxFrame(session), lambda options: capturing_option_context({}, options)),
    ), pytest.raises(expected_error, match="original model failure"):
        helper.submit(compiled_code)
    session.destroy.assert_called_once()
    credentials.odps.return_value.delete_table.assert_not_called()
    credentials.odps.return_value.list_tables.assert_not_called()


def test_retry_does_not_restart_model_while_prior_session_may_live():
    session = FakeSession()
    session.destroy = MagicMock(side_effect=RuntimeError("destroy unavailable"))
    maxframe = SequencedMaxFrame(session, FakeSession("must-not-start"))
    credentials = make_credentials()
    helper = MaxFramePythonJobHelper(make_parsed_model(maxframe_retries=1), credentials)
    with patch(
        "dbt.adapters.maxcompute.python_submissions._load_maxframe_runtime",
        return_value=(maxframe, lambda options: capturing_option_context({}, options)),
    ), pytest.raises(DbtRuntimeError, match="original transport reset"):
        helper.submit(
            "_dbt_maxframe_target_relation = 'analytics.intermediate'\n"
            "raise ConnectionResetError('original transport reset')"
        )
    assert len(maxframe.new_session_kwargs) == 1
    credentials.odps.return_value.delete_table.assert_not_called()


def test_successful_output_is_retained_when_session_destroy_fails():
    session = FakeSession()
    session.destroy = MagicMock(side_effect=RuntimeError("destroy unavailable"))
    credentials = make_credentials()
    helper = MaxFramePythonJobHelper(make_parsed_model(), credentials)
    with patch(
        "dbt.adapters.maxcompute.python_submissions._load_maxframe_runtime",
        return_value=(FakeMaxFrame(session), lambda options: capturing_option_context({}, options)),
    ):
        result = helper.submit("_dbt_maxframe_target_relation = 'analytics.final_output'")
    assert result.run_id == session.session_id
    credentials.odps.return_value.delete_table.assert_not_called()
