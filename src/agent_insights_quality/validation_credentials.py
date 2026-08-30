from __future__ import annotations

import base64
import binascii
import json
import subprocess
from dataclasses import dataclass
from typing import Any

from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.util import ContractError


@dataclass(frozen=True)
class AzureServicePrincipalIdentity:
    client_id: str
    tenant_id: str


class AttestedAzureCliCredential:
    def __init__(
        self,
        credential: Any,
        *,
        client_id: str,
        tenant_id: str,
        object_id: str,
    ) -> None:
        self._credential = credential
        self._client_id = client_id.casefold()
        self._tenant_id = tenant_id.casefold()
        self._object_id = object_id.casefold()

    def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        token = self._credential.get_token(*scopes, **kwargs)
        claims = _token_identity(str(token.token))
        if (
            claims["client_id"] != self._client_id
            or claims["tenant_id"] != self._tenant_id
            or claims["object_id"] != self._object_id
        ):
            raise ContractError(
                "Validation Blob token identity rotated away from the "
                "attested federated principal"
            )
        return token


def verify_azure_service_principal(
    expected_client_id: str,
) -> AzureServicePrincipalIdentity:
    expected = expected_client_id.strip().casefold()
    if not expected:
        raise ContractError("Expected Azure federated client identity is missing")
    process = subprocess.run(
        [azure_cli(), "account", "show", "--output", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if process.returncode != 0:
        raise ContractError("Azure federated identity could not be queried")
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise ContractError("Azure federated identity response is invalid") from error
    user = value.get("user") if isinstance(value, dict) else None
    client_id = (
        str(user.get("name") or "").strip().casefold()
        if isinstance(user, dict)
        else ""
    )
    identity_type = (
        str(user.get("type") or "").strip().casefold()
        if isinstance(user, dict)
        else ""
    )
    tenant_id = str(value.get("tenantId") or "").strip().casefold()
    if (
        identity_type != "serviceprincipal"
        or client_id != expected
        or not tenant_id
    ):
        raise ContractError(
            "Selected Azure credential does not match the attested federated principal"
        )
    return AzureServicePrincipalIdentity(
        client_id=client_id,
        tenant_id=tenant_id,
    )


def validation_blob_credential(
    expected_client_id: str,
    expected_object_id: str,
) -> Any:
    identity = verify_azure_service_principal(expected_client_id)
    object_id = expected_object_id.strip().casefold()
    if not object_id:
        raise ContractError("Expected Azure federated object identity is missing")
    try:
        from azure.identity import AzureCliCredential
    except ImportError as error:
        raise ContractError(
            "Validation Blob operations require the azure optional dependencies"
        ) from error
    credential = AzureCliCredential()
    token = credential.get_token("https://storage.azure.com/.default")
    claims = _token_identity(str(token.token))
    if (
        claims["client_id"] != identity.client_id
        or claims["tenant_id"] != identity.tenant_id
        or claims["object_id"] != object_id
    ):
        raise ContractError(
            "Validation Blob token principal does not match the attested "
            "federated principal"
        )
    return AttestedAzureCliCredential(
        credential,
        client_id=identity.client_id,
        tenant_id=identity.tenant_id,
        object_id=object_id,
    )


def _token_identity(token: str) -> dict[str, str]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ContractError("Validation Blob access token is malformed")
    try:
        payload = parts[1] + ("=" * (-len(parts[1]) % 4))
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise ContractError("Validation Blob access token claims are invalid") from error
    if not isinstance(claims, dict):
        raise ContractError("Validation Blob access token claims are invalid")
    identity = {
        "client_id": str(claims.get("appid") or claims.get("azp") or "")
        .strip()
        .casefold(),
        "tenant_id": str(claims.get("tid") or "").strip().casefold(),
        "object_id": str(claims.get("oid") or "").strip().casefold(),
    }
    if not all(identity.values()):
        raise ContractError(
            "Validation Blob access token lacks complete principal claims"
        )
    return identity
