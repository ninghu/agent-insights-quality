from __future__ import annotations

import json
import subprocess

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
