"""Static guards for the materializations this adapter copies from dbt-core.

A copied materialization keeps working when core changes only as long as the
differences are deliberate, so the two invariants that were broken in the past
are pinned here rather than left to code review:

* `run_hooks` selects hooks by their `transaction` flag, so a phase is only
  complete when a materialization calls it twice -- once per value. A single
  `run_hooks(pre_hooks)` silently drops every `transaction: false` hook.
* core's `run_hooks` emits a literal `commit;` before the first
  outside-transaction hook. MaxCompute's SQL parser rejects that statement, so
  the copy must stay without it.
"""

import re
from pathlib import Path

MACROS = Path(__file__).resolve().parents[2] / "dbt" / "include" / "maxcompute" / "macros"

MATERIALIZATION_RE = re.compile(r"\{%-?\s*materialization\s+(\w+)")


def materialization_files():
    for path in sorted(MACROS.rglob("*.sql")):
        text = path.read_text(encoding="utf-8")
        match = MATERIALIZATION_RE.search(text)
        if match:
            yield match.group(1), path, text


def test_guard_is_looking_at_the_copied_materializations():
    names = {name for name, _path, _text in materialization_files()}
    assert {"table", "incremental", "snapshot"} <= names, names


def test_every_hook_phase_is_run_for_both_transaction_flags():
    offenders = []
    for name, path, text in materialization_files():
        for phase in ("pre_hooks", "post_hooks"):
            calls = re.findall(r"\{\{\s*run_hooks\(\s*" + phase + r"[^)]*\)\s*\}\}", text)
            if not calls:
                continue
            seen = " ".join(calls)
            missing = {
                flag
                for flag in ("inside_transaction=False", "inside_transaction=True")
                if flag not in seen
            }
            if missing:
                offenders.append(f"{name}: {phase} called without {sorted(missing)} ({path.name})")
    assert not offenders, offenders


def test_run_hooks_keeps_the_transaction_filter_but_not_the_commit_statement():
    text = (MACROS / "materializations" / "hooks.sql").read_text(encoding="utf-8")
    assert (
        "selectattr('transaction', 'equalto', inside_transaction)" in text
    ), "the flag filter is what makes the two calls per phase meaningful"
    assert not re.search(
        r"^\s*commit\s*;", text, re.M | re.I
    ), "a bare `commit;` is a parse error on MaxCompute"
