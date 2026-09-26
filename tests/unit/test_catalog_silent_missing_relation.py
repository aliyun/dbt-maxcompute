"""Regression tests for the silent catalog node drop when relation metadata is unreadable.

dbt docs generate builds catalog.json by listing relations and then reading each
relation's metadata. Historically, a relation that was listed but kept raising
NoSuchObject was retried 10x10s and then skipped with only a per-relation warning,
so catalog.json silently lost the node while the command still succeeded.

Current contract (this file pins it):
- default: the catalog build still succeeds (unchanged exit semantics), but a single
  summary warning names every skipped relation and points at the opt-in switch;
- `catalog_strict_metadata: true` in the profile: the build raises, dbt-core collects
  the exception into catalog.json `errors`, and docs generation fails visibly;
- NoSuchObject raised by get_table itself (not only reload) is retried like reload;
- a non-NoSuchObject wrapped ODPSError still propagates immediately (never silent).

Offline and deterministic: the ODPS client is injected, so nothing depends on the
sporadic server-side "visible in list / unreadable via get" window.
"""

import logging
from types import SimpleNamespace

import agate
import pytest
from dbt_common.exceptions import DbtRuntimeError
from odps.errors import NoSuchObject, ODPSError

from dbt.adapters.maxcompute import impl as mc_impl
from dbt.adapters.maxcompute.impl import MaxComputeAdapter
from dbt.adapters.maxcompute.relation import MaxComputeRelation

PROJECT = "proj"
SCHEMA = "sch"


class FakeColumn:
    def __init__(self, name):
        self.name = name
        self.type = type("T", (), {"name": "string"})()
        self.comment = None


class FakeTable:
    def __init__(self, name, fail_reload=False):
        self.name = name
        self._fail_reload = fail_reload
        self.is_virtual_view = False
        self.is_materialized_view = False
        self.comment = None
        self.owner = "owner"
        self.table_schema = type("TS", (), {"simple_columns": [FakeColumn("c1")]})()

    def reload(self):
        if self._fail_reload:
            raise NoSuchObject(f"Table {SCHEMA}.{self.name} does not exist")


class FakeODPS:
    def __init__(self, tables, raise_on_get=()):
        self._tables = tables
        self._raise_on_get = set(raise_on_get)

    def get_table(self, name, project=None, schema=None):
        if name in self._raise_on_get:
            raise NoSuchObject(f"Table {SCHEMA}.{name} does not exist")
        return self._tables[name]


def _relation(name):
    return MaxComputeRelation.create(database=PROJECT, schema=SCHEMA, identifier=name)


def _build_adapter(monkeypatch, client, sleep_records, strict=None):
    adapter = MaxComputeAdapter.__new__(MaxComputeAdapter)
    adapter.get_odps_client = lambda: client
    monkeypatch.setattr(mc_impl.time, "sleep", lambda seconds: sleep_records.append(seconds))
    if strict is not None:
        adapter.config = SimpleNamespace(credentials=SimpleNamespace(catalog_strict_metadata=strict))
    return adapter


def _catalog(adapter):
    relations = [_relation("readable"), _relation("gone")]
    used_schemas = frozenset({(PROJECT, SCHEMA)})
    return adapter._get_one_catalog_by_relations(None, relations, used_schemas)


def _names(catalog_table):
    if not isinstance(catalog_table, agate.Table):
        return set()
    return {row["table_name"] for row in catalog_table.rows}


def _tables():
    return {
        "readable": FakeTable("readable"),
        "gone": FakeTable("gone", fail_reload=True),
    }


def test_catalog_exhausted_get_skips_with_actionable_warning(monkeypatch, caplog):
    """Default: exit semantics unchanged, but the gap is named and actionable."""
    sleep_records = []
    adapter = _build_adapter(monkeypatch, FakeODPS(_tables()), sleep_records, strict=False)

    with caplog.at_level(logging.INFO, logger="dbt.adapters.maxcompute.impl"):
        catalog = _catalog(adapter)

    assert _names(catalog) == {"readable"}, "the unreadable relation is not in the catalog"
    assert len(sleep_records) == 10 and all(s == 10 for s in sleep_records)
    retry_lines = [r for r in caplog.records if "does not exist, retrying" in r.getMessage()]
    assert len(retry_lines) == 10
    summary = [r for r in caplog.records if "Catalog skipped" in r.getMessage()]
    assert len(summary) == 1, "exactly one actionable summary warning"
    message = summary[0].getMessage()
    assert "`proj`.`sch`.`gone`" in message
    assert "missing from catalog.json" in message
    assert "catalog_strict_metadata: true" in message


def test_catalog_strict_metadata_raises(monkeypatch):
    """Opt-in strict: the build fails and dbt-core surfaces the error in catalog.json."""
    sleep_records = []
    adapter = _build_adapter(monkeypatch, FakeODPS(_tables()), sleep_records, strict=True)

    with pytest.raises(DbtRuntimeError) as excinfo:
        _catalog(adapter)
    message = str(excinfo.value)
    assert "catalog_strict_metadata is enabled" in message
    assert "`proj`.`sch`.`gone`" in message


def test_no_such_object_from_get_table_is_retried(monkeypatch):
    """get_table itself raising NoSuchObject is treated like reload: retried, then None."""
    sleep_records = []
    client = FakeODPS({"readable": FakeTable("readable")}, raise_on_get=("gone",))
    adapter = _build_adapter(monkeypatch, client, sleep_records, strict=False)
    relation = _relation("gone")
    assert adapter.get_odps_table_by_relation(relation, 3) is None
    assert len(sleep_records) == 3


def test_wrapped_internal_error_is_not_silent(monkeypatch):
    """A non-NoSuchObject wrapped ODPSError (e.g. server wrapping NoSuchObjectException
    as InvalidParameter / ODPS-0010000) propagates immediately instead of being
    swallowed by the retry loop."""
    sleep_records = []

    class BoomODPS(FakeODPS):
        def get_table(self, name, project=None, schema=None):
            if name == "gone":
                raise ODPSError("ODPS-0110061 InvalidParameter: "
                                "NoSuchObjectException cannot be cast to java.lang.RuntimeException")
            return self._tables[name]

    tables = {"readable": FakeTable("readable"), "gone": None}
    adapter = _build_adapter(monkeypatch, BoomODPS(tables), sleep_records)

    with pytest.raises(ODPSError):
        _catalog(adapter)
    assert sleep_records == []
