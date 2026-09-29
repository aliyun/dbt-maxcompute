"""The snapshot materialization is a hand-maintained copy of dbt-core's.

Two of its statements depend on variables that the copy must keep assigning:
`grant_config` (used by `apply_grants`) and `tblproperties` (used by both table
builds).  A cleanup edit in this repository deleted the `grant_config` line while
leaving the `apply_grants(..., grant_config, ...)` call in place, and the live
suite still passed - dbt treats the undefined value as "no grants", so snapshots
would have silently stopped applying `grants:` config.  A whole-suite green run is
not evidence that a config surface still works; this is the cheap guard.
"""

import pathlib
import re

MACRO = (
    pathlib.Path(__file__).resolve().parents[2]
    / "dbt/include/maxcompute/macros/materializations/snapshots/snapshot.sql"
)


def _materialization():
    text = MACRO.read_text(encoding="utf-8")
    start = text.index("{% materialization snapshot")
    return text[start : text.index("{% endmaterialization", start)]


def _assigned(name):
    # f-string, not %-formatting: the pattern starts with "{%" which % would eat.
    return re.search(rf"\{{%-?\s*set\s+{re.escape(name)}\s*=", _materialization())


def test_grant_config_is_still_assigned_before_apply_grants_uses_it():
    body = _materialization()
    assert (
        "apply_grants(target_relation, grant_config" in body
    ), "the call moved; update this guard"
    assert _assigned("grant_config"), (
        "`grant_config` is passed to apply_grants but never assigned - dbt reads an "
        "undefined value as 'no grants', so snapshot grants would stop silently"
    )


def test_other_copied_variables_are_still_assigned():
    for name in ("tblproperties", "strategy_name", "unique_key", "columns"):
        assert _assigned(name), f"{name} is used by the copied materialization but not assigned"
