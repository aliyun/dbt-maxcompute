"""Snapshots built on an ephemeral model, including hard deletes.

Upstream ships these four cases in
`dbt.tests.adapter.simple_snapshot.test_ephemeral_snapshot_hard_deletes`; they were not wired
here, so "snapshotting through an ephemeral model" was an open *Not verified* item in
`docs/snapshot-support.md` even though the adapter is used for it.

Only two fixtures are overridden, each because of a measured server response rather than
assumption:

* the source table has to be created `transactional` - upstream deletes a row from it, and a
  `delete` against a non-transactional table comes back as
  `ODPS-0130071 ... trying to delete from a non-transactional table is not allowed. Set
  tblproperties ("transactional" = "true") ...`, while the same statement against a
  transactional table removes the row;
* the seed rows are inserted with `cast('...' as timestamp)` - a bare string literal in a
  `timestamp` column is refused with `ODPS-0130071 ... incompatible type STRING with
  destination column`;
* `id` is declared `INT`: upstream writes `INTEGER`, which is not a MaxCompute column type at
  all - `create table t (id INTEGER, ...)` fails with `ODPS-0130161 ... invalid token 'INTEGER'`
  on plain and transactional tables alike, while `INT` and `BIGINT` both parse.

Upstream's `alter table ... add column dummy_column varchar(50) default 'dummy_value'` is kept
verbatim: that form parses on MaxCompute (a *literal* default is accepted; `default null` is a
parse error, see `test_simple_snapshot.py`).  No assertions were weakened and nothing is
skipped.
"""

import pytest
from dbt.tests.adapter.simple_snapshot.test_ephemeral_snapshot_hard_deletes import (
    BaseSnapshotEphemeralHardDeletes,
    BaseSnapshotNewColumnSpecificCheckCols,
    BaseSnapshotNewColumnTimestampStrategy,
    BaseSnapshotNewColumnWithDeletes,
)

SOURCE_CREATE_SQL = """
create table {database}.{schema}.src_customers (
    id INT,
    first_name VARCHAR(50),
    last_name VARCHAR(50),
    email VARCHAR(50),
    updated_at TIMESTAMP
) TBLPROPERTIES("transactional"="true");
"""

SOURCE_INSERT_SQL = """
insert into {database}.{schema}.src_customers (id, first_name, last_name, email, updated_at) values
(1, 'John', 'Doe', 'john.doe@example.com', cast('2023-01-01 10:00:00' as timestamp)),
(2, 'Jane', 'Smith', 'jane.smith@example.com', cast('2023-01-02 11:00:00' as timestamp)),
(3, 'Bob', 'Johnson', 'bob.johnson@example.com', cast('2023-01-03 12:00:00' as timestamp));
"""


class EphemeralHardDeletesOnMaxCompute:
    @pytest.fixture(scope="class")
    def source_create_sql(self):
        return SOURCE_CREATE_SQL

    @pytest.fixture(scope="class")
    def source_insert_sql(self):
        return SOURCE_INSERT_SQL


class TestSnapshotEphemeralHardDeletes(
    EphemeralHardDeletesOnMaxCompute, BaseSnapshotEphemeralHardDeletes
):
    """check_cols='all' + hard_deletes='new_record' over an ephemeral model."""


class TestSnapshotNewColumnTimestampStrategy(
    EphemeralHardDeletesOnMaxCompute, BaseSnapshotNewColumnTimestampStrategy
):
    """timestamp strategy + hard_deletes='new_record' after a column appears in the source."""


class TestSnapshotNewColumnSpecificCheckCols(
    EphemeralHardDeletesOnMaxCompute, BaseSnapshotNewColumnSpecificCheckCols
):
    """a new column that is deliberately not in check_cols must not create a new version."""


class TestSnapshotNewColumnWithDeletes(
    EphemeralHardDeletesOnMaxCompute, BaseSnapshotNewColumnWithDeletes
):
    """a new column and a hard delete in the same run exercise the deletion_records CTE."""
