from dataclasses import dataclass
from typing import Optional, TypeVar, Any

from dbt.adapters.base.column import Column
from odps.models.table import TableSchema
from odps.types import Char, Decimal, Varchar

Self = TypeVar("Self", bound="MaxComputeColumn")

# MaxCompute's own limits, measured rather than copied: a varchar column tops out at
# 65535 characters and a char column at 255, and a `change column` that would shrink
# existing data is rejected by the server instead of truncating it.
VARCHAR_MAX_SIZE = 65535
CHAR_MAX_SIZE = 255


@dataclass
class MaxComputeColumn(Column):
    table_column: TableSchema.TableColumn = None
    comment: str = ""
    # MaxCompute refuses to change the type of these two kinds of column at all (see
    # `expand_column_types` in impl.py for the measurements). They are recorded here so
    # the widening pass can skip them instead of submitting a DDL that fails the run.
    is_partition: bool = False
    is_primary_key: bool = False

    TYPE_LABELS = {
        "TEXT": "STRING",
        "INTEGER": "INT",
        "BOOL": "BOOLEAN",
        "NUMERIC": "DECIMAL",
        "REAL": "FLOAT",
    }

    @property
    def quoted(self):
        return "`{}`".format(self.column)

    def literal(self, value):
        return "cast({} as {})".format(value, self.data_type)

    @classmethod
    def numeric_type(cls, dtype: str, precision: Any, scale: Any) -> str:
        return "DECIMAL({}, {})".format(precision, scale)

    def is_string(self) -> bool:
        lower = self.dtype.lower()
        if lower.startswith("char") or lower.startswith("varchar"):
            return True
        return lower in [
            "string",
            "text",
            "character varying",
            "character",
            "char",
            "varchar",
        ]

    def is_integer(self) -> bool:
        return self.dtype.lower() in [
            # real types
            "tinyint",
            "smallint",
            "integer",
            "bigint",
            "smallserial",
            "serial",
            "bigserial",
            # aliases
            "int",
            "int2",
            "int4",
            "int8",
            "serial2",
            "serial4",
            "serial8",
        ]

    def is_numeric(self) -> bool:
        lower = self.dtype.lower()
        if lower.startswith("decimal") or lower.startswith("numeric"):
            return True
        return lower in ["numeric", "decimal"]

    @classmethod
    def string_type(cls, size: int = 0) -> str:
        """The type a widened string column is asked for.

        dbt-core calls this with the *incoming* column's width. Rendering plain
        ``string`` for every width -- what this method used to do -- is what made the
        widening pass unsafe to switch on: every text column of every incremental model
        would have been rewritten to unbounded ``string``, which MaxCompute cannot undo.
        A known width therefore stays a known width.
        """
        if size and 0 < int(size) <= VARCHAR_MAX_SIZE:
            return f"varchar({int(size)})"
        return "string"

    def declared_size(self) -> Optional[int]:
        """The width this column actually declares, or None when it is unbounded.

        ``Column.string_size()`` cannot answer this: it reports 256 for any string
        column whose ``char_size`` is unset, which is exactly what an unbounded
        ``string`` is. Every width decision here goes through this method instead.
        """
        if not self.is_string():
            return None
        if self.char_size is None:
            return None
        return int(self.char_size)

    def is_unbounded_string(self) -> bool:
        """True for ``string``: it holds any length, so nothing is wider than it."""
        return self.is_string() and self.declared_size() is None

    def will_truncate(self, other_column: Self) -> bool:
        """True when a value from ``other_column`` would lose characters in this column.

        This is the question the widening pass exists to ask. An unbounded incoming
        column counts as "does not fit": nothing says how long its values are, so no
        declared width can be trusted with them.
        """
        if not self.is_string() or not other_column.is_string():
            return False
        target_size = self.declared_size()
        if target_size is None:
            return False  # unbounded: cannot truncate
        incoming_size = other_column.declared_size()
        return incoming_size is None or incoming_size > target_size

    def widening_blocked_because(self) -> Optional[str]:
        """Why this column cannot be re-typed at all, or None if it can.

        MaxCompute rejects a ``change column`` on a primary key or a partition column
        (`column id cannot be changed except for its comment because it is a primary key
        column` / `partition keys can not be changed`), so offering a widening for one
        of them is offering a DDL that fails the run.
        """
        if self.is_partition:
            return "it is a partition column"
        if self.is_primary_key:
            return "it is a primary key column"
        return None

    def can_expand_to(self, other_column: Self) -> bool:
        """True when this column both needs and allows widening to ``other_column``.

        Two things the inherited "both columns are strings" rule got wrong here: it
        ignored width, so ``string`` to ``string`` answered True and every text column
        looked due for a rewrite; and it ignored that primary key and partition columns
        cannot be re-typed at all, so a fix that started submitting the DDL would have
        failed runs that are green today.
        """
        if self.widening_blocked_because() is not None:
            return False
        return self.will_truncate(other_column)

    def widening_notice(self, other_column: Self, reason: str) -> str:
        """A one-line, actionable statement that values will be cut down."""
        target_size = self.declared_size()
        incoming_size = other_column.declared_size()
        incoming = (
            "unbounded string" if incoming_size is None else f"varchar/char({incoming_size})"
        )
        return (
            f"column {self.quoted} is declared varchar/char({target_size}) but the incoming "
            f"column is {incoming}; {reason}, so values longer than {target_size} characters "
            f"will be truncated on insert"
        )

    def __repr__(self) -> str:
        return "<MaxComputeColumn {} ({})>".format(self.name, self.dtype)

    @classmethod
    def from_odps_column(
        cls,
        column: TableSchema.TableColumn,
        is_partition: bool = False,
        is_primary_key: bool = False,
    ):
        char_size = None
        numeric_precision = None
        numeric_scale = None

        if isinstance(column.type, Decimal):
            numeric_precision = column.type.precision
            numeric_scale = column.type.scale
        elif isinstance(column.type, (Varchar, Char)):
            # `char(n)` needs its width recorded too: an unknown width reads as
            # unbounded and the column silently drops out of every width comparison.
            char_size = column.type.size_limit

        return cls(
            column=column.name,
            dtype=column.type.name.lower(),
            char_size=char_size,
            numeric_precision=numeric_precision,
            numeric_scale=numeric_scale,
            table_column=column,
            comment=column.comment,
            is_partition=is_partition,
            is_primary_key=is_primary_key,
        )
