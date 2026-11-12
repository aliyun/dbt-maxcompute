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

Exit codes: 0 evidence is sufficient, 1 evidence is insufficient (skips, no passes,
a required suite produced nothing), 2 the input itself is unusable (missing/unparsable
report, no testcase at all).
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Sequence, Tuple

#: skipped/failed/error markers inside a JUnit ``testcase`` element
_MARKERS = ("failure", "error", "skipped")


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
            "%d case(s) failed or errored: %s" % (len(broken), "; ".join(_label(c) for c in broken[:5]))
        )
    if len(passed) < min_passed:
        problems.append(
            "only %d live case(s) passed, %d required (skipped: %d)" % (len(passed), min_passed, len(skipped))
        )

    for token in required:
        mine = [c for c in cases if _matches(c, token)]
        if not mine:
            problems.append("required suite %r produced no result at all" % token)
            continue
        if not [c for c in mine if c["state"] == "passed"]:
            only_skips = all(c["state"] == "skipped" for c in mine)
            if only_skips and allowed_skips and all(_allowed(c, allowed_skips) for c in mine):
                continue  # explicitly allowed capability skip
            problems.append(
                "required suite %r passed nothing (cases: %s)"
                % (token, ", ".join("%s=%s" % (c["name"], c["state"]) for c in mine[:5]))
            )
    return problems


def _label(case: Dict[str, str]) -> str:
    return "%s::%s" % (case["classname"] or case["file"] or "?", case["name"])


def _matches(case: Dict[str, str], token: str) -> bool:
    haystack = "%s %s" % (case["classname"], case["file"])
    return token in haystack


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
    fun.add_argument("--min-passed", type=int, default=1, help="minimum passing cases overall (default 1)")
    args = parser.parse_args(argv)

    required = [t.strip() for t in args.require.split(",") if t.strip()]
    try:
        cases = read_results(args.xml)
    except FileNotFoundError:
        print("::error::no functional report at %s: the suite never ran" % args.xml, file=sys.stderr)
        return 2
    except ET.ParseError as exc:
        print("::error::functional report %s is not parsable: %s" % (args.xml, exc), file=sys.stderr)
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
    print("publish gate satisfied for %s (required: %s)" % (args.xml, ", ".join(required) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
