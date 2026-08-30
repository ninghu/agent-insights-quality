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
                    "user": {
                        "type": "servicePrincipal",
                        "name": "synthetic-client-id",
                    }
                }
            ),
        ),
    )
    verify_azure_service_principal("synthetic-client-id")
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
                    "user": {
                        "type": "servicePrincipal",
                        "name": "synthetic-client-id",
                    }
                }
            ),
        ),
    )
    credential = SimpleNamespace(
        get_token=lambda _scope: SimpleNamespace(
            token=_jwt({"appid": "synthetic-client-id"})
        )
    )
    identity = types.ModuleType("azure.identity")
    identity.AzureCliCredential = lambda: credential
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    assert validation_blob_credential("synthetic-client-id") is credential

    credential.get_token = lambda _scope: SimpleNamespace(
        token=_jwt({"azp": "different-client-id"})
    )
    with pytest.raises(ContractError, match="Blob token principal"):
        validation_blob_credential("synthetic-client-id")
