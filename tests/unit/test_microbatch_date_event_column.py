"""The DATE-event-column refusal is decided by config, so it can be pinned without a server.

`mc_validate_microbatch_config` runs while the materialization is being built, and the only facts it
has are the model's own declared `partition_by` and `event_time`. This renders the macro itself with
Jinja (the `do` extension, as dbt configures it) and a stub `exceptions`, so the truth table below
runs on a machine with no MaxCompute credentials:

* the refused case is the one that silently writes nothing: the event field is the auto-partition
  field **and** is declared `date`;
* the types that work today - `timestamp`, `datetime`, `timestamp_ntz` - must keep compiling, or
  this would be a regression for every existing model;
* a `date` partition column that is *not* the event field is a different situation (the window is
  compared against something else), and a non-auto partition type carries no time semantics at all;
  neither is refused here.

The end-to-end shape - an empty target behind a successful run - is measured on a real project in
`tests/functional/adapter/incremental/test_microbatch_date_partition.py`.
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, StrictUndefined

_MACRO_PATH = (
    Path(__file__).resolve().parents[2]
    / "dbt/include/maxcompute/macros/materializations/incremental/incremental_strategy/microbatch.sql"
)
_REASON = "cannot batch on a `date` event column"


class _CompilerError(Exception):
    """Stand-in for dbt's `exceptions.raise_compiler_error`."""


def _raise_compiler_error(message):
    raise _CompilerError(message)


def _load_macro():
    """The text of `mc_validate_microbatch_config`, nothing else from the file."""
    source = _MACRO_PATH.read_text()
    start = source.index("{% macro mc_validate_microbatch_config(")
    following = source.find("{% macro ", start + 1)
    return source[start : following if following != -1 else len(source)]


def _validate(fields, data_types, event_time, granularity="day", batch_size="day"):
    env = Environment(extensions=["jinja2.ext.do"], undefined=StrictUndefined)
    template = env.from_string(
        _load_macro()
        + "\n{{ mc_validate_microbatch_config(partition_by, batch_size, event_time) }}"
    )

    def auto_partition():
        return (
            all(
                t.lower() in ("timestamp", "date", "datetime", "timestamp_ntz") for t in data_types
            )
            and len(fields) == 1
        )

    partition_by = SimpleNamespace(
        fields=fields,
        data_types=data_types,
        granularity=granularity,
        auto_partition=auto_partition,
    )
    template.render(
        partition_by=partition_by,
        batch_size=batch_size,
        event_time=event_time,
        exceptions=SimpleNamespace(raise_compiler_error=_raise_compiler_error),
    )


def test_macro_source_is_found():
    assert _REASON in _load_macro(), "the refusal moved out of the macro this test renders"


@pytest.mark.parametrize("event_field", ["event_date", "`event_date`", "EVENT_DATE"])
def test_date_event_column_is_refused(event_field):
    with pytest.raises(_CompilerError) as excinfo:
        _validate(["event_date"], ["date"], event_field)
    message = str(excinfo.value)
    assert _REASON in message
    # The message has to carry the way out, not only the veto - including where the cast belongs,
    # which is measured (a cast inside this model's own SELECT leaves the batch empty).
    assert "upstream relation's `event_time` column" in message
    assert "too late" in message
    assert "data_type: timestamp" in message


@pytest.mark.parametrize("data_type", ["timestamp", "datetime", "timestamp_ntz"])
def test_types_that_work_today_keep_compiling(data_type):
    _validate(["event_date"], [data_type], "event_date")


def test_date_partition_column_that_is_not_the_event_field_is_not_refused():
    # The window is compared against `created_at`; the DATE column only names partitions. Refusing
    # this would stop a model whose batches do select rows.
    _validate(["event_date"], ["date"], "created_at")


def test_non_auto_partition_types_are_not_refused():
    _validate(["dt"], ["string"], "dt")
