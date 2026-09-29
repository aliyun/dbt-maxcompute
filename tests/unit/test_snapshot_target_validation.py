"""The snapshot target check is a contract, not a server round-trip.

A snapshot whose target already exists as a non-transactional table cannot keep
history: MaxCompute only runs the ``merge into`` that expires an old version on
a transactional table.  Both outcomes are pinned offline, so a contributor
without MaxCompute credentials still proves the adapter rejects the right
target with a message that names the missing property - and does not reject the
targets it cannot read.
"""

from types import SimpleNamespace

import pytest
from dbt_common.exceptions import DbtRuntimeError

from dbt.adapters.maxcompute.impl import MaxComputeAdapter
from dbt.adapters.maxcompute.relation import MaxComputeRelation

SNAPSHOT_COLUMNS = [
    "id",
    "name",
    "amount",
    "updated_at",
    "dbt_scd_id",
    "dbt_updated_at",
    "dbt_valid_from",
    "dbt_valid_to",
]


class _Columns:
    def __init__(self, names):
        self._names = names

    def __iter__(self):
        return iter(SimpleNamespace(name=name) for name in self._names)


def _adapter(transactional, primary_key=None, columns_for_table=None):
    """An adapter whose only server interaction is a table we describe."""
    columns_for_table = columns_for_table if columns_for_table is not None else SNAPSHOT_COLUMNS
    adapter = MaxComputeAdapter.__new__(MaxComputeAdapter)
    adapter.get_columns_in_relation = lambda relation: _Columns(SNAPSHOT_COLUMNS)
    if transactional is _MISSING:
        table = object()
    else:
        table = SimpleNamespace(
            is_transactional=transactional,
            primary_key=primary_key or [],
            table_schema=SimpleNamespace(
                columns=[SimpleNamespace(name=name) for name in columns_for_table]
            ),
        )
    adapter.get_odps_table_by_relation = lambda relation, retry_times=1: table
    return adapter


_MISSING = object()
RELATION = MaxComputeRelation.create(
    database="project", schema="myschema", identifier="my_snapshot"
)


def test_transactional_target_is_accepted():
    _adapter(True).valid_snapshot_target(RELATION)


def test_non_transactional_target_names_the_missing_property():
    with pytest.raises(DbtRuntimeError) as error:
        _adapter(False).valid_snapshot_target(RELATION)
    message = str(error.value).lower()
    assert "non-transactional" in message
    assert "merge" in message, "the message has to say why history is impossible"
    assert 'tblproperties("transactional"="true")' in message or "let dbt create" in message
    # The point of checking in the adapter is that the user is not handed a
    # bare server error to interpret.
    assert "odps-" not in message


@pytest.mark.parametrize("table", [None, object()])
def test_target_whose_metadata_cannot_be_read_is_not_rejected(table):
    """A view, an external table, or an older pyodps: unknown is not "no"."""
    adapter = _adapter(_MISSING)
    adapter.get_odps_table_by_relation = lambda relation, retry_times=1: table
    adapter.valid_snapshot_target(RELATION)


def test_missing_snapshot_columns_are_still_dbt_s_own_error():
    adapter = _adapter(True)
    adapter.get_columns_in_relation = lambda relation: _Columns(["id", "name"])
    with pytest.raises(Exception) as error:
        adapter.valid_snapshot_target(RELATION)
    assert "dbt_scd_id" in str(error.value)


def test_primary_key_target_is_rejected_because_history_cannot_coexist():
    """A PK Delta table upserts by key; the record loses its current version."""
    with pytest.raises(DbtRuntimeError) as error:
        _adapter(True, primary_key=["id"]).valid_snapshot_target(RELATION)
    message = str(error.value)
    assert "primary key" in message.lower()
    assert "id" in message, "the message has to name the key that collides"
    assert "no current version" in message.lower()


def test_append_delta_target_without_a_key_is_accepted():
    _adapter(True, primary_key=[]).valid_snapshot_target(RELATION)


def test_key_on_the_version_id_column_is_accepted():
    """`dbt_scd_id` is already unique per version, so a key there is not a
    collision: refusing it would reject a target that can hold history."""
    adapter = _adapter(True, primary_key=["dbt_scd_id"])
    adapter.valid_snapshot_target(RELATION)


def test_case_differences_in_the_key_name_do_not_change_the_verdict():
    adapter = _adapter(True, primary_key=["ID"])
    with pytest.raises(DbtRuntimeError):
        adapter.valid_snapshot_target(RELATION)


def test_renamed_version_id_column_is_respected():
    column_names = {
        "dbt_scd_id": "my_scd_id",
        "dbt_valid_from": "dbt_valid_from",
        "dbt_valid_to": "dbt_valid_to",
    }
    adapter = _adapter(True, primary_key=["my_scd_id"])
    adapter.get_columns_in_relation = lambda relation: _Columns(
        [
            "id",
            "name",
            "amount",
            "updated_at",
            "my_scd_id",
            "dbt_updated_at",
            "dbt_valid_from",
            "dbt_valid_to",
        ]
    )
    adapter.valid_snapshot_target(RELATION, column_names)


def test_leftover_staging_columns_are_refused_with_the_remedy():
    """A table an older adapter polluted can never be snapshotted again."""
    adapter = _adapter(
        True, columns_for_table=SNAPSHOT_COLUMNS + ["dbt_unique_key_1", "dbt_unique_key_2"]
    )
    with pytest.raises(DbtRuntimeError) as error:
        adapter.valid_snapshot_target(RELATION)
    message = str(error.value).lower()
    assert "dbt_unique_key_1" in message, "name the columns the user has to remove"
    assert "drop columns" in message or "new" in message
    assert "ambiguous" in message, "say what would otherwise keep happening"
