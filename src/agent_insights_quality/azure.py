from __future__ import annotations

import subprocess
from pathlib import Path

from agent_insights_quality.automation_policy import load_automation_policy
from agent_insights_quality.progress import ProgressReporter
from agent_insights_quality.util import ROOT, ContractError
from agent_insights_quality.azure_cli import azure_cli

SWEDEN_DEPLOYMENT_NAME = "agent-insights-quality-swedencentral-g30"
ANALYTICS_DEPLOYMENT_NAME = "agent-insights-quality-analytics-westus2"
_DEPLOYMENT_LOCATIONS = {
    SWEDEN_DEPLOYMENT_NAME: "swedencentral",
    ANALYTICS_DEPLOYMENT_NAME: "westus2",
}


def deploy_infrastructure() -> None:
    progress = ProgressReporter("aiq-infra")
    progress.emit("full infrastructure reconciliation started")
    policy = load_automation_policy()
    telemetry_resource_set = policy.telemetry_resource_set
    if telemetry_resource_set != "g30":
        raise ContractError("Infrastructure telemetry environment is not reviewed")
    principal_id = _current_principal_id(progress)
    _deploy_template(
        ROOT / "infra" / "main.bicep",
        [
            "location=swedencentral",
            "resourceGroupName=agent-insights-quality-rg",
            "terraModelVersion=2026-07-09",
            "testAgentModelVersion=2026-03-17",
            f"telemetryGeneration={telemetry_resource_set}",
            "testAgentCapacity=4500",
            "insightGenerationCapacity=100",
            f"storageAccountPrefix={policy.storage_account_prefix}",
            f"storageResourceRole={policy.storage_resource_role}",
            f"qualityArtifactContainerName={policy.quality_artifact_container}",
            f"deploymentRegistryContainerName={policy.deployment_registry_container}",
            "automationOwner=ninghu",
            f"automationPrincipalId={principal_id}",
        ],
        deployment_name=SWEDEN_DEPLOYMENT_NAME,
        deployment_location="swedencentral",
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
        deployment_name=ANALYTICS_DEPLOYMENT_NAME,
        deployment_location="westus2",
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
    deployment_name: str,
    deployment_location: str,
    progress: ProgressReporter | None = None,
) -> None:
    if _DEPLOYMENT_LOCATIONS.get(deployment_name) != deployment_location:
        raise ContractError("Infrastructure deployment name and location are not reviewed")
    arguments = [
        azure_cli(),
        "deployment",
        "sub",
        "create",
        "--name",
        deployment_name,
        "--location",
        deployment_location,
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
