from contextlib import contextmanager
from dataclasses import dataclass

from dbt_common.exceptions import DbtConfigError, DbtRuntimeError
from dbt.adapters.contracts.connection import Credentials, AdapterResponse

from dbt.adapters.sql import SQLConnectionManager
from dbt.adapters.events.logging import AdapterLogger

from odps import ODPS, options
from odps.accounts import StsAccount

from dbt.adapters.maxcompute.context import GLOBAL_SQL_HINTS
from dbt.adapters.maxcompute.wrapper import ConnectionWrapper

logger = AdapterLogger("MaxCompute")


@dataclass
class MaxComputeCredentials(Credentials):
    endpoint: str
    accessId: str
    accessKey: str
    securityToken: str | None = None

    _ALIASES = {
        "project": "database",
        "ak": "accessId",
        "sk": "accessKey",
        "sts": "securityToken",
    }

    @property
    def type(self):
        return "maxcompute"

    @property
    def unique_field(self):
        return self.endpoint + "_" + self.database

    def _connection_keys(self):
        return ("project", "database", "schema", "endpoint")


class MaxComputeConnectionManager(SQLConnectionManager):
    TYPE = "maxcompute"

    @classmethod
    def open(cls, connection):
        if connection.state == "open":
            logger.debug("Connection is already open, skipping open.")
            return connection

        credentials = connection.credentials

        # Use STS credentials
        if credentials.securityToken is not None:
            account = StsAccount(
                credentials.accessId, credentials.accessKey, credentials.securityToken
            )
            o = ODPS(
                account=account,
                project=credentials.database,
                endpoint=credentials.endpoint,
            )
        # Use regular SK credentials
        else:
            o = ODPS(
                credentials.accessId,
                credentials.accessKey,
                project=credentials.database,
                endpoint=credentials.endpoint,
            )
        o.schema = credentials.schema
        # always use UTC timezone
        options.local_timezone = False

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
