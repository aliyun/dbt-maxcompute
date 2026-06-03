import copy
import time
from dataclasses import dataclass
from typing import Optional

from dbt.adapters.events.logging import AdapterLogger
from odps.dbapi import Cursor, Connection
from odps.errors import ODPSError

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


class CursorWrapper(Cursor):
    def __init__(self, connection, *args, maxqa_config=None, **kwargs):
        super().__init__(connection, *args, **kwargs)
        self._maxqa_config = maxqa_config

    def execute(self, operation, parameters=None, **kwargs):
        result = SettingParser.parse(operation)
        effective_maxqa = self._resolve_maxqa(result.settings)
        retry_times = 10
        for i in range(retry_times):
            try:
                if effective_maxqa:
                    self._execute_maxqa(result, effective_maxqa)
                else:
                    super().execute(result.remaining_query, hints=result.settings)
                    self._instance.wait_for_success()
                return
            except ODPSError as e:
                if (
                    e.code == "ODPS-0130201"
                    or e.code == "ODPS-0130211"
                    or e.code == "ODPS-0110061"
                    or e.code == "ODPS-0130131"
                    or e.code == "ODPS-0420111"
                ):
                    if i == retry_times - 1:
                        raise e
                    logger.warning(f"Retry because of {e}, retry times {i + 1}")
                    time.sleep(15)
                    continue
                else:
                    o = self.connection.odps
                    if e.instance_id:
                        instance = o.get_instance(e.instance_id)
                        logger.error(instance.get_logview_address())
                    raise e

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
