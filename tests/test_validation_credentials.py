from __future__ import annotations

import base64
import json
import sys
import types
from types import SimpleNamespace

import pytest

from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_credentials import (
    validation_blob_credential,
    verify_azure_service_principal,
)


def test_azure_federated_identity_must_match_expected_client(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_credentials.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "tenantId": "synthetic-tenant-id",
                    "user": {
                        "type": "servicePrincipal",
                        "name": "synthetic-client-id",
                    }
                }
            ),
        ),
    )
    identity = verify_azure_service_principal("synthetic-client-id")
    assert identity.tenant_id == "synthetic-tenant-id"
    with pytest.raises(ContractError, match="does not match"):
        verify_azure_service_principal("different-client-id")


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_blob_credential_token_principal_must_match_attested_client(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_credentials.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "tenantId": "synthetic-tenant-id",
                    "user": {
                        "type": "servicePrincipal",
                        "name": "synthetic-client-id",
                    }
                }
            ),
        ),
    )
    tokens = iter(
        [
            _jwt(
                {
                    "appid": "synthetic-client-id",
                    "tid": "synthetic-tenant-id",
                    "oid": "synthetic-object-id",
                }
            ),
            _jwt(
                {
                    "appid": "synthetic-client-id",
                    "tid": "synthetic-tenant-id",
                    "oid": "synthetic-object-id",
                }
            ),
            _jwt(
                {
                    "appid": "synthetic-client-id",
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
    wrapped = validation_blob_credential(
        "synthetic-client-id",
        "synthetic-object-id",
    )
    assert wrapped.get_token("scope").token
    with pytest.raises(ContractError, match="identity rotated"):
        wrapped.get_token("scope")
