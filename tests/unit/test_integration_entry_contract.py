"""The entry script's three exit states, exercised without a server.

`scripts/run-integration-tests.sh` promises three different outcomes, and the
interesting failure mode is collapsing them: a run whose residue check could not
be read used to end `PASSED` with exit `0`. These tests run the real script with
the interpreter swapped for a stub that answers the gating helper and pytest
deterministically, so no credential, no project and no network is involved.

The stub covers one branch each, and the pass/fail pairings are what make the
third state meaningful:

  ok              -> 0  cases ran, residue verified gone
  leak            -> 1  a schema this run created survived
  no-manifest     -> 2  no verdict: nothing was recorded to check
  list-fail       -> 2  no verdict: the schema list could not be read
  preflight-fail  -> 2  no verdict: nothing reached the server
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "run-integration-tests.sh"

STUB = r"""#!/usr/bin/env bash
# Plays the interpreter for the entry script: pytest, the gating helper, and the
# inline JUnit parser. Everything it does is decided by STUB_MODE - no server.
set -u
MODE="${STUB_MODE:-ok}"
REAL="${STUB_REAL_PYTHON:-python3}"

# the script's own inline parser: `python - <junit.xml>` must run for real
if [ "$1" = "-" ]; then
  exec "$REAL" "$@"
fi

if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
  xml=""
  for arg in "$@"; do
    case "$arg" in
      --junitxml=*) xml="${arg#--junitxml=}" ;;
    esac
  done
  if [ -n "$xml" ]; then
    cat >"$xml" <<'XML'
<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" errors="0" failures="0" skipped="0" tests="2">
  <testcase classname="stub" name="one" time="0.01" />
  <testcase classname="stub" name="two" time="0.01" />
</testsuite>
XML
  fi
  if [ "$MODE" != "no-manifest" ] && [ -n "${DBT_INTEGRATION_SCHEMA_MANIFEST:-}" ]; then
    printf 'teststub_schemas\n' >>"$DBT_INTEGRATION_SCHEMA_MANIFEST"
  fi
  exit 0
fi

# gating helper calls: "<gate.py> <command>"
case "${@: -1}" in
  summary)
    echo "project=stub project_layers=three-tier endpoint_host=stub auth_type=chain schema_prefix=test*"
    ;;
  preflight)
    if [ "$MODE" = "preflight-fail" ]; then
      echo "no credential resolved in this environment (stub)" >&2
      exit 1
    fi
    ;;
  leftover-schemas)
    if [ "$MODE" = "list-fail" ]; then
      echo "cannot list the project schemas (stub)" >&2
      exit 1
    fi
    if [ "$MODE" = "leak" ]; then
      echo "teststub_schemas"
    fi
    ;;
esac
exit 0
"""


def run_entry(tmp_path: Path, mode: str) -> subprocess.CompletedProcess:
    """Run the real entry script with a stubbed interpreter, in an isolated env."""
    if os.name != "posix" or shutil.which("bash") is None:
        pytest.skip("this contract exercises a bash entry point")
    stub = tmp_path / "stub-python"
    stub.write_text(STUB)
    stub.chmod(0o755)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHON": str(stub),
        "STUB_MODE": mode,
        "STUB_REAL_PYTHON": sys.executable,
    }
    for noisy in ("GITHUB_STEP_SUMMARY", "DBT_PROFILE_PATH", "MC_PROJECT", "MC_ENDPOINT"):
        env.setdefault(noisy, "")
    env = {k: v for k, v in env.items() if v != "" or k == "HOME"}
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_clean_run_is_the_only_green(tmp_path):
    res = run_entry(tmp_path, "ok")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "cleanup: ok" in res.stdout
    assert "status:  PASSED" in res.stdout
    assert "executed=2 passed=2" in res.stdout


def test_surviving_schema_is_a_violation_not_a_no_verdict(tmp_path):
    res = run_entry(tmp_path, "leak")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "FAILED - schemas this run created are still present" in res.stdout


def test_unreadable_schema_list_is_no_verdict_not_green(tmp_path):
    res = run_entry(tmp_path, "list-fail")
    assert res.returncode == 2, res.stdout + res.stderr
    assert "cleanup: UNKNOWN" in res.stdout
    assert "status:  NO-VERDICT" in res.stdout
    assert "PASSED" not in res.stdout


def test_run_that_recorded_no_schemas_is_no_verdict_not_green(tmp_path):
    res = run_entry(tmp_path, "no-manifest")
    assert res.returncode == 2, res.stdout + res.stderr
    assert "status:  NO-VERDICT" in res.stdout
    assert "recorded no schemas" in res.stdout


def test_no_evidence_at_all_stays_blocked(tmp_path):
    res = run_entry(tmp_path, "preflight-fail")
    assert res.returncode == 2, res.stdout + res.stderr
    assert "INTEGRATION: BLOCKED" in res.stdout
    assert "NO-VERDICT" not in res.stdout


def test_help_prints_the_whole_exit_contract(tmp_path):
    if os.name != "posix" or shutil.which("bash") is None:
        pytest.skip("this contract exercises a bash entry point")
    res = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0
    # a fixed sed range used to clip this block when it grew; the third state must
    # never be hidden from the person reading --help
    assert "Exit status" in res.stdout
    assert "no verdict, never a pass" in res.stdout
    assert "schema-residue check could not be read" in res.stdout
