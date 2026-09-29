#!/usr/bin/env bash
# Run the real-SQL integration regression for dbt-maxcompute.
#
# This is the single entry point for a developer laptop and for a trusted CI
# job. It decides up front whether a real run is possible and says so out loud:
# a run that never reached a server is reported as BLOCKED and exits non-zero,
# never as a passing integration.
#
#   scripts/run-integration-tests.sh                     # minimal SQL set (fast)
#   scripts/run-integration-tests.sh --suite core        # broader release suite
#   scripts/run-integration-tests.sh -- -k TestMinimalView
#
# Profile resolution, in order:
#   1. $DBT_PROFILE_PATH, or ./dbt_profile.yml if it exists (git-ignored).
#      This is what CI uses: the profile comes from a repository secret.
#   2. otherwise, when MC_PROJECT and MC_ENDPOINT are exported, a profile is
#      written to a private temp file for the duration of the run with
#      auth_type: chain, so access keys stay in the environment and are never
#      copied into the repository.
#
# Exit status:
#   0  cases ran on the server and all passed
#   1  cases ran and at least one failed, or a schema this run created survived
#   2  integration could not run: no credentials, two-tier project, or the
#      project was unreachable. No evidence was produced.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python3}"
GATE="$REPO_ROOT/tests/maxcompute_gating.py"
SUITE="smoke"
PASSTHROUGH=()

die() {
  echo "$*" >&2
  exit 2
}

# finish <status-line> <exit-code>: print the verdict and, when the caller is a
# GitHub Actions job, mirror the same numbers into the job summary.
finish() {
  local status="$1" code="$2"
  echo "  status:  ${status}"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
      echo ""
      echo "### SQL integration regression (${SUITE})"
      echo ""
      echo "| target | cases | result |"
      echo "| --- | --- | --- |"
      printf '| `%s` | `%s` | %s |\n' "$TARGET" "$SUMMARY" "$status"
    } >>"$GITHUB_STEP_SUMMARY"
  fi
  exit "$code"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --suite)
      [ $# -ge 2 ] || die "--suite needs smoke|core"
      SUITE="$2"
      shift 2
      ;;
    --help | -h)
      sed -n '2,25p' "${BASH_SOURCE[0]}"  # the header comment block, without the code
      exit 0
      ;;
    --)
      shift
      PASSTHROUGH=("$@")
      break
      ;;
    *)
      PASSTHROUGH+=("$1")
      shift
      ;;
  esac
done

case "$SUITE" in
  smoke) MARKER="integration_smoke" ;;
  core) MARKER="core_test" ;;
  *) die "unknown --suite '$SUITE' (expected smoke or core)" ;;
esac

if [ ! -f "$GATE" ]; then
  die "gating helper missing at $GATE (run from a repository checkout)"
fi

# --------------------------------------------------------------------------
# 1. resolve a profile
# --------------------------------------------------------------------------
TEMP_DIR="$(umask 077; mktemp -d)"
# The suite appends every schema it creates to this file, so the cleanup check
# below is about *this run*.  A MaxCompute project is shared: other runs' test*
# schemas are not our leak, and our own leak must not hide among them.
export DBT_INTEGRATION_SCHEMA_MANIFEST="${TEMP_DIR}/schemas.txt"
: >"$DBT_INTEGRATION_SCHEMA_MANIFEST"

cleanup() {
  rm -rf "$TEMP_DIR"
}
# HUP/INT/TERM as well as EXIT: a cancelled CI job or a Ctrl-C must not leave the
# generated profile, the JUnit record or the schema manifest behind in /tmp.
trap cleanup EXIT HUP INT TERM

if [ -z "${DBT_PROFILE_PATH:-}" ] && [ ! -f "$REPO_ROOT/dbt_profile.yml" ]; then
  if [ -n "${MC_PROJECT:-}" ] && [ -n "${MC_ENDPOINT:-}" ]; then
    AUTH_TYPE="chain"
    {
      echo "type: maxcompute"
      echo "project: ${MC_PROJECT}"
      echo "schema: ${MC_SCHEMA:-integration}"
      echo "endpoint: ${MC_ENDPOINT}"
      echo "auth_type: ${AUTH_TYPE}"
      echo "threads: 4"
    } >"${TEMP_DIR}/dbt_profile.yml"
    export DBT_PROFILE_PATH="${TEMP_DIR}/dbt_profile.yml"
    echo "profile: generated a temp profile with auth_type=${AUTH_TYPE} (credentials stay in the environment)"
  fi
fi

TARGET="$("$PYTHON" "$GATE" summary 2>/dev/null || echo 'no profile configured')"
echo "target:  ${TARGET}"

# --------------------------------------------------------------------------
# 2. preflight: credentials, three-tier project, reachability
# --------------------------------------------------------------------------
SUMMARY="executed=0 passed=0 failed=0 errors=0 skipped=0"
if ! REASON="$("$PYTHON" "$GATE" preflight 2>&1)"; then
  echo "INTEGRATION: BLOCKED (no cases were executed)"
  echo "reason:      ${REASON}"
  echo "not a pass:  the SQL regression did not reach a MaxCompute project"
  finish "BLOCKED - no evidence produced (preflight failed: ${REASON})" 2
fi

# --------------------------------------------------------------------------
# 3. run the cases
# --------------------------------------------------------------------------
# inside TEMP_DIR so the EXIT trap takes it with the profile and the manifest
JUNIT_XML="${TEMP_DIR}/junit.xml"
echo "suite:     ${SUITE} (pytest -m ${MARKER})"
rc=0
# --continue-on-collection-errors: one broken import somewhere else in
# tests/functional must not abort the session and silently replace our evidence
# with "0 cases ran".  The error is still reported and still turns the run red.
"$PYTHON" -m pytest \
  -m "$MARKER" \
  -v -rA --tb=short \
  --continue-on-collection-errors \
  --junitxml="$JUNIT_XML" \
  tests/functional \
  ${DBT_INTEGRATION_KEYWORD:+-k "$DBT_INTEGRATION_KEYWORD"} \
  ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"} || rc=$?

# --------------------------------------------------------------------------
# 4. report what actually ran, from the JUnit record
# --------------------------------------------------------------------------
SUMMARY="$("$PYTHON" - "$JUNIT_XML" <<'PY'
import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
cases = list(root.iter("testcase"))
passed = failed = errors = skipped = 0
broken = []
for case in cases:
    name = case.get("name", "?")
    if case.find("failure") is not None:
        failed += 1
    elif case.find("error") is not None:
        # a collection error shows up as a testcase too, but nothing ran
        errors += 1
        broken.append(name)
    elif case.find("skipped") is not None:
        skipped += 1
    else:
        passed += 1
executed = passed + failed
print(
    f"executed={executed} passed={passed} failed={failed} errors={errors} skipped={skipped}"
    + (f" not_run=[{', '.join(sorted(broken))}]" if broken else "")
)
PY
)"

echo
echo "INTEGRATION RESULTS"
echo "  cases:   ${SUMMARY}"

case "$rc" in
  0) outcome="ran" ;;
  1) outcome="ran with failures" ;;
  2) outcome="pytest interrupted (collection or usage error - see the log above)" ;;
  3) outcome="pytest internal error" ;;
  4) outcome="pytest usage error" ;;
  5) outcome="no tests collected" ;;
  *) outcome="abnormal exit (pytest rc=${rc})" ;;
esac

EXECUTED="$(printf '%s' "$SUMMARY" | sed -nE 's/.*executed=([0-9]+).*/\1/p')"
if [ "$EXECUTED" = "0" ]; then
  finish "BLOCKED - ${outcome}; no case reached the MaxCompute project, which is not a pass" 2
fi

# --------------------------------------------------------------------------
# 5. cleanup check: schemas created by this run must be gone
# --------------------------------------------------------------------------
RECORDED=0
if [ -f "$DBT_INTEGRATION_SCHEMA_MANIFEST" ]; then
  RECORDED="$(grep -c . "$DBT_INTEGRATION_SCHEMA_MANIFEST" || true)"
fi

if [ "$RECORDED" = "0" ]; then
  # Nothing recorded: the suite either skipped, or was run without this script.
  echo "  cleanup: UNKNOWN - this run recorded no schemas (the suite's own assertion still applies)"
elif LEAKED="$("$PYTHON" "$GATE" leftover-schemas 2>&1)"; then
  if [ -n "$LEAKED" ]; then
    finish "FAILED - schemas this run created are still present: $(printf '%s ' $LEAKED)" 1
  fi
  echo "  cleanup: ok (${RECORDED} schemas created by this run, all dropped)"
else
  echo "  cleanup: UNKNOWN - cannot list the project's schemas: $(printf '%s' "$LEAKED" | head -1)"
fi
echo "  cases:   server-side assertions are listed under SERVER[...] in the run output"

if [ "$rc" != "0" ]; then
  finish "FAILED (${outcome})" 1
fi
finish "PASSED (server-side evidence produced)" 0
