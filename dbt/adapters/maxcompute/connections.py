from contextlib import contextmanager

from dbt.adapters.contracts.connection import AdapterResponse
from dbt.adapters.events.logging import AdapterLogger
from dbt.adapters.sql import SQLConnectionManager
from dbt_common.exceptions import DbtConfigError, DbtRuntimeError
from odps import options

from dbt.adapters.maxcompute.context import GLOBAL_SQL_HINTS
from dbt.adapters.maxcompute.wrapper import ConnectionWrapper

logger = AdapterLogger("MaxCompute")


class MaxComputeConnectionManager(SQLConnectionManager):
    TYPE = "maxcompute"

    @classmethod
    def open(cls, connection):
        if connection.state == "open":
            logger.debug("Connection is already open, skipping open.")
            return connection

        credentials = connection.credentials
        o = credentials.odps()
        options.user_agent_pattern = "dbt-maxcompute $pyodps_version $python_version"

        try:
            o.get_project().reload()
        except Exception as e:
            raise DbtConfigError(f"Failed to connect to MaxCompute: {str(e)}") from e

        handle = ConnectionWrapper(odps=o, hints=GLOBAL_SQL_HINTS)
        connection.state = "open"
        connection.handle = handle
        return connection

    @classmethod
    def get_response(cls, cursor):
        # FIXME：we should get 'code', 'message', 'rows_affected' from cursor
        logger.debug("Current instance id is " + cursor._instance.id)
        return AdapterResponse(_message="OK")

    @contextmanager
    def exception_handler(self, sql: str):
        try:
            yield
        except Exception as exc:
            logger.debug("Error while running:\n{}".format(sql))
            logger.debug(exc)
            if len(exc.args) == 0:
                raise
            thrift_resp = exc.args[0]
            if hasattr(thrift_resp, "status"):
                msg = thrift_resp.status.errorMessage
                raise DbtRuntimeError(msg)
            else:
                raise DbtRuntimeError(str(exc))

    def cancel(self, connection):
        connection.handle.cancel()

    def begin(self):
        logger.debug("Trigger beginning transaction, actually do nothing...")

    # FIXME: Sometimes the number of commits is greater than the number of begins.
    #  It should be a problem with the micro, which can be reproduced through the test of dbt_show.
    def commit(self):
        logger.debug("Committing transaction, actually do nothing...")

    def add_begin_query(self):
        pass

    def add_commit_query(self):
        pass
