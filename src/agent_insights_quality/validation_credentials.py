from __future__ import annotations

import base64
import json
import subprocess
from typing import Any

from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.util import ContractError


def verify_azure_service_principal(expected_client_id: str) -> None:
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
    if identity_type != "serviceprincipal" or client_id != expected:
        raise ContractError(
            "Selected Azure credential does not match the attested federated principal"
        )


def validation_blob_credential(expected_client_id: str) -> Any:
    verify_azure_service_principal(expected_client_id)
    try:
        from azure.identity import AzureCliCredential
    except ImportError as error:
        raise ContractError(
            "Validation Blob operations require the azure optional dependencies"
        ) from error
    credential = AzureCliCredential()
    token = credential.get_token("https://storage.azure.com/.default")
    principal = _token_principal(str(token.token))
    if principal.casefold() != expected_client_id.strip().casefold():
        raise ContractError(
            "Validation Blob token principal does not match the attested "
            "federated principal"
        )
    return credential


def _token_principal(token: str) -> str:
    parts = token.split(".")
    if len(parts) != 3:
        raise ContractError("Validation Blob access token is malformed")
    try:
        payload = parts[1] + ("=" * (-len(parts[1]) % 4))
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError) as error:
        raise ContractError("Validation Blob access token claims are invalid") from error
    if not isinstance(claims, dict):
        raise ContractError("Validation Blob access token claims are invalid")
    principal = str(claims.get("appid") or claims.get("azp") or "").strip()
    if not principal:
        raise ContractError("Validation Blob access token has no principal claim")
    return principal
