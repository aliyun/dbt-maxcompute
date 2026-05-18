"""Adapter-side unit test pinning the get_columns_in_relation fix.

The merge + partitioned-table bug stemmed from PyODPS' simple_columns
property excluding partition columns. This test guards both ends of the
fix:

  1. non-auto partition columns ARE returned (so merge INSERT clauses
     contain them, avoiding ODPS-0123031).
  2. auto-derived partition columns are NOT returned (they're populated
     by MaxCompute from a source data column via trunc_time; including
     them in INSERT clauses would fail).

Live PyODPS table objects can't be instantiated without an HTTP round
trip, so the test exercises the filter logic directly against a
hand-built schema. The adapter method then trivially wraps the same
filter.
"""
import unittest

from odps.models.table import TableSchema


def _filter_partition_cols_like_adapter(schema):
    """Mirror impl.py:get_columns_in_relation partition handling."""
    cols = [c.name for c in schema.simple_columns]
    for p in schema._partitions or []:
        if getattr(p, "_generate_expression", None):
            continue
        cols.append(p.name)
    return cols


class TestGetColumnsPartitionFilter(unittest.TestCase):
    def test_non_auto_partition_columns_are_included(self):
        schema = TableSchema.from_lists(
            ["id", "name", "event_time"],
            ["bigint", "string", "timestamp"],
            ["pt"],
            ["string"],
        )
        self.assertEqual(
            _filter_partition_cols_like_adapter(schema),
            ["id", "name", "event_time", "pt"],
        )

    def test_multi_field_non_auto_partitions_are_included(self):
        schema = TableSchema.from_lists(
            ["id", "value"],
            ["bigint", "double"],
            ["region", "dt"],
            ["string", "string"],
        )
        self.assertEqual(
            _filter_partition_cols_like_adapter(schema),
            ["id", "value", "region", "dt"],
        )

    def test_auto_derived_partition_is_excluded(self):
        schema = TableSchema.from_lists(
            ["id", "event_time"],
            ["bigint", "timestamp"],
            ["pt_month"],
            ["string"],
        )
        # Simulate the server marking the partition column as auto-generated.
        schema._partitions[0]._generate_expression = (
            'trunc_time(`event_time`, "month")'
        )
        self.assertEqual(
            _filter_partition_cols_like_adapter(schema),
            ["id", "event_time"],
        )

    def test_unpartitioned_table_unaffected(self):
        schema = TableSchema.from_lists(
            ["id", "value"], ["bigint", "double"]
        )
        self.assertEqual(
            _filter_partition_cols_like_adapter(schema), ["id", "value"]
        )


if __name__ == "__main__":
    unittest.main()
