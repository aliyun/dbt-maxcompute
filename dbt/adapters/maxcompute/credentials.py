from dataclasses import dataclass
from typing import Optional

from alibabacloud_credentials.client import Client
from alibabacloud_credentials.models import Config
from dbt.adapters.contracts.connection import Credentials
from odps import ODPS
from odps.accounts import CredentialProviderAccount


@dataclass
class MaxComputeCredentials(Credentials):
    endpoint: str

    # auth config: All configuration items supported by alibabacloud_credentials
    # It should be noted that in order to avoid ambiguity,
    # `type` becomes `auth_type`, `policy` becomes `auth_policy`, `host` becomes `auth_host`,
    # `timeout` becomes `auth_timeout`, `connect_timeout` becomes `auth_connect_timeout`, `proxy` becomes `auth_proxy`
    auth_type: str = "access_key"
    access_key_id: Optional[str] = None
    access_key_secret: Optional[str] = None
    security_token: Optional[str] = None
    bearer_token: Optional[str] = None
    duration_seconds: Optional[int] = None
    role_arn: Optional[str] = None
    oidc_provider_arn: Optional[str] = None
    oidc_token_file_path: Optional[str] = None
    auth_policy: Optional[str] = None
    role_session_expiration: Optional[int] = None
    role_session_name: Optional[str] = None
    public_key_id: Optional[str] = None
    private_key_file: Optional[str] = None
    role_name: Optional[str] = None
    auth_host: Optional[str] = None
    auth_timeout: Optional[int] = 1000
    auth_connect_timeout: Optional[int] = 1000
    auth_proxy: Optional[str] = None
    credentials_uri: Optional[str] = None
    disable_imds_v1: Optional[bool] = False
    enable_imds_v2: Optional[bool] = False
    metadata_token_duration: Optional[int] = 21600
    sts_endpoint: Optional[str] = None

    _ALIASES = {
        "project": "database",
        "ak": "access_key_id",
        "sk": "access_key_secret",
        "sts": "security_token",
        "accessId": "access_key_id",
        "accessKey": "access_key_secret",
    }

    @property
    def type(self):
        return "maxcompute"

    @property
    def unique_field(self):
        return self.endpoint + "_" + self.database

    def _connection_keys(self):
        return "project", "database", "schema", "endpoint"

    def odps(self):
        if self.auth_type == "chain":
            cred = Client()
        else:
            config = Config(
                type=self.auth_type,
                access_key_id=self.access_key_id,
                access_key_secret=self.access_key_secret,
                security_token=self.security_token,
                bearer_token=self.bearer_token,
                duration_seconds=self.duration_seconds,
                role_arn=self.role_arn,
                oidc_provider_arn=self.oidc_provider_arn,
                oidc_token_file_path=self.oidc_token_file_path,
                policy=self.auth_policy,
                role_session_expiration=self.role_session_expiration,
                role_session_name=self.role_session_name,
                public_key_id=self.public_key_id,
                private_key_file=self.private_key_file,
                role_name=self.role_name,
                host=self.auth_host,
                timeout=self.auth_timeout,
                connect_timeout=self.auth_connect_timeout,
                proxy=self.auth_proxy,
                credentials_uri=self.credentials_uri,
                disable_imds_v1=self.disable_imds_v1,
                enable_imds_v2=self.enable_imds_v2,
                metadata_token_duration=self.metadata_token_duration,
                sts_endpoint=self.sts_endpoint,
            )
            cred = Client(config)
        account = CredentialProviderAccount(cred)
        o = ODPS(
            account=account,
            project=self.database,
            endpoint=self.endpoint,
        )
        o.schema = self.schema
        return o
