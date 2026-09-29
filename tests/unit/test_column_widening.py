"""Which column widenings the adapter may offer, and which it must never attempt.

`expand_target_column_types` decides, per column, whether an incoming value fits into
the target's declared type. The rules came out of a real-server measurement matrix
(`alter table ... change column` on plain / partitioned / transactional / Append Delta /
PK Delta tables, in both directions), and the two halves of it are guarded separately
because they fail differently:

* widening a column that is fine to widen must produce a `varchar`/`char` of the
  incoming width -- rendering plain `string` instead would rewrite every text column of
  every incremental model, which MaxCompute cannot undo;
* offering a DDL for a column the server refuses to re-type (primary key, partition)
  turns a green run into a failed one, so those must be skipped *and* reported: the
  alternative is losing characters in silence, since an over-long value inserted into a
  narrow column succeeds and keeps the prefix.
"""

import pytest
from dbt_common.exceptions import DbtRuntimeError

from dbt.adapters.maxcompute.column import MaxComputeColumn
from dbt.adapters.maxcompute.impl import MaxComputeAdapter


def column(dtype, char_size=None, **kwargs):
    return MaxComputeColumn(column="c", dtype=dtype, char_size=char_size, **kwargs)


VARCHAR_10 = lambda **kw: column("varchar(10)", 10, **kw)
VARCHAR_20 = lambda **kw: column("varchar(20)", 20, **kw)
STRING = lambda **kw: column("string", None, **kw)
CHAR_5 = lambda **kw: column("char(5)", 5, **kw)
CHAR_10 = lambda **kw: column("char(10)", 10, **kw)


class TestDeclaredWidth:
    def test_unbounded_string_has_no_declared_size(self):
        # `Column.string_size()` answers 256 for a column whose char_size is unset, so it
        # cannot be used to tell "unbounded" from "256 wide" -- `declared_size` must.
        assert STRING().declared_size() is None
        assert STRING().string_size() == 256
        assert VARCHAR_10().declared_size() == 10

    def test_char_width_is_recorded(self):
        # A char column with an unknown width would read as unbounded and drop out of
        # every comparison, truncating in silence.
        assert CHAR_5().declared_size() == 5
        assert CHAR_5().will_truncate(CHAR_10())


class TestWhatNeedsWidening:
    @pytest.mark.parametrize(
        "target,source,expected",
        [
            (VARCHAR_10(), VARCHAR_20(), True),  # same family, roomier
            (VARCHAR_20(), VARCHAR_10(), False),  # narrower incoming: it fits
            (VARCHAR_10(), VARCHAR_10(), False),  # nothing to do
            (STRING(), STRING(), False),  # both unbounded: never issue a DDL
            (STRING(), VARCHAR_20(), False),  # unbounded already holds it
            (VARCHAR_10(), STRING(), True),  # unknown incoming length cannot fit a bound
            (CHAR_5(), CHAR_10(), True),
        ],
    )
    def test_can_expand_to_compares_width(self, target, source, expected):
        assert target.can_expand_to(source) is expected

    @pytest.mark.parametrize(
        "blocked", [VARCHAR_10(is_partition=True), VARCHAR_10(is_primary_key=True)]
    )
    def test_columns_the_server_will_not_re_type_are_never_offered_a_ddl(self, blocked):
        assert blocked.can_expand_to(VARCHAR_20()) is False
        assert blocked.can_expand_to(STRING()) is False
        assert blocked.widening_blocked_because() is not None

    def test_a_plain_column_has_no_block(self):
        assert VARCHAR_10().widening_blocked_because() is None


class TestWidenedType:
    @pytest.mark.parametrize(
        "target,source,expected",
        [
            (VARCHAR_10(), VARCHAR_20(), "varchar(20)"),
            (CHAR_5(), CHAR_10(), "char(10)"),  # family kept while it holds the width
            (CHAR_5(), VARCHAR_20(), "char(20)"),  # 20 chars fit in char(20)
            (STRING(), VARCHAR_20(), None),
            (VARCHAR_20(), VARCHAR_10(), None),
            (VARCHAR_10(is_primary_key=True), VARCHAR_20(), None),
            (VARCHAR_10(is_partition=True), STRING(), None),
        ],
    )
    def test_default_mode(self, target, source, expected):
        assert MaxComputeAdapter._widened_string_type(target, source, "bounded") == expected

    def test_string_type_keeps_a_known_width(self):
        # The old rendering returned `string` for every width, which is what made the
        # widening unsafe: the answer must carry the width it was asked for.
        assert MaxComputeColumn.string_type(20) == "varchar(20)"
        assert MaxComputeColumn.string_type(0) == "string"
        assert MaxComputeColumn.string_type(None) == "string"


class TestModes:
    def test_the_default_never_drops_a_declared_width(self):
        # `string` to hold a value is the one widening MaxCompute cannot undo, so it is
        # opt-in rather than the default.
        assert MaxComputeAdapter._widened_string_type(VARCHAR_10(), STRING(), "bounded") is None
        assert (
            MaxComputeAdapter._widened_string_type(VARCHAR_10(), VARCHAR_20(), "bounded")
            == "varchar(20)"
        )

    def test_widen_mode_gives_up_the_bound_for_the_value(self):
        assert MaxComputeAdapter._widened_string_type(VARCHAR_10(), STRING(), "widen") == "string"

    def test_declining_the_default_says_why_and_how_to_change_it(self):
        reason = MaxComputeAdapter._widening_decline_reason(VARCHAR_10(), STRING(), "bounded")
        assert reason is not None and "expand_column_types" in reason

    def test_declined_because_of_the_column_kind_beats_the_mode_reason(self):
        target = VARCHAR_10(is_partition=True)
        reason = MaxComputeAdapter._widening_decline_reason(target, STRING(), "widen")
        assert "partition column" in reason

    def test_a_column_that_fits_produces_no_notice(self):
        assert VARCHAR_20().will_truncate(VARCHAR_10()) is False
        assert VARCHAR_10().will_truncate(VARCHAR_10()) is False
        assert MaxComputeAdapter._widening_decline_reason(STRING(), VARCHAR_10(), "widen") is None

    def test_notice_names_the_width_and_the_fate_of_the_value(self):
        notice = VARCHAR_10().widening_notice(STRING(), "the server will not re-type it")
        assert "varchar/char(10)" in notice
        assert "unbounded string" in notice
        assert "truncated" in notice

    def test_unknown_mode_is_rejected_earlier_than_a_per_column_decision(self):
        # `expand_target_column_types` returns before reading any relation; an unknown
        # value must be a loud config error rather than a silent default.
        with pytest.raises(DbtRuntimeError):
            MaxComputeAdapter.expand_target_column_types(_SelfStub(), "a", "b", mode="wider")

    def test_known_modes_are_accepted(self):
        for mode in ("bounded", "widen", "off"):
            MaxComputeAdapter.expand_target_column_types(_SelfStub(), "a", "b", mode=mode)


class _SelfStub:
    """Enough of an adapter for the mode checks, which run before any metadata read."""

    Relation = object  # the argument-type guard is satisfied by anything under `object`

    def get_columns_in_relation(self, relation):
        return []
