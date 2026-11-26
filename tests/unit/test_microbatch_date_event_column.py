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

* the refusal is scoped to SQL models. A MaxFrame Python model binds its window in Python -
  pandas Timestamp bounds against the event series after ``astype("datetime64[ns]")``,
  ``relations/table/create.sql:102-116`` - so it never performs the DATE-versus-timestamp-text
  comparison this message describes; any language other than an explicit ``python`` keeps the
  check, so an unknown one fails closed rather than silently running empty.
  Whether the Python path returns the right rows for a DATE column is not measured here.

The end-to-end shape - an empty target behind a successful run - is measured on a real project in
`tests/functional/adapter/incremental/test_microbatch_date_partition.py`.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, StrictUndefined

_MACRO_PATH = (
    Path(__file__).resolve().parents[2]
    / "dbt/include/maxcompute/macros/materializations/incremental/incremental_strategy/microbatch.sql"
)
_REASON = "notes (informational, this does not stop the model)"


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


def _validate(fields, data_types, event_time, granularity="day", batch_size="day", language="sql"):
    env = Environment(extensions=["jinja2.ext.do"], undefined=StrictUndefined)
    template = env.from_string(
        _load_macro()
        + "\n{{ mc_validate_microbatch_config(partition_by, batch_size, event_time, language) }}"
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
    warned: list = []
    logged: list = []

    def _log(message, info=False, debug=False):
        # dbt's global `log` takes the message positionally and `info`/`debug` as keywords; the stub
        # keeps both so the cells can assert the level, not only the text.
        logged.append((message, info, debug))

    template.render(
        partition_by=partition_by,
        batch_size=batch_size,
        event_time=event_time,
        language=language,
        exceptions=SimpleNamespace(raise_compiler_error=_raise_compiler_error, warn=warned.append),
        log=_log,
    )
    assert not warned, (
        "the declaration-only diagnostic must not reach `exceptions.warn`: dbt escalates "
        "JinjaLogWarning under --warn-error / warn_error_options, which would give a YAML "
        "declaration the power to stop a model whose column is physically an instant"
    )
    assert all(info for _, info, _ in logged), [entry for entry in logged if not entry[1]]
    return [message for message, _, _ in logged]


def test_macro_source_is_found():
    assert _REASON in _load_macro(), "the refusal moved out of the macro this test renders"


@pytest.mark.parametrize("event_field", ["event_date", "`event_date`", "EVENT_DATE"])
def test_a_declared_date_event_column_is_logged_and_does_not_stop_the_model(event_field):
    """A declaration is not the authority any more, so it cannot stop a model on its own.

    The refusal for a real `DATE` column lives in the adapter's filter path, where the warehouse can
    be asked; this check only says early, in the model's own terms, that the shape is known-broken.
    Two absences are the point: no error (the case this guard used to refuse - a physically
    instant column declared `date`, which is what the real `ref()` path produces when YAML and the
    table disagree - has to compile) and no `exceptions.warn`, because an escalable warning is
    only one CLI flag away from being that same error again.
    """
    warned = _validate(["event_date"], ["date"], event_field)
    assert len(warned) == 1, warned
    message = str(warned[0])
    assert _REASON in message
    # The message has to carry the way out, not only the veto - including where the cast belongs,
    # which is measured (a cast inside this model's own SELECT leaves the batch empty).
    assert "upstream relation's `event_time` column" in message
    assert "too late" in message
    assert "data_type: timestamp" in message


@pytest.mark.parametrize("data_type", ["timestamp", "datetime", "timestamp_ntz"])
def test_types_that_work_today_keep_compiling(data_type):
    assert _validate(["event_date"], [data_type], "event_date") == []


def test_date_partition_column_that_is_not_the_event_field_is_not_diagnosed():
    # The window is compared against `created_at`; the DATE column only names partitions. Refusing
    # this would stop a model whose batches do select rows.
    _validate(["event_date"], ["date"], "created_at")


def test_non_auto_partition_types_are_not_diagnosed():
    assert _validate(["dt"], ["string"], "dt") == []


def test_python_model_hears_no_sql_diagnostic_at_all():
    # The message's justification is a property of SQL boundary text; a MaxFrame model filters in
    # Python, so even the warning would describe a comparison it never performs.
    assert _validate(["event_date"], ["date"], "event_date", language="python") == []


def test_a_python_date_model_keeps_the_other_checks():
    # Narrowing the DATE case must not quietly drop the checks that apply to both paths.
    with pytest.raises(_CompilerError) as excinfo:
        _validate(["event_date"], ["date"], "event_date", language="python", batch_size="hour")
    assert "same granularity" in str(excinfo.value)


@pytest.mark.parametrize("language", ["sql", None, "python2"])
def test_anything_that_is_not_explicitly_python_still_hears_the_diagnostic(language):
    """The diagnostic itself is not silently skipped for a language this repo does not know."""
    warned = _validate(["event_date"], ["date"], "event_date", language=language)
    assert len(warned) == 1, (language, warned)
    assert _REASON in str(warned[0])
