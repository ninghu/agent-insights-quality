from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from agent_insights_quality.automation_policy import load_automation_policy
from agent_insights_quality.progress import ProgressReporter
from agent_insights_quality.registry import (
    ENVIRONMENT_ID,
    PROFILE_LOCATION,
    PROFILE_PROJECTS,
    TELEMETRY_RESOURCE_SET,
)
from agent_insights_quality.util import ContractError, runtime_root
from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.azure_regions import location_display_name

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
    environment_id: str = ENVIRONMENT_ID
    location: str = PROFILE_LOCATION

    @classmethod
    def from_env(
        cls,
        name: str,
        telemetry_resource_set: str | None = None,
    ) -> "RuntimeProfile":
        if name not in PROFILE_PROJECTS:
            raise ContractError("Profile must be daily or staging")
        policy = load_automation_policy()
        resource_set = (
            telemetry_resource_set
            if telemetry_resource_set is not None
            else policy.telemetry_resource_set
        )
        if resource_set != TELEMETRY_RESOURCE_SET:
            raise ContractError("Profile telemetry resource set is not the active environment")
        project_name = PROFILE_PROJECTS[name]
        resources = _azure_resources()
        accounts = [
            item
            for item in resources
            if str(item.get("type") or "").casefold()
            == "microsoft.cognitiveservices/accounts"
            and item.get("kind") == "AIServices"
            and isinstance(item.get("tags"), dict)
            and item["tags"].get("profile") == name
            and item["tags"].get("environment") == PROFILE_LOCATION
            and item["tags"].get("location") == PROFILE_LOCATION
            and item["tags"].get("generation") == resource_set
            and str(item.get("location") or "").casefold() == PROFILE_LOCATION
            and item.get("name") == project_name
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
            and str(item.get("kind") or "").casefold() == "storagev2"
            and str(item.get("name") or "").startswith(policy.storage_account_prefix)
            and isinstance(item.get("tags"), dict)
            and item["tags"].get("purpose") == "agent-insights-quality"
            and item["tags"].get("environment") == PROFILE_LOCATION
            and item["tags"].get("location") == PROFILE_LOCATION
            and item["tags"].get("generation") == resource_set
            and item["tags"].get("resourceRole") == policy.storage_resource_role
            and str(item.get("location") or "").casefold() == PROFILE_LOCATION
        ]
        profile_insights = [
            item
            for item in resources
            if str(item.get("type") or "").casefold()
            == "microsoft.insights/components"
            and isinstance(item.get("tags"), dict)
            and item["tags"].get("profile") == name
            and item["tags"].get("environment") == PROFILE_LOCATION
            and item["tags"].get("location") == PROFILE_LOCATION
            and item["tags"].get("generation") == resource_set
            and str(item.get("location") or "").casefold() == PROFILE_LOCATION
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
            registry_path=(
                runtime_root()
                / "deployment-registries"
                / ENVIRONMENT_ID
                / f"{name}.json"
            ),
            account_name=account_name,
            container_registry_name=str(registries[0]["name"]),
            registry_storage_account_name=str(storage_accounts[0]["name"]),
            account_resource_id=str(accounts[0]["id"]),
            telemetry_resource_set=resource_set,
            environment_id=ENVIRONMENT_ID,
            location=PROFILE_LOCATION,
        )

    def assert_insights_connection(
        self,
        connection_name: str | None = None,
    ) -> None:
        if not self.account_resource_id:
            raise ContractError("Profile account resource identity is unavailable")
        project_connection_name = connection_name or f"application-insights-{self.name}"
        project_connection_id = (
            f"{self.account_resource_id}/projects/{self.project_name}/connections/"
            f"{project_connection_name}"
        )
        project_connection = _read_arm_connection(
            project_connection_id,
            "Project",
        )
        _assert_arm_insights_connection(
            project_connection,
            expected_target=self.application_insights_resource_id,
            scope="Project",
        )

    def with_project(
        self,
        *,
        name: str,
        project_name: str,
        registry_path: Path,
    ) -> "RuntimeProfile":
        if not name or not project_name:
            raise ContractError("Derived runtime profile identity is required")
        if not self.account_name or not self.account_resource_id:
            raise ContractError("Derived runtime profile requires a Foundry account")
        endpoint = (
            f"https://{self.account_name}.services.ai.azure.com/api/projects/"
            f"{project_name}"
        )
        return RuntimeProfile(
            name=name,
            project_name=project_name,
            project_endpoint=endpoint,
            insights_endpoint=endpoint,
            application_insights_resource_id=self.application_insights_resource_id,
            registry_path=registry_path,
            account_name=self.account_name,
            container_registry_name=self.container_registry_name,
            registry_storage_account_name=self.registry_storage_account_name,
            account_resource_id=self.account_resource_id,
            telemetry_resource_set=self.telemetry_resource_set,
            environment_id=self.environment_id,
            location=self.location,
        )

    def resolve_test_region(self) -> str:
        if not self.account_resource_id or not self.project_name:
            raise ContractError("Foundry Project identity is unavailable")
        project = _run_azure_read(
            [
                azure_cli(),
                "rest",
                "--method",
                "get",
                "--url",
                "https://management.azure.com"
                + self.account_resource_id
                + "/projects/"
                + self.project_name
                + "?api-version=2025-06-01",
                "--output",
                "json",
            ]
        )
        if project.returncode != 0:
            raise ContractError("Foundry Project location could not be queried")
        try:
            project_value = json.loads(project.stdout)
        except json.JSONDecodeError as error:
            raise ContractError("Foundry Project location response is invalid") from error
        location = (
            str(project_value.get("location") or "").strip()
            if isinstance(project_value, dict)
            else ""
        )
        if not location:
            raise ContractError("Foundry Project location is missing")
        locations = _run_azure_read(
            [
                azure_cli(),
                "account",
                "list-locations",
                "--output",
                "json",
            ]
        )
        if locations.returncode != 0:
            raise ContractError("Azure location metadata could not be queried")
        try:
            location_values = json.loads(locations.stdout)
        except json.JSONDecodeError as error:
            raise ContractError("Azure location metadata is invalid") from error
        if not isinstance(location_values, list):
            raise ContractError("Azure location metadata is invalid")
        return location_display_name(
            location,
            [item for item in location_values if isinstance(item, dict)],
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
            or str((value.get("sku") or {}).get("name") or "")
            != "DataZoneStandard"
            or (value.get("sku") or {}).get("capacity") != 4500
        ):
            raise ContractError("Test Agent model deployment is not the reviewed version")


def _read_arm_connection(connection_id: str, scope: str) -> dict:
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
        raise ContractError(f"{scope} telemetry connection could not be queried")
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise ContractError(f"{scope} telemetry connection response is invalid") from error
    if not isinstance(value, dict):
        raise ContractError(f"{scope} telemetry connection response is invalid")
    return value


def _assert_arm_insights_connection(
    value: dict,
    *,
    expected_target: str,
    scope: str,
) -> None:
    properties = value.get("properties")
    if not isinstance(properties, dict):
        raise ContractError(f"{scope} telemetry connection response is invalid")
    metadata = properties.get("metadata")
    target = str(properties.get("target") or "")
    if target.casefold() != expected_target.casefold():
        raise ContractError(
            f"{scope} telemetry connection does not match the active resource set"
        )
    if (
        properties.get("category") != "AppInsights"
        or properties.get("authType") != "ApiKey"
        or not isinstance(metadata, dict)
        or metadata.get("ApiType") != "Azure"
        or str(metadata.get("ResourceId") or "").casefold()
        != expected_target.casefold()
    ):
        raise ContractError(
            f"{scope} telemetry connection is not the official App Insights shape"
        )


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
