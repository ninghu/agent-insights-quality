from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Mapping

from agent_insights_quality.automation_policy import load_automation_policy
from agent_insights_quality.util import ROOT, ContractError
from agent_insights_quality.azure_cli import azure_cli


def deploy_infrastructure(
    environment: Mapping[str, str] | None = None,
) -> None:
    del environment
    terra_model_version = resolve_latest_terra_version()
    telemetry_resource_set = load_automation_policy().telemetry_resource_set
    identity = subprocess.run(
        [azure_cli(), "ad", "signed-in-user", "show", "--query", "id", "--output", "tsv"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    principal_id = identity.stdout.strip()
    if identity.returncode != 0 or not principal_id:
        raise ContractError("Current Azure user identity could not be resolved")

    arguments = [
        azure_cli(),
        "deployment",
        "sub",
        "create",
        "--location",
        "westus2",
        "--template-file",
        str(ROOT / "infra" / "main.bicep"),
        "--parameters",
        "location=westus2",
        "resourceGroupName=agent-insights-quality-rg",
        f"terraModelVersion={terra_model_version}",
        f"telemetryGeneration={telemetry_resource_set}",
        "automationOwner=ninghu",
        f"automationPrincipalId={principal_id}",
        "--only-show-errors",
        "--output",
        "none",
    ]
    process = subprocess.run(
        arguments,
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        timeout=45 * 60,
        check=False,
    )
    if process.returncode != 0:
        raise ContractError("Infrastructure deployment failed; inspect protected Azure diagnostics")


def resolve_latest_terra_version() -> str:
    account = subprocess.run(
        [azure_cli(), "account", "show", "--output", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if account.returncode != 0:
        raise ContractError("Current Azure subscription could not be resolved")
    subscription_id = str(json.loads(account.stdout).get("id") or "")
    if not subscription_id:
        raise ContractError("Current Azure subscription has no identity")
    url = (
        "https://management.azure.com/subscriptions/"
        + subscription_id
        + "/providers/Microsoft.CognitiveServices/locations/westus2/models"
        + "?api-version=2025-06-01"
    )
    response = subprocess.run(
        [azure_cli(), "rest", "--method", "get", "--url", url, "--output", "json"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if response.returncode != 0:
        raise ContractError("Azure model catalog query failed")
    versions = []
    for item in json.loads(response.stdout).get("value", []):
        match = re.fullmatch(
            r"OpenAI\.gpt-5\.6-terra\.(\d{4}-\d{2}-\d{2})",
            str(item.get("name") or ""),
        )
        if match:
            versions.append(match.group(1))
    if not versions:
        raise ContractError("GPT-5.6 Terra is unavailable in West US 2")
    return max(versions)
