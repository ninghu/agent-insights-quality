from __future__ import annotations

import base64
import json
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from azure.identity import CredentialUnavailableError

from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_credentials import (
    VerifiedAzureCliCredential,
    local_azure_operator,
)


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    return f"header.{payload.rstrip('=')}.signature"


def test_local_operator_requires_user_and_rechecks_every_token(monkeypatch) -> None:
    def run(arguments, **_kwargs):
        value = (
            {
                "id": "synthetic-subscription-id",
                "tenantId": "synthetic-tenant-id",
                "user": {"type": "user", "name": "synthetic-user"},
            }
            if "account" in arguments
            else {"id": "synthetic-object-id"}
        )
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))

    monkeypatch.setattr(
        "agent_insights_quality.validation_credentials.subprocess.run",
        run,
    )
    tokens = iter(
        [
            _jwt(
                {
                    "tid": "synthetic-tenant-id",
                    "oid": "synthetic-object-id",
                }
            ),
            _jwt(
                {
                    "tid": "synthetic-tenant-id",
                    "oid": "synthetic-object-id",
                }
            ),
            _jwt(
                {
                    "tid": "synthetic-tenant-id",
                    "oid": "rotated-object-id",
                }
            ),
        ]
    )
    credential = SimpleNamespace(
        get_token=lambda _scope: SimpleNamespace(token=next(tokens))
    )
    identity = types.ModuleType("azure.identity")
    constructor = {}

    def azure_cli_credential(**kwargs):
        constructor.update(kwargs)
        return credential

    identity.AzureCliCredential = azure_cli_credential
    monkeypatch.setitem(sys.modules, "azure.identity", identity)

    operator = local_azure_operator()
    assert constructor == {"process_timeout": 60}
    assert operator.object_id == "synthetic-object-id"
    assert operator.subscription_id == "synthetic-subscription-id"
    assert operator.tenant_id == "synthetic-tenant-id"
    assert operator.token_provider("scope")
    with pytest.raises(ContractError, match="identity changed"):
        operator.token_provider("scope")


def test_local_operator_rejects_service_principal(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_credentials.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "id": "synthetic-subscription-id",
                    "tenantId": "synthetic-tenant-id",
                    "user": {
                        "type": "servicePrincipal",
                        "name": "synthetic-client-id",
                    },
                }
            ),
        ),
    )
    with pytest.raises(ContractError, match="authenticated Azure CLI user"):
        local_azure_operator()


def test_verified_credential_serializes_and_caches_each_scope() -> None:
    calls = 0
    calls_lock = threading.Lock()
    token = SimpleNamespace(
        token=_jwt(
            {
                "tid": "synthetic-tenant-id",
                "oid": "synthetic-object-id",
            }
        ),
        expires_on=int(time.time()) + 3600,
    )

    def get_token(*_scopes, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        return token

    credential = VerifiedAzureCliCredential(
        SimpleNamespace(get_token=get_token),
        tenant_id="synthetic-tenant-id",
        object_id="synthetic-object-id",
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: credential.get_token("scope"), range(16)))
    assert values == [token] * 16
    assert calls == 1


def test_verified_credential_converts_cli_failure_to_contract_error() -> None:
    credential = VerifiedAzureCliCredential(
        SimpleNamespace(
            get_token=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                CredentialUnavailableError("synthetic timeout")
            )
        ),
        tenant_id="synthetic-tenant-id",
        object_id="synthetic-object-id",
    )
    with pytest.raises(ContractError, match="token acquisition failed"):
        credential.get_token("scope")
