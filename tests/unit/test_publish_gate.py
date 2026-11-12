"""Offline regression cases for the manual publish gate (``scripts/publish_gate.py``).

These exist because of a review finding: the publish path used to trust the functional
pytest step's exit status, and ``tests/conftest.py`` converts an unusable profile into
``pytest.skip`` for every case - so pytest exits 0 with nothing executed. Every case here
is offline: no MaxCompute project, no credentials, no network except the read-only
``python tests/maxcompute_gating.py blocked`` invocation, which is the shared
"profile is usable" question this suite is about.

Written without pytest fixtures on purpose: the helpers take their temp directory as an
argument so the same functions can be driven by a plain interpreter as well.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = REPO_ROOT / "scripts" / "publish_gate.py"
GATING_PATH = REPO_ROOT / "tests" / "maxcompute_gating.py"

REQUIRED = ("tests/functional/test_core.py", "tests/functional/adapter/test_python_model.py", "tests/functional/adapter/test_udf.py")


def _gate():
    spec = importlib.util.spec_from_file_location("publish_gate", str(GATE_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _xml(cases):
    body = "".join(
        '<testcase classname="%(cls)s" name="%(name)s" file="%(f)s">%(child)s</testcase>'
        % {
            "cls": c["classname"],
            "name": c["name"],
            "f": c["file"],
            "child": ("<skipped message=%r />" % c["reason"]) if c.get("reason") is not None else "",
        }
        for c in cases
    )
    return '<testsuite name="pytest" tests="%d">%s</testsuite>' % (len(cases), body)


def _write(tmp, text, name="report-functional.xml"):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _run(argv):
    return subprocess.run([sys.executable, str(GATE_PATH)] + argv, capture_output=True, text=True)


def _passed(cls, name, file_):
    return {"classname": cls, "name": name, "file": file_, "reason": None}


def _skipped(cls, name, file_, reason):
    return {"classname": cls, "name": name, "file": file_, "reason": reason}


def all_passing(tmp=None):
    tmp = tmp or tempfile.mkdtemp(prefix="gate-pass-")
    cases = [
        _passed("tests.functional.test_core", "test_table_materialization", REQUIRED[0]),
        _passed("tests.functional.adapter.test_python_model", "test_python_model", REQUIRED[1]),
        _passed("tests.functional.adapter.test_udf", "test_udf", REQUIRED[2]),
    ]
    return _write(tmp, _xml(cases))


def test_positive_control_full_pass():
    """The guard must be able to say yes, otherwise it proves nothing."""
    tmp = tempfile.mkdtemp(prefix="gate-control-")
    xml = all_passing(tmp)
    result = _run(["functional", "--xml", xml, "--require", ",".join(REQUIRED)])
    assert result.returncode == 0, result.stderr
    assert "publish gate satisfied" in result.stdout


def test_all_skipped_functional_result_is_refused_even_though_pytest_exits_zero():
    """The reviewed shape: every case skipped because the profile was unusable."""
    tmp = tempfile.mkdtemp(prefix="gate-skip-")
    cases = [
        _skipped("tests.functional.test_core", "test_a", REQUIRED[0], "MaxCompute integration not run: profile is missing required key(s)"),
        _skipped("tests.functional.adapter.test_python_model", "test_b", REQUIRED[1], "MaxCompute integration not run: profile is missing required key(s)"),
        _skipped("tests.functional.adapter.test_udf", "test_c", REQUIRED[2], "MaxCompute integration not run: profile is missing required key(s)"),
    ]
    xml = _write(tmp, _xml(cases))
    result = _run(["functional", "--xml", xml, "--require", ",".join(REQUIRED)])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "passed nothing" in result.stderr
    assert "live case(s) passed" in result.stderr


def test_nonempty_incomplete_profile_is_blocked_by_the_shared_gate():
    """The first link of the chain, executed for real: a nonempty but key-less profile."""
    tmp = tempfile.mkdtemp(prefix="gate-profile-")
    profile = os.path.join(tmp, "dbt_profile.yml")
    with open(profile, "w", encoding="utf-8") as handle:
        handle.write("{}\n")
    env = dict(os.environ)
    env["DBT_PROFILE_PATH"] = profile
    result = subprocess.run([sys.executable, str(GATING_PATH), "blocked"], capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "missing required key(s)" in result.stdout
    assert "access_key" not in result.stdout.lower()  # no credential material echoed


def test_required_suite_with_no_result_at_all_is_refused():
    tmp = tempfile.mkdtemp(prefix="gate-missing-")
    cases = [_passed("tests.functional.test_core", "test_a", REQUIRED[0])]
    xml = _write(tmp, _xml(cases))
    result = _run(["functional", "--xml", xml, "--require", ",".join(REQUIRED)])
    assert result.returncode == 1
    for token in REQUIRED[1:]:
        assert token in result.stderr


def test_report_with_zero_cases_is_unusable_not_passing():
    tmp = tempfile.mkdtemp(prefix="gate-empty-")
    xml = _write(tmp, '<testsuite name="pytest" tests="0"></testsuite>')
    result = _run(["functional", "--xml", xml, "--require", ",".join(REQUIRED)])
    assert result.returncode == 2 or result.returncode == 1, result.stdout + result.stderr
    assert "no testcase element" in result.stderr


def test_missing_report_is_refused():
    result = _run(["functional", "--xml", "/nonexistent/report-functional.xml", "--require", REQUIRED[0]])
    assert result.returncode == 2
    assert "never ran" in result.stderr


def test_failed_case_is_refused():
    tmp = tempfile.mkdtemp(prefix="gate-fail-")
    text = (
        '<testsuite name="pytest" tests="3">'
        '<testcase classname="tests.functional.test_core" name="test_a" file="%s"><failure message="boom">x</failure></testcase>'
        '<testcase classname="tests.functional.adapter.test_python_model" name="test_b" file="%s"></testcase>'
        '<testcase classname="tests.functional.adapter.test_udf" name="test_c" file="%s"></testcase>'
        "</testsuite>" % REQUIRED
    )
    xml = _write(tmp, text)
    result = _run(["functional", "--xml", xml, "--require", ",".join(REQUIRED)])
    assert result.returncode == 1
    assert "failed or errored" in result.stderr


def test_explicit_capability_skip_is_the_only_accepted_skip():
    tmp = tempfile.mkdtemp(prefix="gate-capability-")
    cases = [
        _passed("tests.functional.test_core", "test_a", REQUIRED[0]),
        _skipped("tests.functional.adapter.test_python_model", "test_b", REQUIRED[1], "Python model requires dbt-core >= 1.7"),
        _passed("tests.functional.adapter.test_udf", "test_c", REQUIRED[2]),
    ]
    xml = _write(tmp, _xml(cases))
    strict = _run(["functional", "--xml", xml, "--require", ",".join(REQUIRED)])
    assert strict.returncode == 1, "an unexplained skip must not publish"
    allowed = _run(
        [
            "functional",
            "--xml",
            xml,
            "--require",
            ",".join(REQUIRED),
            "--allow-skip",
            "requires dbt-core",
        ]
    )
    assert allowed.returncode == 0, allowed.stderr


if __name__ == "__main__":  # pragma: no cover - lets a plain interpreter run the suite
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001 - a local runner, not production code
                failures += 1
                print("FAIL", name, repr(exc))
    sys.exit(1 if failures else 0)
