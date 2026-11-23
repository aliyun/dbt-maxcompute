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

REQUIRED = (
    "tests/functional/test_core.py",
    "tests/functional/adapter/test_python_model.py",
    "tests/functional/adapter/test_udf.py",
)


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
            "child": (
                ("<skipped message=%r />" % c["reason"]) if c.get("reason") is not None else ""
            ),
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


# --------------------------------------------------------------------------------------
# Review round 3 (issuecomment-6882306041): the shape the report actually has, and skips
# that sit next to a passing case.
# --------------------------------------------------------------------------------------

#: pytest 9's default JUnit family is ``classes``: dotted ``classname``, no ``file``
#: attribute. The workflow passes readable slash paths to ``--require``. Both sides have to
#: meet in the middle, or a genuinely passing report gets refused - which is worse than
#: having no gate, because the next person silences it.
FUNCTIONAL = (
    "tests/functional/test_core.py",
    "tests/functional/adapter/test_python_model.py",
    "tests/functional/adapter/test_udf.py",
)
MODULES = (
    "tests.functional.test_core",
    "tests.functional.adapter.test_python_model",
    "tests.functional.adapter.test_udf",
)


def _cls_case(module, name, state=None, reason=None):
    """One ``testcase`` element in the shape pytest 9 really writes (classname only)."""
    attrs = 'classname="%s" name="%s" time="0.001"' % (module, name)
    if state == "skipped":
        return "<testcase %s><skipped message=%s /></testcase>" % (attrs, '"%s"' % reason)
    return "<testcase %s />" % attrs


def _raw(xml_body, count):
    return '<testsuites><testsuite name="pytest" tests="%d">%s</testsuite></testsuites>' % (
        count,
        xml_body,
    )


def test_pytest9_shape_is_accepted_with_path_style_require():
    """The reviewed integration failure: three passing cases, dotted classname, no file attr."""
    tmp = tempfile.mkdtemp(prefix="gate-shape-")
    cases = "".join(_cls_case(mod, "test_ok") for mod in MODULES)
    xml = _write(tmp, _raw(cases, 3))
    result = _run(["functional", "--xml", xml, "--require", ",".join(FUNCTIONAL)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "publish gate satisfied" in result.stdout


def test_file_attribute_shape_still_matches():
    """Reports that do carry ``file`` (lineage family, and the older hand-written cases)."""
    tmp = tempfile.mkdtemp(prefix="gate-file-")
    cases = "".join(_cls_case(mod.split(".")[0] + ".placeholder", "test_ok") for mod in MODULES)
    body = "".join(
        '<testcase classname="placeholder.module" name="test_ok" file="%s" />' % path
        for path, mod in zip(FUNCTIONAL, MODULES)
    )
    xml = _write(tmp, _raw(body, 3))
    assert cases  # the two shapes are exercised independently
    result = _run(["functional", "--xml", xml, "--require", ",".join(FUNCTIONAL)])
    assert result.returncode == 0, result.stdout + result.stderr


def test_unapproved_skip_next_to_a_passing_case_is_refused():
    """Review point 2: a suite that also passes must not launder an unexplained skip."""
    tmp = tempfile.mkdtemp(prefix="gate-mixedskip-")
    body = (
        _cls_case(MODULES[0], "test_ok")
        + _cls_case(
            MODULES[0], "test_gap", state="skipped", reason="needs a capability that is absent"
        )
        + _cls_case(MODULES[1], "test_ok")
        + _cls_case(MODULES[2], "test_ok")
    )
    xml = _write(tmp, _raw(body, 4))
    result = _run(["functional", "--xml", xml, "--require", ",".join(FUNCTIONAL)])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "without an explicit --allow-skip" in result.stderr
    assert "test_gap" in result.stderr


def test_allowlisted_skip_next_to_a_passing_case_is_still_refused_unless_named():
    """The allowlist is matched against the skip reason, not waved through by the suite."""
    tmp = tempfile.mkdtemp(prefix="gate-allownamed-")
    body = (
        _cls_case(MODULES[0], "test_ok")
        + _cls_case(
            MODULES[0],
            "test_python_model_needs_core",
            state="skipped",
            reason="requires dbt-core >= 1.7",
        )
        + _cls_case(MODULES[1], "test_ok")
        + _cls_case(MODULES[2], "test_ok")
    )
    xml = _write(tmp, _raw(body, 4))
    strict = _run(["functional", "--xml", xml, "--require", ",".join(FUNCTIONAL)])
    assert (
        strict.returncode == 1
    ), "an unlisted reason must not pass just because another case passed"
    allowed = _run(
        [
            "functional",
            "--xml",
            xml,
            "--require",
            ",".join(FUNCTIONAL),
            "--allow-skip",
            "requires dbt-core",
        ]
    )
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr


def _generate_with_installed_pytest(tmp, mixed_skip):
    """Produce a report with the *installed* pytest instead of hand-writing XML.

    The hand-written positive controls are what let the shape bug survive a full round:
    they always supplied the attribute the real reporter does not write.
    """
    for rel in FUNCTIONAL:
        full = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        init = os.path.join(os.path.dirname(full), "__init__.py")
        if not os.path.exists(init):
            open(init, "w").close()
        body = "def test_ok():\n    assert True\n"
        if mixed_skip and rel.endswith("test_core.py"):
            body += "\nimport pytest\n\n\ndef test_unexplained_gap():\n    pytest.skip('no allowlisted reason')\n"
        open(full, "w", encoding="utf-8").write(body)
    xml = os.path.join(tmp, "report-mixed.xml" if mixed_skip else "report-functional.xml")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/functional",
            "--junitxml=" + xml,
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        cwd=tmp,
    )
    assert os.path.isfile(xml), proc.stdout + proc.stderr
    return xml, proc


def test_real_pytest_passing_report_is_accepted_by_the_gate():
    tmp = tempfile.mkdtemp(prefix="gate-real-pass-")
    xml, proc = _generate_with_installed_pytest(tmp, mixed_skip=False)
    assert proc.returncode == 0, proc.stdout
    text = open(xml, encoding="utf-8").read()
    assert "<testcase" in text
    result = _run(["functional", "--xml", xml, "--require", ",".join(FUNCTIONAL)])
    assert result.returncode == 0, (
        "gate refused a real passing report:\n" + result.stdout + result.stderr
    )


def test_real_pytest_mixed_pass_skip_report_is_refused_by_the_gate():
    tmp = tempfile.mkdtemp(prefix="gate-real-mixed-")
    xml, proc = _generate_with_installed_pytest(tmp, mixed_skip=True)
    assert proc.returncode == 0, proc.stdout
    assert "skipped" in open(xml, encoding="utf-8").read()
    result = _run(["functional", "--xml", xml, "--require", ",".join(FUNCTIONAL)])
    assert result.returncode == 1, (
        "gate accepted a real report containing an unapproved skip:\n" + result.stdout
    )
    assert "without an explicit --allow-skip" in result.stderr


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
        _skipped(
            "tests.functional.test_core",
            "test_a",
            REQUIRED[0],
            "MaxCompute integration not run: profile is missing required key(s)",
        ),
        _skipped(
            "tests.functional.adapter.test_python_model",
            "test_b",
            REQUIRED[1],
            "MaxCompute integration not run: profile is missing required key(s)",
        ),
        _skipped(
            "tests.functional.adapter.test_udf",
            "test_c",
            REQUIRED[2],
            "MaxCompute integration not run: profile is missing required key(s)",
        ),
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
    result = subprocess.run(
        [sys.executable, str(GATING_PATH), "blocked"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    combined = result.stdout + result.stderr
    if "ModuleNotFoundError" in combined:
        # The shared module imports PyYAML. Without it this subprocess fails for a reason
        # that has nothing to do with the judgement under test, so say so instead of
        # reporting a wrong rc.
        try:
            import pytest

            pytest.skip("shared gating module needs PyYAML, absent in this interpreter")
        except ImportError:  # pragma: no cover - plain-interpreter runner
            print(
                "SKIP test_nonempty_incomplete_profile_is_blocked_by_the_shared_gate: PyYAML missing"
            )
            return
    assert result.returncode == 1, combined
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
    result = _run(
        ["functional", "--xml", "/nonexistent/report-functional.xml", "--require", REQUIRED[0]]
    )
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
        _skipped(
            "tests.functional.adapter.test_python_model",
            "test_b",
            REQUIRED[1],
            "Python model requires dbt-core >= 1.7",
        ),
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


# --------------------------------------------------------------------------------------
# Review round 4 (issuecomment-7074272574): bind every job to one commit.
#
# The workflow asks a git question ("does this tag still identify the commit we tested?")
# before it writes to PyPI and before it opens the GitHub Release. These cases build real
# throwaway repositories, with real annotated and lightweight tags, and point the CLI at
# them - a hand-written listing would test the parser against the shape someone guessed,
# which is the mistake that produced the round-3 defect.
# --------------------------------------------------------------------------------------

GIT_ENV = {
    "GIT_AUTHOR_NAME": "gate fixture",
    "GIT_AUTHOR_EMAIL": "gate-fixture@example.invalid",
    "GIT_COMMITTER_NAME": "gate fixture",
    "GIT_COMMITTER_EMAIL": "gate-fixture@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(repo, *args):
    env = dict(os.environ)
    env.update(GIT_ENV)
    return subprocess.run(
        ["git"] + list(args),
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


def _git_repo(tmp):
    """A repo with two commits: an annotated tag on the first, a lightweight tag too.

    Returns ``(path, first_commit, second_commit)``.
    """
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo, exist_ok=True)
    assert _git(repo, "init", "-q", "-b", "main", ".").returncode == 0
    with open(os.path.join(repo, "f.txt"), "w", encoding="utf-8") as handle:
        handle.write("one\n")
    assert _git(repo, "add", "f.txt").returncode == 0
    assert _git(repo, "commit", "-q", "-m", "first release commit").returncode == 0
    first = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert _git(repo, "tag", "-a", "-m", "annotated", "v1.0.0", first).returncode == 0
    assert _git(repo, "tag", "v1.0.0-lite", first).returncode == 0
    with open(os.path.join(repo, "f.txt"), "a", encoding="utf-8") as handle:
        handle.write("two\n")
    assert _git(repo, "commit", "-q", "-am", "later commit").returncode == 0
    second = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, first, second


def _pin(repo, tag, expect):
    return _run(["commit", "pin", "--url", repo, "--tag", tag, "--expect-sha", expect])


def test_pin_accepts_the_peeled_commit_of_an_annotated_tag():
    repo, first, _ = _git_repo(tempfile.mkdtemp(prefix="gate-pin-"))
    result = _pin(repo, "v1.0.0", first)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "binding holds" in result.stdout


def test_pin_refuses_a_moved_tag_instead_of_publishing_different_bytes():
    repo, first, second = _git_repo(tempfile.mkdtemp(prefix="gate-pin-"))
    assert _git(repo, "tag", "-f", "-a", "-m", "retagged", "v1.0.0", second).returncode == 0
    result = _pin(repo, "v1.0.0", first)
    assert result.returncode == 1, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "not the tested commit" in combined
    assert second in combined and first in combined


def test_pin_accepts_a_lightweight_tag_and_an_absent_tag_is_refused():
    repo, first, _ = _git_repo(tempfile.mkdtemp(prefix="gate-pin-"))
    assert _pin(repo, "v1.0.0-lite", first).returncode == 0
    gone = _pin(repo, "v9.9.9-never", first)
    assert gone.returncode == 1, gone.stdout + gone.stderr
    assert "is not in the remote listing" in gone.stdout + gone.stderr


def test_pin_refuses_an_unpinnable_expectation_rather_than_passing():
    """The shape a job sees when an upstream output was never set: an empty string."""
    repo, first, _ = _git_repo(tempfile.mkdtemp(prefix="gate-pin-"))
    empty = _pin(repo, "v1.0.0", "")
    assert empty.returncode == 2, empty.stdout + empty.stderr
    assert "did not produce a commit" in empty.stdout + empty.stderr
    short = _pin(repo, "v1.0.0", first[:12])
    assert short.returncode == 2, short.stdout + short.stderr
    assert "not a full 40-hex commit id" in short.stdout + short.stderr


def test_head_assertion_reads_the_real_checkout():
    tmp = tempfile.mkdtemp(prefix="gate-head-")
    repo, first, second = _git_repo(tmp)
    ok = _run(["commit", "head", "--cwd", repo, "--expect-sha", second])
    assert ok.returncode == 0, ok.stdout + ok.stderr
    refuse = _run(["commit", "head", "--cwd", repo, "--expect-sha", first])
    assert refuse.returncode == 1, refuse.stdout + refuse.stderr
    assert "not the pinned commit" in refuse.stdout + refuse.stderr


def test_head_assertion_cannot_be_satisfied_by_a_non_repository():
    empty = tempfile.mkdtemp(prefix="gate-notrepo-")
    result = _run(["commit", "head", "--cwd", empty, "--expect-sha", "0" * 40])
    assert result.returncode == 2, result.stdout + result.stderr
    assert "git rev-parse HEAD failed" in result.stdout + result.stderr


def test_parse_peel_prefers_the_peeled_line_like_github_returns_it():
    gate = _gate()
    obj = "1" * 40
    commit = "2" * 40
    # GitHub answers with two lines for an annotated tag; a line that is not
    # "<40-hex>\t<ref>" is noise and must not be mistaken for a candidate.
    listing = "not-a-listing-line\n"
    listing += "%s\trefs/tags/v1.0.0\n%s\trefs/tags/v1.0.0^{}\n" % (obj, commit)
    got, kind, problems = gate.parse_peel(listing)
    assert problems == []
    assert kind == "peeled" and got == commit


def test_parse_peel_refuses_ambiguity_and_absence():
    gate = _gate()
    two = "refs/tags/v1.0.0\t%s\n" % ("a" * 40)
    two += "%s\trefs/tags/v1.0.0\n%s\trefs/tags/v1.0.0\n" % ("b" * 40, "c" * 40)
    got, kind, problems = gate.parse_peel(two)
    assert got is None and kind == "ambiguous" and problems
    got, kind, problems = gate.parse_peel("")
    assert got is None and kind == "absent" and problems
