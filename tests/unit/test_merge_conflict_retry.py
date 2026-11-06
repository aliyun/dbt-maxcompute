from unittest.mock import Mock, call

import pytest
from odps.errors import ODPSError, WaitTimeoutError, parse_instance_error
from odps.models import Instance

from dbt.adapters.maxcompute import wrapper


MERGE_SQL = """-- model query comment
SET odps.sql.type.system.odps2=true;
MERGE /* optimizer comment */ INTO accounts AS t USING accounts_stage AS s
ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.name = concat(s.name, ';');"""
ABORT = "ODPS-0429311:Transaction aborted - BeginTs is less than lastCommitTs."
WRAPPED_ABORT = "103:ODPS-0110061: Failed to run ddltask - " + ABORT


def conflict_error(message=WRAPPED_ABORT, instance_id="aborted"):
    error = parse_instance_error(message)
    error.instance_id = instance_id
    return error


@pytest.fixture
def execution(monkeypatch):
    odps = Mock()
    cursor = wrapper.ConnectionWrapper(
        odps=odps, hints={"odps.sql.submit.mode": "script"}
    ).cursor()
    sleep = Mock()
    jitter = Mock(side_effect=lambda lower, upper: upper)
    monkeypatch.setattr(wrapper, "time", Mock(sleep=sleep))
    monkeypatch.setattr(wrapper, "random", Mock(uniform=jitter))
    instance = odps.get_instance.return_value
    instance.is_terminated.return_value = True
    instance.get_task_statuses.return_value = {"sql": Mock(status=Instance.Task.TaskStatus.FAILED)}
    return cursor, odps, sleep, jitter


@pytest.mark.parametrize(
    "message", [ABORT, WRAPPED_ABORT, WRAPPED_ABORT.replace(":Transaction", ": Transaction")]
)
def test_retries_only_the_same_merge_after_confirmed_conflict(execution, message):
    cursor, odps, sleep, jitter = execution
    recovered = Mock(id="recovered")
    odps.execute_sql.side_effect = [conflict_error(message), recovered]

    cursor.execute(MERGE_SQL)

    odps.get_instance.assert_called_once_with("aborted")
    assert odps.execute_sql.call_count == 2
    assert odps.execute_sql.call_args_list[0] == odps.execute_sql.call_args_list[1]
    assert cursor.instance is recovered
    jitter.assert_called_once_with(15, 30)
    sleep.assert_called_once_with(30)


def test_backoff_caps_the_delay_and_shares_the_submission_limit(execution):
    cursor, odps, sleep, jitter = execution
    generic_error = ODPSError("ODPS-0110061: unrelated failure", code="ODPS-0110061")
    conflicts = [conflict_error(instance_id=f"aborted-{i}") for i in range(9)]
    odps.execute_sql.side_effect = [generic_error, *conflicts]

    with pytest.raises(ODPSError) as caught:
        cursor.execute(MERGE_SQL)

    assert caught.value is conflicts[-1]
    assert odps.execute_sql.call_count == 10
    assert (
        jitter.call_args_list == [call(15, 30), call(30, 60), call(60, 120)] + [call(120, 240)] * 5
    )
    assert sleep.call_args_list == [call(delay) for delay in [15, 30, 60, 120, *([240] * 5)]]


@pytest.mark.parametrize("status", ["missing_id", "running", "succeeded", "lookup_failed"])
def test_does_not_replay_an_unconfirmed_abort(execution, status):
    cursor, odps, sleep, jitter = execution
    error = conflict_error()
    instance = odps.get_instance.return_value
    if status == "missing_id":
        error.instance_id = None
    elif status == "running":
        instance.is_terminated.return_value = False
    elif status == "succeeded":
        instance.get_task_statuses.return_value["sql"].status = Instance.Task.TaskStatus.SUCCESS
    else:
        instance.is_terminated.side_effect = ODPSError("Cannot fetch instance")
    odps.execute_sql.side_effect = error

    with pytest.raises(ODPSError) as caught:
        cursor.execute(MERGE_SQL)

    assert caught.value is error
    assert odps.execute_sql.call_count == 1
    sleep.assert_not_called()
    jitter.assert_not_called()


@pytest.mark.parametrize(
    "sql,error",
    [
        (MERGE_SQL + "\nINSERT INTO audit VALUES (1);", conflict_error()),
        ("SELECT 1;\n" + MERGE_SQL, conflict_error()),
        (MERGE_SQL, WaitTimeoutError("Still waiting", instance_id="running")),
        ("UPDATE accounts SET name = 'a' WHERE id = 1;", conflict_error(ABORT)),
    ],
)
def test_does_not_replay_other_sql_or_timeouts(execution, sql, error):
    cursor, odps, sleep, jitter = execution
    odps.execute_sql.side_effect = error

    with pytest.raises(ODPSError) as caught:
        cursor.execute(sql)

    assert caught.value is error
    assert odps.execute_sql.call_count == 1
    odps.get_instance.assert_not_called()
    sleep.assert_not_called()
    jitter.assert_not_called()
