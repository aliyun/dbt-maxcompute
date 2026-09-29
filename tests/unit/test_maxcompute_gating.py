"""Gating rules for the real-SQL integration entry.

These run in CI on every pull request, in an environment with no MaxCompute
credentials - which is precisely the state they assert on: a run that cannot
reach a server must be explained, never reported as a passing integration.
"""

import pytest

import maxcompute_gating
from maxcompute_gating import blocked_reason, preflight_reason, summary

PROFILE_BODY = """
type: maxcompute
project: dbt_integration_project
endpoint: http://service.example.com/api
schema: dbt_integration
"""


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """No inherited profile, no inherited credentials, no cached decisions."""
    for id_key, secret_key in maxcompute_gating.CREDENTIAL_ENV_PAIRS:
        monkeypatch.delenv(id_key, raising=False)
        monkeypatch.delenv(secret_key, raising=False)
    monkeypatch.delenv(maxcompute_gating.PROFILE_PATH_ENV, raising=False)
    maxcompute_gating.clear_caches()
    yield
    maxcompute_gating.clear_caches()


def write_profile(tmp_path, monkeypatch, body):
    path = tmp_path / "dbt_profile.yml"
    path.write_text(body)
    monkeypatch.setenv(maxcompute_gating.PROFILE_PATH_ENV, str(path))
    return path


class TestBlockedReason:
    def test_missing_profile_is_blocked_with_an_action(self, monkeypatch, tmp_path):
        monkeypatch.setenv(maxcompute_gating.PROFILE_PATH_ENV, str(tmp_path / "absent.yml"))
        reason = blocked_reason()
        assert reason is not None
        assert "DBT_PROFILE_PATH" in reason
        assert maxcompute_gating.DOC_URL in reason

    def test_chain_without_exported_credentials_is_blocked(self, tmp_path, monkeypatch):
        write_profile(tmp_path, monkeypatch, PROFILE_BODY + "auth_type: chain\n")
        reason = blocked_reason()
        assert reason is not None
        assert "chain" in reason
        assert "ALIBABA_CLOUD_ACCESS_KEY_ID" in reason

    def test_access_key_profile_without_keys_is_blocked(self, tmp_path, monkeypatch):
        write_profile(tmp_path, monkeypatch, PROFILE_BODY)
        reason = blocked_reason()
        assert reason is not None
        assert "access_key_id" in reason

    def test_profile_missing_required_field_is_blocked(self, tmp_path, monkeypatch):
        write_profile(
            tmp_path,
            monkeypatch,
            "type: maxcompute\nproject: p\nauth_type: access_key\n"
            "access_key_id: id\naccess_key_secret: secret\n",
        )
        reason = blocked_reason()
        assert reason is not None
        assert "endpoint" in reason

    def test_credentials_present_in_the_environment_unblocks_the_chain(
        self, tmp_path, monkeypatch
    ):
        write_profile(tmp_path, monkeypatch, PROFILE_BODY + "auth_type: chain\n")
        monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "id-value")
        monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "secret-value")
        assert blocked_reason() is None

    def test_static_keys_in_the_profile_unblock_access_key_auth(self, tmp_path, monkeypatch):
        write_profile(
            tmp_path,
            monkeypatch,
            PROFILE_BODY
            + "auth_type: access_key\naccess_key_id: id-value\naccess_key_secret: secret-value\n",
        )
        assert blocked_reason() is None

    def test_summary_never_contains_credential_material(self, tmp_path, monkeypatch):
        write_profile(
            tmp_path,
            monkeypatch,
            PROFILE_BODY
            + "auth_type: access_key\naccess_key_id: id-value\naccess_key_secret: secret-value\n",
        )
        assert blocked_reason() is None
        rendered = summary()
        assert "secret-value" not in rendered
        assert "id-value" not in rendered
        assert "service.example.com" in rendered


class FakeClient:
    def __init__(self, error):
        self.error = error

    def list_schemas(self, project=None):
        raise self.error


class TestPreflightReason:
    def test_two_tier_project_is_blocked_as_an_environment_problem(self, tmp_path, monkeypatch):
        write_profile(
            tmp_path,
            monkeypatch,
            PROFILE_BODY + "auth_type: access_key\naccess_key_id: id\naccess_key_secret: s\n",
        )
        monkeypatch.setattr(
            maxcompute_gating,
            "odps_client",
            lambda *args, **kwargs: FakeClient(
                Exception(
                    "ODPS-0110061: Failed to run ddltask - Invalid database operations on two-tier model"
                )
            ),
        )
        reason = preflight_reason()
        assert reason is not None
        assert "three-tier" in reason

    def test_unreachable_project_is_blocked_rather_than_passing(self, tmp_path, monkeypatch):
        write_profile(
            tmp_path,
            monkeypatch,
            PROFILE_BODY + "auth_type: access_key\naccess_key_id: id\naccess_key_secret: s\n",
        )
        monkeypatch.setattr(
            maxcompute_gating,
            "odps_client",
            lambda *args, **kwargs: FakeClient(Exception("Connection refused")),
        )
        reason = preflight_reason()
        assert reason is not None
        assert "not reachable" in reason

    def test_usable_project_reports_no_blocker(self, tmp_path, monkeypatch):
        write_profile(
            tmp_path,
            monkeypatch,
            PROFILE_BODY + "auth_type: access_key\naccess_key_id: id\naccess_key_secret: s\n",
        )

        class OkClient:
            def list_schemas(self, project=None):
                return iter([type("S", (), {"name": "default"})()])

        monkeypatch.setattr(maxcompute_gating, "odps_client", lambda *a, **k: OkClient())
        assert preflight_reason() is None


class TestSchemaManifest:
    """Cleanup has to be attributed to this run, not to the whole project."""

    def test_recording_is_a_noop_without_a_manifest(self, monkeypatch):
        monkeypatch.delenv(maxcompute_gating.SCHEMA_MANIFEST_ENV, raising=False)
        maxcompute_gating.record_schema("test_whatever")
        assert maxcompute_gating.recorded_schemas() == []

    def test_recording_appends_incrementally(self, tmp_path, monkeypatch):
        manifest = tmp_path / "schemas.txt"
        monkeypatch.setenv(maxcompute_gating.SCHEMA_MANIFEST_ENV, str(manifest))
        maxcompute_gating.record_schema("test_run_a")
        maxcompute_gating.record_schema("test_run_b")
        assert maxcompute_gating.recorded_schemas() == ["test_run_a", "test_run_b"]

    def test_recording_is_idempotent(self, tmp_path, monkeypatch):
        """The suite and the observer fixture see the same schema once."""
        manifest = tmp_path / "schemas.txt"
        monkeypatch.setenv(maxcompute_gating.SCHEMA_MANIFEST_ENV, str(manifest))
        maxcompute_gating.record_schema("test_run_a")
        maxcompute_gating.record_schema("test_run_a")
        assert maxcompute_gating.recorded_schemas() == ["test_run_a"]

    def test_only_this_runs_schemas_count_as_leftovers(self, tmp_path, monkeypatch):
        manifest = tmp_path / "schemas.txt"
        monkeypatch.setenv(maxcompute_gating.SCHEMA_MANIFEST_ENV, str(manifest))
        maxcompute_gating.record_schema("test_run_mine")
        maxcompute_gating.record_schema("test_run_dropped")
        # someone else's schema appears while this run works; one of ours survived
        monkeypatch.setattr(
            maxcompute_gating,
            "existing_schemas",
            lambda: ["test_run_mine", "test_someone_elses_run", "default"],
        )
        assert maxcompute_gating.leftover_schemas() == ["test_run_mine"]

    def test_a_foreign_schema_never_fails_the_run(self, tmp_path, monkeypatch):
        manifest = tmp_path / "schemas.txt"
        monkeypatch.setenv(maxcompute_gating.SCHEMA_MANIFEST_ENV, str(manifest))
        maxcompute_gating.record_schema("test_run_mine")
        monkeypatch.setattr(
            maxcompute_gating,
            "existing_schemas",
            lambda: ["default", "test_run_dropped", "test_concurrent_other"],
        )
        assert maxcompute_gating.leftover_schemas() == []

    def test_nothing_recorded_means_unknown_not_clean(self, tmp_path, monkeypatch):
        monkeypatch.setenv(maxcompute_gating.SCHEMA_MANIFEST_ENV, str(tmp_path / "absent.txt"))
        monkeypatch.setattr(maxcompute_gating, "existing_schemas", lambda: ["test_foreign"])
        assert maxcompute_gating.leftover_schemas() == []
        assert maxcompute_gating.recorded_schemas() == []
