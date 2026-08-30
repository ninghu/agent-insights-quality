from __future__ import annotations

import base64
import binascii
import json
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.util import ContractError, content_hash


@dataclass(frozen=True)
class LocalAzureOperator:
    credential: Any
    object_id: str
    tenant_id: str
    subscription_id: str
    operator_reference: str

    def token_provider(self, scope: str) -> str:
        return str(self.credential.get_token(scope).token)


class VerifiedAzureCliCredential:
    def __init__(
        self,
        credential: Any,
        *,
        tenant_id: str,
        object_id: str,
    ) -> None:
        self._credential = credential
        self._tenant_id = tenant_id.casefold()
        self._object_id = object_id.casefold()
        self._lock = threading.Lock()
        self._tokens: dict[tuple[str, ...], Any] = {}

    def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        key = tuple(scopes)
        with self._lock:
            token = self._tokens.get(key) if not kwargs else None
            expires_on = getattr(token, "expires_on", 0)
            if (
                not isinstance(expires_on, (int, float))
                or expires_on <= time.time() + 300
            ):
                try:
                    token = self._credential.get_token(*scopes, **kwargs)
                except _azure_credential_errors() as error:
                    raise ContractError(
                        "Azure CLI token acquisition failed"
                    ) from error
                expires_on = getattr(token, "expires_on", 0)
                if (
                    not kwargs
                    and isinstance(expires_on, (int, float))
                    and expires_on > time.time() + 300
                ):
                    self._tokens[key] = token
        identity = _token_identity(str(token.token))
        if (
            identity["tenant_id"] != self._tenant_id
            or identity["object_id"] != self._object_id
        ):
            raise ContractError(
                "Azure CLI token identity changed from the verified local operator"
            )
        return token


def local_azure_operator() -> LocalAzureOperator:
    account = _azure_json(
        [azure_cli(), "account", "show", "--output", "json"],
        "Azure CLI account",
    )
    user = account.get("user")
    tenant_id = str(account.get("tenantId") or "").strip()
    subscription_id = str(account.get("id") or "").strip()
    if (
        not isinstance(user, dict)
        or str(user.get("type") or "").casefold() != "user"
        or not tenant_id
        or not subscription_id
    ):
        raise ContractError(
            "Local validation requires an authenticated Azure CLI user"
        )
    signed_in = _azure_json(
        [
            azure_cli(),
            "ad",
            "signed-in-user",
            "show",
            "--output",
            "json",
        ],
        "Azure CLI signed-in user",
    )
    object_id = str(signed_in.get("id") or "").strip()
    if not object_id:
        raise ContractError("Local Azure operator object identity is missing")
    try:
        from azure.identity import AzureCliCredential
    except ImportError as error:
        raise ContractError(
            "Local validation requires the azure optional dependencies"
        ) from error
    raw = AzureCliCredential(process_timeout=60)
    credential = VerifiedAzureCliCredential(
        raw,
        tenant_id=tenant_id,
        object_id=object_id,
    )
    credential.get_token("https://storage.azure.com/.default")
    return LocalAzureOperator(
        credential=credential,
        object_id=object_id,
        tenant_id=tenant_id,
        subscription_id=subscription_id,
        operator_reference=content_hash(
            {
                "tenant_id": tenant_id.casefold(),
                "subscription_id": subscription_id.casefold(),
                "object_id": object_id.casefold(),
            }
        ),
    )


def _azure_json(arguments: list[str], label: str) -> dict[str, Any]:
    process = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if process.returncode != 0:
        raise ContractError(f"{label} could not be queried")
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise ContractError(f"{label} response is invalid") from error
    if not isinstance(value, dict):
        raise ContractError(f"{label} response is not an object")
    return value


def _azure_credential_errors() -> tuple[type[BaseException], ...]:
    try:
        from azure.core.exceptions import ClientAuthenticationError
        from azure.identity import CredentialUnavailableError
    except ImportError:
        return ()
    return (ClientAuthenticationError, CredentialUnavailableError)


def _token_identity(token: str) -> dict[str, str]:
    claims = token_claims(token)
    identity = {
        "tenant_id": str(claims.get("tid") or "").strip().casefold(),
        "object_id": str(claims.get("oid") or "").strip().casefold(),
    }
    if not all(identity.values()):
        raise ContractError(
            "Azure CLI access token lacks complete operator claims"
        )
    return identity


def token_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ContractError("Azure CLI access token is malformed")
    try:
        payload = parts[1] + ("=" * (-len(parts[1]) % 4))
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise ContractError("Azure CLI access token claims are invalid") from error
    if not isinstance(claims, dict):
        raise ContractError("Azure CLI access token claims are invalid")
    return claims
