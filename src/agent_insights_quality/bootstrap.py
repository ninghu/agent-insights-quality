from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Mapping
from typing import Any

from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.contracts import Environment
from agent_insights_quality.errors import QualityError

RESOURCE_GROUP = "agent-insights-quality-rg"
ACCOUNTS = {
    "daily": "aiq-daily-swedencentral",
    "staging": "aiq-staging-swedencentral",
}


def azure_json(arguments: list[str]) -> Any:
    try:
        result = subprocess.run(
            [azure_cli(), *arguments, "--output", "json", "--only-show-errors"],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired as error:
        raise QualityError("azure_metadata_timeout") from error
    if result.returncode:
        raise QualityError("azure_metadata_unavailable")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise QualityError("azure_metadata_invalid") from error


async def discover_environment(profile: str) -> Environment:
    if profile not in ACCOUNTS:
        raise QualityError("profile_invalid")
    resources = await asyncio.to_thread(
        azure_json, ["resource", "list", "--resource-group", RESOURCE_GROUP],
    )
    if not isinstance(resources, list):
        raise QualityError("azure_metadata_invalid")

    def unique(kind: str, predicate) -> dict[str, Any]:
        matches = [
            item for item in resources
            if isinstance(item, dict)
            and str(item.get("type", "")).casefold() == kind
            and predicate(item)
        ]
        if len(matches) != 1:
            raise QualityError("environment_resource_ambiguous")
        return matches[0]

    account = unique(
        "microsoft.cognitiveservices/accounts",
        lambda item: item.get("name") == ACCOUNTS[profile],
    )
    def tags(item: Mapping[str, Any]) -> Mapping[str, Any]:
        value = item.get("tags")
        return value if isinstance(value, Mapping) else {}

    insights = unique(
        "microsoft.insights/components",
        lambda item: tags(item).get("profile") == profile
        and tags(item).get("generation") == "g30",
    )
    storage = unique(
        "microsoft.storage/storageaccounts",
        lambda item: str(item.get("name", "")).startswith("aiqsweart")
        and tags(item).get("generation") == "g30",
    )
    registry = unique("microsoft.containerregistry/registries", lambda item: True)
    from agent_insights_quality.providers import resolve_environment

    environment = await resolve_environment(
        profile=profile,
        account_resource_id=account["id"],
        project_name=ACCOUNTS[profile],
        application_insights_resource_id=insights["id"],
        storage_account_name=storage["name"],
        registry_name=registry["name"],
    )
    if environment.location.casefold() != "swedencentral":
        raise QualityError("environment_region_mismatch")
    return environment
