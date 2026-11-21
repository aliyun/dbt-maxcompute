import copy
import random
import re
import time
from dataclasses import dataclass
from typing import Optional

from dbt.adapters.events.logging import AdapterLogger
from odps.dbapi import Cursor, Connection
from odps.errors import ODPSError
from odps.models import Instance
from odps.utils import split_sql_by_semicolon

from dbt.adapters.maxcompute.setting_parser import SettingParser


@dataclass
class MaxQAConfig:
    quota_name: Optional[str] = None
    fallback: bool = True
    offline_quota_name: Optional[str] = None


class ConnectionWrapper(Connection):
    def __init__(self, odps=None, hints=None, maxqa_config=None, **kwargs):
        super().__init__(odps=odps, hints=hints, **kwargs)
        self._maxqa_config = maxqa_config

    def cursor(self, *args, **kwargs):
        return CursorWrapper(
            self,
            *args,
            hints=copy.deepcopy(self._hints),
            maxqa_config=self._maxqa_config,
            **kwargs,
        )

    def cancel(self):
        self.close()


logger = AdapterLogger("MaxCompute")

_MERGE_INTO = re.compile(r"merge(?:\s|/\*.*?\*/|--[^\n]*\n)+into\b", re.IGNORECASE | re.DOTALL)
_WRAPPED_TRANSACTION_ABORT = re.compile(r"\bODPS-0429311:\s*Transaction aborted\b")


def _is_commit_conflict(error: ODPSError) -> bool:
    message = str(error)
    return "BeginTs is less than lastCommitTs" in message and (
        error.code == "ODPS-0429311"
        or (
            error.code == "ODPS-0110061" and _WRAPPED_TRANSACTION_ABORT.search(message) is not None
        )
    )


class CursorWrapper(Cursor):
    def __init__(self, connection, *args, maxqa_config=None, **kwargs):
        super().__init__(connection, *args, **kwargs)
        self._maxqa_config = maxqa_config

    def execute(self, operation, parameters=None, **kwargs):
        result = SettingParser.parse(operation)
        effective_maxqa = self._resolve_maxqa(result.settings)
        max_submissions = 10
        merge_retries = 0
        for submission in range(1, max_submissions + 1):
            try:
                if effective_maxqa:
                    self._execute_maxqa(result, effective_maxqa)
                else:
                    super().execute(result.remaining_query, hints=result.settings)
                    self._instance.wait_for_success()
                if merge_retries:
                    logger.info(
                        f"MERGE INTO succeeded after {merge_retries} conflict retries, "
                        f"instance {self._instance.id}"
                    )
                return
            except ODPSError as e:
                if not effective_maxqa and _is_commit_conflict(e):
                    try:
                        statements = split_sql_by_semicolon(result.remaining_query)
                    except (AssertionError, IndexError):
                        statements = []
                    if not statements:
                        raise
                    if any(_MERGE_INTO.match(sql) for sql in statements):
                        if (
                            len(statements) != 1
                            or submission == max_submissions
                            or not self._confirmed_failed_instance(e)
                        ):
                            logger.warning(f"MERGE INTO commit conflict, instance {e.instance_id}")
                            raise
                        delay_cap = min(30 * 2**merge_retries, 240)
                        delay = random.uniform(delay_cap / 2, delay_cap)
                        merge_retries += 1
                        logger.warning(
                            f"MERGE INTO commit conflict, instance {e.instance_id}, "
                            f"retrying in {delay:.1f}s after submission {submission}/{max_submissions}"
                        )
                        time.sleep(delay)
                        continue
                if (
                    e.code == "ODPS-0130201"
                    or e.code == "ODPS-0130211"
                    or e.code == "ODPS-0110061"
                    or e.code == "ODPS-0130131"
                    or e.code == "ODPS-0420111"
                ):
                    if submission == max_submissions:
                        raise
                    logger.warning(f"Retry because of {e}, retry times {submission}")
                    time.sleep(15)
                    continue
                else:
                    if e.instance_id:
                        # LogView URLs contain temporary access tokens. Keep
                        # the stable instance identifier in logs without
                        # serializing the signed URL.
                        logger.error(
                            f"MaxCompute instance failed: {e.instance_id}; "
                            "signed LogView URL omitted"
                        )
                    raise e

    def _confirmed_failed_instance(self, error: ODPSError) -> bool:
        if not error.instance_id:
            return False
        try:
            instance = self.connection.odps.get_instance(error.instance_id)
            return instance.is_terminated(retry=False) and any(
                task.status == Instance.Task.TaskStatus.FAILED
                for task in instance.get_task_statuses().values()
            )
        except (ODPSError, OSError):
            logger.warning(f"Could not confirm failure of instance {error.instance_id}")
            return False

    def _resolve_maxqa(self, settings):
        model_mode = settings.pop("dbt.execution_mode", None)
        model_quota = settings.pop("dbt.quota_name", None)
        if model_mode == "offline":
            return None
        if model_mode == "maxqa":
            if self._maxqa_config:
                if model_quota:
                    return MaxQAConfig(
                        quota_name=model_quota,
                        fallback=self._maxqa_config.fallback,
                        offline_quota_name=self._maxqa_config.offline_quota_name,
                    )
                return self._maxqa_config
            return MaxQAConfig(quota_name=model_quota)
        return self._maxqa_config

    def _execute_maxqa(self, result, config):
        o = self.connection.odps
        self._instance = o.execute_sql_interactive(
            result.remaining_query,
            use_mcqa_v2=True,
            quota_name=config.quota_name,
            hints=result.settings,
            fallback=config.fallback,
            offline_quota_name=config.offline_quota_name,
        )
