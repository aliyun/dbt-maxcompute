import unittest
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock, patch, PropertyMock

from dbt.adapters.maxcompute.wrapper import MaxQAConfig, CursorWrapper, ConnectionWrapper
from dbt.adapters.maxcompute.setting_parser import ParseResult


def _make_cursor(maxqa_config=None):
    """Build a CursorWrapper with a mocked connection and ODPS client."""
    mock_odps = MagicMock()
    mock_instance = MagicMock()
    mock_instance.id = "test_instance_id"
    mock_odps.execute_sql_interactive.return_value = mock_instance

    mock_conn = MagicMock(spec=ConnectionWrapper)
    mock_conn.odps = mock_odps
    mock_conn._odps = mock_odps
    mock_conn._maxqa_config = maxqa_config
    mock_conn._hints = {}
    mock_conn._session_name = "public"

    cursor = CursorWrapper.__new__(CursorWrapper)
    cursor._connection = mock_conn
    cursor._maxqa_config = maxqa_config
    cursor._sqa_type = None
    cursor._fallback_policy = []
    cursor._hints = {}
    cursor._quota_name = None
    cursor._reset_state()

    return cursor, mock_odps


class TestMaxQAConfig(unittest.TestCase):
    def test_defaults(self):
        config = MaxQAConfig()
        self.assertIsNone(config.quota_name)
        self.assertTrue(config.fallback)
        self.assertIsNone(config.offline_quota_name)

    def test_full_config(self):
        config = MaxQAConfig(
            quota_name="my_quota",
            fallback=False,
            offline_quota_name="offline_q",
        )
        self.assertEqual(config.quota_name, "my_quota")
        self.assertFalse(config.fallback)
        self.assertEqual(config.offline_quota_name, "offline_q")


class TestResolveMaxQA(unittest.TestCase):
    def test_no_config_returns_none(self):
        cursor, _ = _make_cursor(maxqa_config=None)
        settings = {}
        self.assertIsNone(cursor._resolve_maxqa(settings))

    def test_connection_level_maxqa(self):
        config = MaxQAConfig(quota_name="q1", fallback=True)
        cursor, _ = _make_cursor(maxqa_config=config)
        settings = {}
        result = cursor._resolve_maxqa(settings)
        self.assertIs(result, config)

    def test_model_override_offline(self):
        config = MaxQAConfig(quota_name="q1")
        cursor, _ = _make_cursor(maxqa_config=config)
        settings = {"dbt.execution_mode": "maxqa"}
        result = cursor._resolve_maxqa(settings)
        self.assertIsNotNone(result)
        # dbt.execution_mode should be popped
        self.assertNotIn("dbt.execution_mode", settings)

    def test_model_override_forces_offline(self):
        config = MaxQAConfig(quota_name="q1")
        cursor, _ = _make_cursor(maxqa_config=config)
        settings = {"dbt.execution_mode": "offline"}
        result = cursor._resolve_maxqa(settings)
        self.assertIsNone(result)
        self.assertNotIn("dbt.execution_mode", settings)

    def test_model_override_maxqa_without_connection_config(self):
        cursor, _ = _make_cursor(maxqa_config=None)
        settings = {"dbt.execution_mode": "maxqa", "dbt.quota_name": "model_q"}
        result = cursor._resolve_maxqa(settings)
        self.assertIsNotNone(result)
        self.assertEqual(result.quota_name, "model_q")
        self.assertNotIn("dbt.execution_mode", settings)
        self.assertNotIn("dbt.quota_name", settings)

    def test_model_quota_overrides_connection_quota(self):
        config = MaxQAConfig(quota_name="conn_q", fallback=True, offline_quota_name="off_q")
        cursor, _ = _make_cursor(maxqa_config=config)
        settings = {"dbt.execution_mode": "maxqa", "dbt.quota_name": "model_q"}
        result = cursor._resolve_maxqa(settings)
        self.assertEqual(result.quota_name, "model_q")
        # fallback settings inherited from connection config
        self.assertTrue(result.fallback)
        self.assertEqual(result.offline_quota_name, "off_q")

    def test_dbt_hints_are_popped(self):
        cursor, _ = _make_cursor(maxqa_config=None)
        settings = {
            "dbt.execution_mode": "offline",
            "dbt.quota_name": "some_q",
            "odps.sql.allow.fullscan": "true",
        }
        cursor._resolve_maxqa(settings)
        self.assertNotIn("dbt.execution_mode", settings)
        self.assertNotIn("dbt.quota_name", settings)
        self.assertIn("odps.sql.allow.fullscan", settings)


class TestExecuteMaxQA(unittest.TestCase):
    def test_calls_execute_sql_interactive(self):
        config = MaxQAConfig(quota_name="q1", fallback=True, offline_quota_name="off_q")
        cursor, mock_odps = _make_cursor(maxqa_config=config)
        result = ParseResult(
            settings={"odps.sql.allow.fullscan": "true"},
            remaining_query="SELECT 1",
            errors=[],
        )
        cursor._execute_maxqa(result, config)

        mock_odps.execute_sql_interactive.assert_called_once_with(
            "SELECT 1",
            use_mcqa_v2=True,
            quota_name="q1",
            hints={"odps.sql.allow.fullscan": "true"},
            fallback=True,
            offline_quota_name="off_q",
        )
        self.assertIs(cursor._instance, mock_odps.execute_sql_interactive.return_value)

    def test_calls_with_fallback_disabled(self):
        config = MaxQAConfig(quota_name=None, fallback=False)
        cursor, mock_odps = _make_cursor(maxqa_config=config)
        result = ParseResult(settings={}, remaining_query="CREATE TABLE t(id BIGINT)", errors=[])
        cursor._execute_maxqa(result, config)

        mock_odps.execute_sql_interactive.assert_called_once_with(
            "CREATE TABLE t(id BIGINT)",
            use_mcqa_v2=True,
            quota_name=None,
            hints={},
            fallback=False,
            offline_quota_name=None,
        )

    def test_calls_with_fallback_and_offline_quota(self):
        config = MaxQAConfig(quota_name="interactive_q", fallback=True, offline_quota_name="batch_q")
        cursor, mock_odps = _make_cursor(maxqa_config=config)
        result = ParseResult(settings={}, remaining_query="INSERT INTO t SELECT 1", errors=[])
        cursor._execute_maxqa(result, config)

        call_kwargs = mock_odps.execute_sql_interactive.call_args
        self.assertTrue(call_kwargs.kwargs["fallback"])
        self.assertEqual(call_kwargs.kwargs["offline_quota_name"], "batch_q")

    def test_quota_name_none_is_valid(self):
        config = MaxQAConfig(quota_name=None, fallback=True)
        cursor, mock_odps = _make_cursor(maxqa_config=config)
        result = ParseResult(settings={}, remaining_query="SELECT 1", errors=[])
        cursor._execute_maxqa(result, config)

        call_kwargs = mock_odps.execute_sql_interactive.call_args
        self.assertIsNone(call_kwargs.kwargs["quota_name"])


class TestExecuteRouting(unittest.TestCase):
    """Test that CursorWrapper.execute() routes to the correct path."""

    def test_offline_mode_does_not_call_interactive(self):
        cursor, mock_odps = _make_cursor(maxqa_config=None)
        mock_instance = MagicMock()
        mock_instance.id = "test_id"

        with patch.object(CursorWrapper, "_execute_maxqa") as mock_maxqa:
            with patch("odps.dbapi.Cursor.execute") as mock_super_execute:
                cursor._instance = mock_instance
                # Patch super().execute to set _instance
                def side_effect(*a, **kw):
                    cursor._instance = mock_instance
                mock_super_execute.side_effect = side_effect

                cursor.execute("SELECT 1")
                mock_maxqa.assert_not_called()

    def test_maxqa_mode_calls_interactive(self):
        config = MaxQAConfig(quota_name="q1", fallback=True)
        cursor, mock_odps = _make_cursor(maxqa_config=config)

        cursor.execute("SELECT count(*) FROM t")
        mock_odps.execute_sql_interactive.assert_called_once()
        call_kwargs = mock_odps.execute_sql_interactive.call_args
        self.assertTrue(call_kwargs.kwargs["use_mcqa_v2"])

    def test_model_override_in_sql_hints(self):
        cursor, mock_odps = _make_cursor(maxqa_config=None)
        sql = "SET dbt.execution_mode=maxqa;\nSET dbt.quota_name=my_q;\nSELECT 1"

        cursor.execute(sql)
        mock_odps.execute_sql_interactive.assert_called_once()
        call_kwargs = mock_odps.execute_sql_interactive.call_args
        self.assertEqual(call_kwargs.kwargs["quota_name"], "my_q")
        # dbt.* hints should NOT appear in the hints dict sent to MaxCompute
        hints_sent = call_kwargs.kwargs["hints"]
        self.assertNotIn("dbt.execution_mode", hints_sent)
        self.assertNotIn("dbt.quota_name", hints_sent)

    def test_model_override_offline_on_maxqa_connection(self):
        config = MaxQAConfig(quota_name="q1", fallback=True)
        cursor, mock_odps = _make_cursor(maxqa_config=config)
        mock_instance = MagicMock()
        mock_instance.id = "test_id"

        with patch("odps.dbapi.Cursor.execute") as mock_super_execute:
            def side_effect(*a, **kw):
                cursor._instance = mock_instance
            mock_super_execute.side_effect = side_effect

            cursor.execute("SET dbt.execution_mode=offline;\nSELECT 1")
            mock_odps.execute_sql_interactive.assert_not_called()
            mock_super_execute.assert_called_once()


class TestConnectionWrapper(unittest.TestCase):
    def test_maxqa_config_propagated_to_cursor(self):
        config = MaxQAConfig(quota_name="q1")
        mock_odps = MagicMock()
        mock_odps.is_schema_namespace_enabled.return_value = True
        wrapper = ConnectionWrapper(odps=mock_odps, hints={}, maxqa_config=config)
        cursor = wrapper.cursor()
        self.assertIs(cursor._maxqa_config, config)

    def test_no_maxqa_config(self):
        mock_odps = MagicMock()
        mock_odps.is_schema_namespace_enabled.return_value = True
        wrapper = ConnectionWrapper(odps=mock_odps, hints={})
        cursor = wrapper.cursor()
        self.assertIsNone(cursor._maxqa_config)


if __name__ == "__main__":
    unittest.main()
