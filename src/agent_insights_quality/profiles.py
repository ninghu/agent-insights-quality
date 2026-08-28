from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from agent_insights_quality.automation_policy import load_automation_policy
from agent_insights_quality.progress import ProgressReporter
from agent_insights_quality.registry import PROFILE_PROJECTS
from agent_insights_quality.util import ContractError, runtime_root
from agent_insights_quality.azure_cli import azure_cli

RESOURCE_GROUP = "agent-insights-quality-rg"
_PROGRESS = ProgressReporter("aiq-profile")


@dataclass(frozen=True)
class RuntimeProfile:
    name: str
    project_name: str
    project_endpoint: str
    insights_endpoint: str
    application_insights_resource_id: str
    registry_path: Path
    account_name: str = ""
    container_registry_name: str = ""
    registry_storage_account_name: str = ""
    account_resource_id: str = ""
    telemetry_resource_set: str = ""

    @classmethod
    def from_env(
        cls,
        name: str,
        telemetry_resource_set: str | None = None,
    ) -> "RuntimeProfile":
        if name not in PROFILE_PROJECTS:
            raise ContractError("Profile must be daily or staging")
        resource_set = (
            telemetry_resource_set
            if telemetry_resource_set is not None
            else load_automation_policy().telemetry_resource_set
        )
        resources = _azure_resources()
        accounts = [
            item
            for item in resources
            if str(item.get("type") or "").casefold()
            == "microsoft.cognitiveservices/accounts"
            and item.get("kind") == "AIServices"
            and isinstance(item.get("tags"), dict)
            and item["tags"].get("profile") == name
        ]
        registries = [
            item
            for item in resources
            if str(item.get("type") or "").casefold()
            == "microsoft.containerregistry/registries"
        ]
        storage_accounts = [
            item
            for item in resources
            if str(item.get("type") or "").casefold()
            == "microsoft.storage/storageaccounts"
            and isinstance(item.get("tags"), dict)
            and item["tags"].get("purpose") == "agent-insights-quality"
        ]
        profile_insights = [
            item
            for item in resources
            if str(item.get("type") or "").casefold()
            == "microsoft.insights/components"
            and isinstance(item.get("tags"), dict)
            and item["tags"].get("profile") == name
            and item["tags"].get("generation") == resource_set
        ]
        if len(profile_insights) != 1:
            raise ContractError(
                f"Telemetry resource set {resource_set} is retired, missing, or ambiguous"
            )
        if (
            len(accounts) != 1
            or len(registries) != 1
            or len(storage_accounts) != 1
        ):
            raise ContractError(
                "Fixed Azure resources could not be resolved uniquely for the profile"
            )
        account_name = str(accounts[0]["name"])
        project_name = PROFILE_PROJECTS[name]
        endpoint = (
            f"https://{account_name}.services.ai.azure.com/api/projects/{project_name}"
        )
        resource_id = str(profile_insights[0]["id"])
        return cls(
            name=name,
            project_name=project_name,
            project_endpoint=endpoint,
            insights_endpoint=endpoint,
            application_insights_resource_id=resource_id,
            registry_path=runtime_root() / "deployment-registries" / f"{name}.json",
            account_name=account_name,
            container_registry_name=str(registries[0]["name"]),
            registry_storage_account_name=str(storage_accounts[0]["name"]),
            account_resource_id=str(accounts[0]["id"]),
            telemetry_resource_set=resource_set,
        )

    def assert_insights_connection(self) -> None:
        if not self.account_resource_id:
            raise ContractError("Profile account resource identity is unavailable")
        connection_id = (
            f"{self.account_resource_id}/projects/{self.project_name}/connections/"
            f"application-insights-{self.name}"
        )
        process = _run_azure_read(
            [
                azure_cli(),
                "rest",
                "--method",
                "get",
                "--url",
                "https://management.azure.com"
                + connection_id
                + "?api-version=2025-06-01",
                "--output",
                "json",
            ],
        )
        if process.returncode != 0:
            raise ContractError("Project telemetry connection could not be queried")
        value = json.loads(process.stdout)
        target = str(value.get("properties", {}).get("target") or "")
        if target.casefold() != self.application_insights_resource_id.casefold():
            raise ContractError(
                "Project telemetry connection does not match the active resource set"
            )

    def assert_test_agent_model(self, expected: dict[str, str]) -> None:
        if not self.account_name:
            raise ContractError("Profile account name is unavailable")
        process = _run_azure_read(
            [
                azure_cli(),
                "cognitiveservices",
                "account",
                "deployment",
                "show",
                "--name",
                self.account_name,
                "--resource-group",
                RESOURCE_GROUP,
                "--deployment-name",
                expected["deployment_name"],
                "--output",
                "json",
            ]
        )
        if process.returncode != 0:
            raise ContractError("Test Agent model deployment could not be queried")
        value = json.loads(process.stdout)
        properties = value.get("properties") or {}
        model = properties.get("model") or {}
        if (
            properties.get("provisioningState") != "Succeeded"
            or str(value.get("name") or "") != expected["deployment_name"]
            or str(model.get("name") or "") != expected["model_id"]
            or str(model.get("version") or "") != expected["model_version"]
        ):
            raise ContractError("Test Agent model deployment is not the reviewed version")


def _azure_resources() -> list[dict]:
    process = _run_azure_read(
        [
            azure_cli(),
            "resource",
            "list",
            "--resource-group",
            RESOURCE_GROUP,
            "--output",
            "json",
        ],
    )
    if process.returncode != 0:
        raise ContractError("Fixed Azure resource group could not be queried")
    value = json.loads(process.stdout)
    if not isinstance(value, list):
        raise ContractError("Azure resource discovery returned an invalid payload")
    return [item for item in value if isinstance(item, dict)]


def _run_azure_read(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    process: subprocess.CompletedProcess[str] | None = None
    for attempt in range(3):
        try:
            with _PROGRESS.heartbeat(
                f"Azure profile read attempt {attempt + 1}/3"
            ) as outcome:
                process = subprocess.run(
                    arguments,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if process.returncode != 0:
                    outcome.fail()
        except subprocess.TimeoutExpired:
            if attempt == 2:
                raise ContractError("Azure read timed out after bounded retries") from None
            time.sleep(2**attempt)
            continue
        if process.returncode == 0:
            return process
        if attempt < 2:
            time.sleep(2**attempt)
    if process is None:
        raise ContractError("Azure read retry loop did not execute")
    return process
