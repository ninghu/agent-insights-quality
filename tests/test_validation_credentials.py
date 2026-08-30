from __future__ import annotations

import base64
import json
import sys
import types
from types import SimpleNamespace

import pytest

from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_credentials import local_azure_operator


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
    identity.AzureCliCredential = lambda: credential
    monkeypatch.setitem(sys.modules, "azure.identity", identity)

    operator = local_azure_operator()
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
