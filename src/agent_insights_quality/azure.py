from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Mapping

from agent_insights_quality.automation_policy import load_automation_policy
from agent_insights_quality.catalogs import load_catalogs, agent_model_contract
from agent_insights_quality.progress import ProgressReporter
from agent_insights_quality.util import ROOT, ContractError
from agent_insights_quality.azure_cli import azure_cli


def deploy_infrastructure(
    environment: Mapping[str, str] | None = None,
) -> None:
    values = environment or os.environ
    progress = ProgressReporter("aiq-infra")
    progress.emit("full infrastructure reconciliation started")
    terra_model_version = resolve_latest_terra_version()
    test_agent_model_version = agent_model_contract(load_catalogs()[0])[
        "model_version"
    ]
    telemetry_resource_set = load_automation_policy().telemetry_resource_set
    principal_id = _current_principal_id(progress)
    validation_principal_id = str(
        values.get("AIQ_VALIDATION_PRINCIPAL_ID") or ""
    ).strip()
    if not validation_principal_id:
        raise ContractError(
            "Protected validation principal identity is required for infrastructure"
        )
    validation_receipt_principal_id = str(
        values.get("AIQ_VALIDATION_RECEIPT_PRINCIPAL_ID") or ""
    ).strip()
    if not validation_receipt_principal_id:
        raise ContractError(
            "Protected validation receipt principal identity is required for "
            "infrastructure"
        )
    _deploy_template(
        ROOT / "infra" / "main.bicep",
        [
            "location=westus2",
            "resourceGroupName=agent-insights-quality-rg",
            f"terraModelVersion={terra_model_version}",
            f"testAgentModelVersion={test_agent_model_version}",
            f"telemetryGeneration={telemetry_resource_set}",
            "automationOwner=ninghu",
            f"automationPrincipalId={principal_id}",
            f"validationPrincipalId={validation_principal_id}",
            f"validationReceiptPrincipalId={validation_receipt_principal_id}",
        ],
        progress=progress,
    )
    progress.emit("full infrastructure reconciliation completed")


def deploy_analytics_infrastructure() -> None:
    progress = ProgressReporter("aiq-infra")
    progress.emit("ADX infrastructure reconciliation started")
    principal_id = _current_principal_id(progress)
    _deploy_template(
        ROOT / "infra" / "analytics.bicep",
        [
            "location=westus2",
            "resourceGroupName=agent-insights-quality-rg",
            "automationOwner=ninghu",
            f"automationPrincipalId={principal_id}",
        ],
        progress=progress,
    )
    progress.emit("ADX infrastructure reconciliation completed")


def _current_principal_id(progress: ProgressReporter | None = None) -> str:
    reporter = progress or ProgressReporter("aiq-infra")
    with reporter.heartbeat("Azure identity resolution") as outcome:
        identity = subprocess.run(
            [
                azure_cli(),
                "ad",
                "signed-in-user",
                "show",
                "--query",
                "id",
                "--output",
                "tsv",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if identity.returncode != 0:
            outcome.fail()
    principal_id = identity.stdout.strip()
    if identity.returncode != 0 or not principal_id:
        raise ContractError("Current Azure user identity could not be resolved")
    return principal_id


def _deploy_template(
    template: Path,
    parameters: list[str],
    *,
    progress: ProgressReporter | None = None,
) -> None:
    arguments = [
        azure_cli(),
        "deployment",
        "sub",
        "create",
        "--location",
        "westus2",
        "--template-file",
        str(template),
        "--parameters",
        *parameters,
        "--only-show-errors",
        "--output",
        "none",
    ]
    reporter = progress or ProgressReporter("aiq-infra")
    with reporter.heartbeat(f"{template.stem} deployment") as outcome:
        process = subprocess.run(
            arguments,
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            timeout=45 * 60,
            check=False,
        )
        if process.returncode != 0:
            outcome.fail()
    if process.returncode != 0:
        raise ContractError("Infrastructure deployment failed; inspect protected Azure diagnostics")


def resolve_latest_terra_version() -> str:
    return resolve_latest_model_version("gpt-5.6-terra")


def resolve_latest_model_version(
    model_name: str,
    *,
    progress: ProgressReporter | None = None,
) -> str:
    reporter = progress or ProgressReporter("aiq-infra")
    with reporter.heartbeat("Azure subscription resolution") as outcome:
        account = subprocess.run(
            [azure_cli(), "account", "show", "--output", "json"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if account.returncode != 0:
            outcome.fail()
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
    with reporter.heartbeat(f"{model_name} model catalog query") as outcome:
        response = subprocess.run(
            [azure_cli(), "rest", "--method", "get", "--url", url, "--output", "json"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if response.returncode != 0:
            outcome.fail()
    if response.returncode != 0:
        raise ContractError("Azure model catalog query failed")
    versions = []
    for item in json.loads(response.stdout).get("value", []):
        match = re.fullmatch(
            rf"OpenAI\.{re.escape(model_name)}\.(\d{{4}}-\d{{2}}-\d{{2}})",
            str(item.get("name") or ""),
        )
        if match:
            versions.append(match.group(1))
    if not versions:
        raise ContractError(f"{model_name} is unavailable in West US 2")
    return max(versions)
