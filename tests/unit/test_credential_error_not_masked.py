"""Dynamic-credential failures must reach the caller as credential failures.

PyODPS ``CredentialProviderAccount._refresh_credential``:

    try:
        credential = self.provider.get_credential()
    except Exception:
        credential = self.provider.get_credentials()   # no arguments

The second call exists for providers that only expose the legacy
``get_credentials()`` API.  With alibabacloud-credentials >= 1.0 that name is a
*static* method taking a ``config``, so the compatibility call raises
``TypeError: ... missing 1 required positional argument: 'config'`` and, because
the ``except Exception`` block does not keep the original error, the caller
loses the real credential problem (no credentials configured, metadata service
unreachable, expired STS token ...).

These tests run offline - no MaxCompute service, no network.  They assert on
what the caller receives, so they stay valid whichever side ends up owning the
fix (PyODPS itself, or a wrapper shipped by dbt-maxcompute).
"""

import inspect
import logging
import os

import pytest

# The class under test.  The same file is delivered against both hosts; only
# this default differs (MC_CRED_ACCOUNT_HOST overrides it for cross-checks).
DEFAULT_ACCOUNT_HOST = os.environ.get("MC_CRED_ACCOUNT_HOST", "dbt")

REAL_CAUSE = "unable to load credentials from any of the providers in the chain"

# Signature-mismatch noise produced by the compatibility call.  Anything that
# still mentions it means the credential problem has been masked again.
MASKING_MARKERS = (
    "missing 1 required positional argument",
    "get_credentials()",
    "unexpected keyword argument",
)


def resolve_account_class(host=None):
    host = host or DEFAULT_ACCOUNT_HOST
    if host == "pyodps":
        from odps.accounts import CredentialProviderAccount

        return CredentialProviderAccount
    if host == "dbt":
        from dbt.adapters.maxcompute.credentials import (
            _CopySafeCredentialProviderAccount,
        )

        return _CopySafeCredentialProviderAccount
    raise ValueError("unknown MC_CRED_ACCOUNT_HOST: %r" % (host,))


class CredentialProbeError(RuntimeError):
    """Stands in for the library's own credential failure (CredentialException)."""


class _Credentials:
    def __init__(self, access_id="ak-from-new-api", secret="sk", token=None):
        self._access_id = access_id
        self._secret = secret
        self._token = token

    def get_access_key_id(self):
        return self._access_id

    def get_access_key_secret(self):
        return self._secret

    def get_security_token(self):
        return self._token


class _NewApiOnlyProvider:
    """Shape of alibabacloud-credentials 1.0.12 ``Client``.

    ``get_credential()`` is the working instance method; ``get_credentials``
    is a static method that requires a ``config`` - so calling it with no
    arguments is a signature error, not a credential error.
    """

    def get_credential(self):
        raise CredentialProbeError(REAL_CAUSE)

    @staticmethod
    def get_credentials(config):  # pragma: no cover - must never be called bare
        raise AssertionError("get_credentials(config) must not be called here")


class _LegacyApiOnlyProvider:
    """Shape of alibabacloud-credentials < 1.0: only ``get_credentials()``."""

    def get_credentials(self):
        return _Credentials(access_id="ak-from-legacy-api")


class _BothApisProvider:
    """Legacy ``get_credentials()`` still usable after a failed ``get_credential``."""

    def get_credential(self):
        raise CredentialProbeError(REAL_CAUSE)

    def get_credentials(self):
        return _Credentials(access_id="ak-from-legacy-api")


@pytest.fixture
def account_class():
    return resolve_account_class()


def _refresh(account):
    """Call the private refresh entry that ``sign_request`` uses."""
    return account._refresh_credential()


def test_new_api_failure_is_not_replaced_by_signature_error(account_class):
    account = account_class(_NewApiOnlyProvider())

    with pytest.raises(Exception) as excinfo:
        _refresh(account)

    message = str(excinfo.value)
    leaked = [marker for marker in MASKING_MARKERS if marker in message]
    assert not leaked, "the compatibility call masked it again: %s" % message
    assert REAL_CAUSE in message
    assert isinstance(excinfo.value, CredentialProbeError)


def test_original_error_keeps_its_type_and_message(account_class):
    account = account_class(_NewApiOnlyProvider())

    with pytest.raises(CredentialProbeError) as excinfo:
        _refresh(account)

    assert str(excinfo.value) == REAL_CAUSE


def test_legacy_provider_api_still_works(account_class):
    """Negative control: the fix must not drop support for old credential libs."""
    account = account_class(_LegacyApiOnlyProvider())

    _refresh(account)

    assert account.access_id == "ak-from-legacy-api"


def test_legacy_fallback_still_used_when_new_api_fails(account_class):
    """Negative control for the second branch: a usable legacy method is used."""
    account = account_class(_BothApisProvider())

    _refresh(account)

    assert account.access_id == "ak-from-legacy-api"


def test_signature_error_cannot_leak_for_any_provider_shape(account_class):
    """Whichever path is taken, the caller must not see a binding/signature error."""
    for provider in (_NewApiOnlyProvider(), _BothApisProvider()):
        account = account_class(provider)
        try:
            _refresh(account)
        except Exception as exc:
            message = str(exc)
            leaked = [marker for marker in MASKING_MARKERS if marker in message]
            detail = "%s leaked from %s: %s" % (
                type(exc).__name__,
                type(provider).__name__,
                message,
            )
            assert not leaked, detail


def test_real_credentials_client_reports_credential_problem(account_class):
    """Same assertion against the real library object, when it truly fails.

    Skipped when the machine can resolve credentials or when the installed
    alibabacloud-credentials still has the legacy method shape: then there is
    no signature mismatch to unmask.  Offline either way - the default chain
    only touches the local metadata address with a short timeout.
    """
    try:
        from alibabacloud_credentials.client import Client
        from alibabacloud_credentials.exceptions import CredentialException
    except Exception:  # pragma: no cover - library layout differs
        pytest.skip("alibabacloud-credentials not importable")

    # The chain logs every attempt with a full traceback: one unconfigured
    # preflight measured 3060 lines / 275 KB in our own integration entry work.
    logging.disable(logging.ERROR)
    try:
        client = Client()
        if "self" in inspect.signature(type(client).get_credentials).parameters:
            pytest.skip("legacy get_credentials() shape: no signature mismatch")
        try:
            client.get_credential()
        except CredentialException:
            pass
        except Exception as exc:  # pragma: no cover - unexpected library shape
            pytest.skip("real client failed differently: %s" % exc)
        else:
            pytest.skip("credentials resolve here: nothing to unmask")

        account = account_class(client)
        with pytest.raises(Exception) as excinfo:
            _refresh(account)
    finally:
        logging.disable(logging.NOTSET)

    message = str(excinfo.value)
    leaked = [marker for marker in MASKING_MARKERS if marker in message]
    assert not leaked, message
    assert "credential" in message.lower()
