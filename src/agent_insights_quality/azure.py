from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agent_insights_quality.automation_policy import load_automation_policy
from agent_insights_quality.progress import ProgressReporter
from agent_insights_quality.util import ROOT, ContractError
from agent_insights_quality.azure_cli import azure_cli


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
            f"approvedRecordContainerName={policy.approved_record_container}",
            "automationOwner=ninghu",
            f"automationPrincipalId={principal_id}",
        ],
        deployment_location="swedencentral",
        progress=progress,
    )
    _lock_approved_validation_policy(progress)
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
    deployment_location: str,
    progress: ProgressReporter | None = None,
) -> None:
    arguments = [
        azure_cli(),
        "deployment",
        "sub",
        "create",
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


def _lock_approved_validation_policy(progress: ProgressReporter) -> None:
    policy = load_automation_policy()
    container = policy.approved_record_container
    account_query = (
        "[?starts_with(name, '"
        + policy.storage_account_prefix
        + "') && kind=='StorageV2'"
        " && location=='swedencentral'"
        " && tags.purpose=='agent-insights-quality'"
        " && tags.environment=='swedencentral'"
        " && tags.location=='swedencentral'"
        " && tags.generation=='"
        + policy.telemetry_resource_set
        + "' && tags.resourceRole=='"
        + policy.storage_resource_role
        + "'].name"
    )
    account = subprocess.run(
        [
            azure_cli(),
            "storage",
            "account",
            "list",
            "--resource-group",
            "agent-insights-quality-rg",
            "--query",
            account_query,
            "--output",
            "tsv",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    names = [item for item in account.stdout.splitlines() if item.strip()]
    if (
        account.returncode != 0
        or len(names) != 1
        or not names[0].startswith(policy.storage_account_prefix)
    ):
        raise ContractError(
            "Approved validation record storage account is missing or ambiguous"
        )
    arguments = [
        "--account-name",
        names[0],
        "--container-name",
        container,
        "--output",
        "json",
    ]

    def read_policy(*, allow_missing: bool = False) -> dict | None:
        process = subprocess.run(
            [
                azure_cli(),
                "storage",
                "container",
                "immutability-policy",
                "show",
                *arguments,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        error_lines = [
            line.strip()
            for line in str(getattr(process, "stderr", "") or "").splitlines()
            if line.strip()
        ]
        if (
            allow_missing
            and process.returncode != 0
            and error_lines
            and error_lines[0].startswith("(ResourceNotFound)")
            and "Code: ResourceNotFound" in error_lines
        ):
            return None
        try:
            value = json.loads(process.stdout)
        except json.JSONDecodeError as error:
            raise ContractError(
                "Approved validation immutability policy is invalid"
            ) from error
        if (
            process.returncode != 0
            or not isinstance(value, dict)
            or value.get("immutabilityPeriodSinceCreationInDays") != 90
            or value.get("allowProtectedAppendWrites") is not False
        ):
            raise ContractError(
                "Approved validation immutability policy does not match 90-day WORM"
            )
        return value

    policy = read_policy(allow_missing=True)
    if policy is None:
        with progress.heartbeat(
            "approved validation record WORM policy creation"
        ) as outcome:
            created = subprocess.run(
                [
                    azure_cli(),
                    "storage",
                    "container",
                    "immutability-policy",
                    "create",
                    *arguments,
                    "--period",
                    "90",
                    "--allow-protected-append-writes",
                    "false",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if created.returncode != 0:
                outcome.fail()
        if created.returncode != 0:
            raise ContractError(
                "Approved validation immutability policy creation failed"
            )
        policy = read_policy()
        if policy is None:
            raise ContractError(
                "Approved validation immutability policy was not created"
            )
    if policy.get("state") == "Unlocked":
        etag = str(policy.get("etag") or "")
        if not etag:
            raise ContractError(
                "Approved validation immutability policy ETag is missing"
            )
        with progress.heartbeat(
            "approved validation record WORM lock"
        ) as outcome:
            locked = subprocess.run(
                [
                    azure_cli(),
                    "storage",
                    "container",
                    "immutability-policy",
                    "lock",
                    *arguments,
                    "--if-match",
                    etag,
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if locked.returncode != 0:
                outcome.fail()
        if locked.returncode != 0:
            raise ContractError(
                "Approved validation immutability policy lock failed"
            )
        policy = read_policy()
        if policy is None:
            raise ContractError(
                "Approved validation immutability policy disappeared after locking"
            )
    if policy.get("state") != "Locked":
        raise ContractError(
            "Approved validation immutability policy is not locked"
        )
