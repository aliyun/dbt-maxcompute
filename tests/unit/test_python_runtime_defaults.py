from unittest.mock import MagicMock

import pytest

from dbt.adapters.maxcompute.context import GLOBAL_SQL_HINTS
from dbt.adapters.maxcompute.wrapper import ConnectionWrapper


@pytest.mark.parametrize("runtime", [None, "cp27", "cp37", "cp311"])
def test_preview_python_runtime_default_and_explicit_legacy_overrides(runtime):
    client = MagicMock()
    connection = ConnectionWrapper(odps=client, hints=GLOBAL_SQL_HINTS)
    sql = "select 1"
    if runtime:
        sql = f"set odps.sql.python.version={runtime}; {sql}"
    connection.cursor().execute(sql)
    assert client.execute_sql.call_args.kwargs["hints"]["odps.sql.python.version"] == (
        runtime or "cp311"
    )
    assert GLOBAL_SQL_HINTS["odps.sql.python.version"] == "cp311"
