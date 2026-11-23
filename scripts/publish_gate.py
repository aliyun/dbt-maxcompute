#!/usr/bin/env python3
"""Fail-closed gate for the manual publish path: prove live MaxCompute evidence exists.

Why this file exists: ``tests/conftest.py`` turns an unusable profile into
``pytest.skip`` for every case (``blocked_reason()`` -> ``pytest.skip``), and pytest
exits 0 when every selected test was skipped. So "the functional step succeeded" is
not evidence that a live test ran - a profile that is nonempty but incomplete (say
``{}``) walks exactly that path. This module reads machine-readable results instead of
exit codes, and the publish workflow refuses when the live evidence is missing.

Only the standard library is used, and profiles themselves are validated by the shared
``tests/maxcompute_gating.py`` (single source for "is this profile usable"), which the
workflow runs as its own required step. Nothing here prints credential material.

Command line::

    python scripts/publish_gate.py functional --xml dist-results/report-functional.xml \
        --require test_core,test_python_model,test_udf
    python scripts/publish_gate.py commit pin  --tag v1.12.2 --expect-sha <40-hex>
    python scripts/publish_gate.py commit head --expect-sha <40-hex>

Exit codes: 0 the evidence is sufficient and the commit binding holds, 1 the evidence is
insufficient (skips, no passes, a required suite produced nothing) or the tag no longer
identifies the tested commit, 2 the input itself is unusable (missing/unparsable report,
no testcase at all, a malformed expectation, git could not be asked).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Sequence, Tuple

_SHA = re.compile(r"^[0-9a-f]{40}$")

#: skipped/failed/error markers inside a JUnit ``testcase`` element
_MARKERS = ("failure", "error", "skipped")


def canonical(value: str) -> str:
    """Put a suite reference into one comparable dotted form.

    Needed because the two sides of the comparison are not spelled alike. pytest 9's
    default JUnit family (``classes``) writes ``classname="tests.functional.test_core"``
    and **no** ``file`` attribute, while a workflow argument is more readable as a path
    (``tests/functional/test_core.py``). Comparing those with a plain substring test
    rejects a genuinely passing report, which is exactly what this gate must never do:
    a gate that only fires on the synthetic shape it was written against is a gate that
    will be switched off the first time it cries wolf.
    """
    text = (value or "").strip().replace("\\", "/")
    if text.endswith(".py"):
        text = text[:-3]
    text = text.replace("/", ".")
    return text.lstrip(".")


def _classify(case: ET.Element) -> Tuple[str, str]:
    """Return ``(state, reason)`` for one ``testcase`` element."""
    for marker in _MARKERS:
        node = case.find(marker)
        if node is not None:
            reason = (node.get("message") or node.text or "").strip().replace("\n", " ")
            return ("skipped" if marker == "skipped" else marker), reason
    return "passed", ""


def read_results(path: str) -> List[Dict[str, str]]:
    """Parse a pytest JUnit XML report into flat case records.

    Raises ``FileNotFoundError`` / ``ET.ParseError`` so the caller can distinguish
    "no report" (collection blew up) from "report says nothing ran".
    """
    tree = ET.parse(path)
    cases: List[Dict[str, str]] = []
    for case in tree.iter("testcase"):
        state, reason = _classify(case)
        cases.append(
            {
                "name": case.get("name") or "",
                "classname": case.get("classname") or "",
                "file": case.get("file") or "",
                "state": state,
                "reason": reason,
            }
        )
    return cases


def evaluate(
    cases: Sequence[Dict[str, str]],
    required: Sequence[str],
    allowed_skips: Sequence[str] = (),
    min_passed: int = 1,
) -> List[str]:
    """Return the list of reasons this evidence must not be used for publishing."""
    problems: List[str] = []
    if not cases:
        return ["the report contains no testcase element, so nothing was executed"]

    passed = [c for c in cases if c["state"] == "passed"]
    skipped = [c for c in cases if c["state"] == "skipped"]
    broken = [c for c in cases if c["state"] in ("failure", "error")]

    if broken:
        problems.append(
            "%d case(s) failed or errored: %s"
            % (len(broken), "; ".join(_label(c) for c in broken[:5]))
        )
    if len(passed) < min_passed:
        problems.append(
            "only %d live case(s) passed, %d required (skipped: %d)"
            % (len(passed), min_passed, len(skipped))
        )

    # A skip is only acceptable when it was allowed on purpose, and that judgement does
    # not depend on whether the same suite happened to have another case pass: an
    # unapproved skip sitting next to a pass is still a live test that never ran.
    unapproved = [c for c in skipped if not _allowed(c, allowed_skips)]
    if unapproved:
        problems.append(
            "%d skipped case(s) without an explicit --allow-skip: %s"
            % (
                len(unapproved),
                "; ".join(
                    "%s (%s)" % (_label(c), c["reason"][:60] or "no reason")
                    for c in unapproved[:5]
                ),
            )
        )

    for token in required:
        mine = [c for c in cases if _matches(c, token)]
        if not mine:
            seen = sorted({ident for c in cases for ident in _case_ids(c)})
            problems.append(
                "required suite %r produced no result at all (report has %d case(s); suites present: %s)"
                % (token, len(cases), ", ".join(seen[:5]) or "none")
            )
            continue
        if not [c for c in mine if c["state"] == "passed"]:
            only_allowed_skips = all(
                c["state"] == "skipped" and _allowed(c, allowed_skips) for c in mine
            )
            if only_allowed_skips:
                continue  # explicitly allowed capability skip for the whole suite
            problems.append(
                "required suite %r passed nothing (cases: %s)"
                % (token, ", ".join("%s=%s" % (c["name"], c["state"]) for c in mine[:5]))
            )
    return problems


# -------------------------------------------------------------------------------------
# commit binding: prove that every job in one run used the same commit the tag names
# -------------------------------------------------------------------------------------


def ls_remote(url: str, tag: str) -> Tuple[int, str, str]:
    """Ask the remote for one tag name, in both shapes a tag name can occupy.

    An annotated tag puts two lines in the listing (the tag object and the commit it
    peels to), a lightweight tag puts one. Asking for both explicitly is what stops a
    tag *name* from being mistaken for a commit.
    """
    proc = subprocess.run(
        ["git", "ls-remote", url, "refs/tags/%s" % tag, "refs/tags/%s^{}" % tag],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def parse_peel(listing: str) -> Tuple[Optional[str], str, List[str]]:
    """Split an ``ls-remote`` listing into ``(commit, kind, problems)``.

    ``kind`` is ``peeled`` when the commit came from a ``^{}`` line and ``lightweight``
    when the tag name is the commit itself. Absence and ambiguity come back as problems
    instead of a guess: guessing here means publishing bytes nothing tested.
    """
    direct: List[str] = []
    peeled: List[str] = []
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) != 2 or not _SHA.match(parts[0]):
            continue
        sha, ref = parts
        if ref.endswith("^{}"):
            if sha not in peeled:
                peeled.append(sha)
        elif sha not in direct:
            direct.append(sha)
    if not direct and not peeled:
        return None, "absent", ["the tag name is not in the remote listing"]
    if len(peeled) > 1:
        return None, "ambiguous", ["the peeled ref resolved to %d commits" % len(peeled)]
    if len(direct) > 1:
        return None, "ambiguous", ["the tag name resolved to %d commits" % len(direct)]
    if peeled:
        return peeled[0], "peeled", []
    return direct[0], "lightweight", []


def check_pin(listing: str, tag: str, expected_sha: str) -> List[str]:
    """Reasons this tag must not be published as ``expected_sha`` right now."""
    commit, kind, problems = parse_peel(listing)
    if problems:
        return ["cannot bind tag %r to a commit: %s" % (tag, problem) for problem in problems]
    if commit != expected_sha:
        return [
            "tag %r now identifies %s (%s tag), not the tested commit %s"
            % (tag, commit, kind, expected_sha)
        ]
    return []


def check_head(actual_head: Optional[str], expected_sha: str) -> List[str]:
    """Reasons this checkout is not the commit preflight pinned."""
    if not actual_head:
        return ["this checkout does not resolve a HEAD commit"]
    if actual_head != expected_sha:
        return ["this checkout is at %s, not the pinned commit %s" % (actual_head, expected_sha)]
    return []


def resolve_head(cwd: str) -> Tuple[Optional[str], Optional[str]]:
    proc = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"], capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:160]
        return None, "git rev-parse HEAD failed: %s" % detail
    sha = proc.stdout.strip()
    if not _SHA.match(sha):
        return None, "git rev-parse HEAD returned a non-commit value %r" % sha[:60]
    return sha, None


def _validate_expect_sha(value: str) -> Optional[str]:
    """An empty expectation is the failure mode of an unresolved job output, not a pass."""
    if not value:
        return "--expect-sha is empty; the upstream pin did not produce a commit"
    if not _SHA.match(value):
        return "--expect-sha %r is not a full 40-hex commit id" % value[:60]
    return None


def command_commit(args: argparse.Namespace) -> int:
    expected = args.expect_sha
    bad = _validate_expect_sha(expected)
    if bad:
        print("::error::publish gate refused: %s" % bad, file=sys.stderr)
        return 2
    if args.what == "pin":
        url = args.url or ("https://github.com/%s" % args.repo if args.repo else "")
        if not url:
            print(
                "::error::publish gate refused: need --repo owner/name or --url", file=sys.stderr
            )
            return 2
        rc, listing, err = ls_remote(url, args.tag)
        if rc != 0:
            detail = err.strip()[:160]
            print(
                "::error::publish gate refused: cannot ask the remote about tag %r: %s"
                % (args.tag, detail),
                file=sys.stderr,
            )
            return 2
        problems = check_pin(listing, args.tag, expected)
        if problems:
            for problem in problems:
                print("::error::publish gate refused: %s" % problem, file=sys.stderr)
            return 1
        print("tag %s still identifies %s: binding holds" % (args.tag, expected))
        return 0
    head, err = (args.head, None) if args.head else resolve_head(args.cwd)
    if err:
        print("::error::publish gate refused: %s" % err, file=sys.stderr)
        return 2
    problems = check_head(head, expected)
    if problems:
        for problem in problems:
            print("::error::publish gate refused: %s" % problem, file=sys.stderr)
        return 1
    print("checkout is at the pinned commit %s: binding holds" % expected)
    return 0


def _label(case: Dict[str, str]) -> str:
    return "%s::%s" % (case["classname"] or case["file"] or "?", case["name"])


def _case_ids(case: Dict[str, str]) -> List[str]:
    """Every canonical name this case is known by (classname, file, and the module part)."""
    ids = []
    for raw in (case.get("classname") or "", case.get("file") or ""):
        if not raw:
            continue
        ids.append(canonical(raw))
    return [i for i in ids if i]


def _matches(case: Dict[str, str], token: str) -> bool:
    """Does this case belong to suite ``token``?

    Matches the module itself and anything nested under it (``...test_core.TestFoo``),
    but never a sibling whose name merely starts with the same letters.
    """
    want = canonical(token)
    if not want:
        return False
    return any(ident == want or ident.startswith(want + ".") for ident in _case_ids(case))


def _allowed(case: Dict[str, str], allowed_skips: Sequence[str]) -> bool:
    return any(token and token in case["reason"] for token in allowed_skips)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="publish_gate.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    fun = sub.add_parser("functional", help="judge a functional JUnit report")
    fun.add_argument("--xml", required=True, help="path to the pytest --junitxml report")
    fun.add_argument(
        "--require",
        default="",
        help="comma separated tokens; each must appear in some case's classname/file and have >=1 passed case",
    )
    fun.add_argument(
        "--allow-skip",
        action="append",
        default=[],
        help="a skip reason substring that makes a skipped required suite acceptable (repeatable)",
    )
    fun.add_argument(
        "--min-passed", type=int, default=1, help="minimum passing cases overall (default 1)"
    )
    commit = sub.add_parser("commit", help="prove a job uses the commit the tag names")
    commit.add_argument(
        "what", choices=["pin", "head"], help="pin: tag -> commit, head: checkout -> commit"
    )
    commit.add_argument("--tag", help="tag name used as the release label (pin)")
    commit.add_argument("--repo", help="owner/name, used to build the https remote (pin)")
    commit.add_argument("--url", help="explicit remote url, overrides --repo (pin)")
    commit.add_argument(
        "--expect-sha", required=True, dest="expect_sha", help="the pinned commit id"
    )
    commit.add_argument("--cwd", default=".", help="checkout to read HEAD from (head)")
    commit.add_argument("--head", help="pre-read HEAD value; tests only (head)")
    args = parser.parse_args(argv)

    if args.command == "commit":
        return command_commit(args)

    required = [t.strip() for t in args.require.split(",") if t.strip()]
    try:
        cases = read_results(args.xml)
    except FileNotFoundError:
        print(
            "::error::no functional report at %s: the suite never ran" % args.xml, file=sys.stderr
        )
        return 2
    except ET.ParseError as exc:
        print(
            "::error::functional report %s is not parsable: %s" % (args.xml, exc), file=sys.stderr
        )
        return 2

    counts: Dict[str, int] = {}
    for case in cases:
        counts[case["state"]] = counts.get(case["state"], 0) + 1
    print("report %s: %d case(s), %s" % (args.xml, len(cases), counts or "none"))
    problems = evaluate(cases, required, args.allow_skip, args.min_passed)
    if problems:
        for problem in problems:
            print("::error::publish gate refused: %s" % problem, file=sys.stderr)
        return 1
    print(
        "publish gate satisfied for %s (required: %s)" % (args.xml, ", ".join(required) or "none")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
