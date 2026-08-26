from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from agent_insights_quality.registry import PROFILE_PROJECTS
from agent_insights_quality.util import ROOT, ContractError, read_yaml, runtime_root
from agent_insights_quality.azure_cli import azure_cli

RESOURCE_GROUP = "agent-insights-quality-rg"
TELEMETRY_GENERATION = str(
    read_yaml(ROOT / "config" / "automation.yaml")["telemetry_generation"]
)


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

    @classmethod
    def from_env(cls, name: str) -> "RuntimeProfile":
        if name not in PROFILE_PROJECTS:
            raise ContractError("Profile must be daily or staging")
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
            and item["tags"].get("generation") == TELEMETRY_GENERATION
        ]
        if (
            len(accounts) != 1
            or len(registries) != 1
            or len(storage_accounts) != 1
            or len(profile_insights) != 1
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
        )


def _azure_resources() -> list[dict]:
    process = subprocess.run(
        [
            azure_cli(),
            "resource",
            "list",
            "--resource-group",
            RESOURCE_GROUP,
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if process.returncode != 0:
        raise ContractError("Fixed Azure resource group could not be queried")
    value = json.loads(process.stdout)
    if not isinstance(value, list):
        raise ContractError("Azure resource discovery returned an invalid payload")
    return [item for item in value if isinstance(item, dict)]
