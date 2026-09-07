from __future__ import annotations

import re

from agent_insights_quality.azure_regions import location_display_name
from agent_insights_quality.contracts import Environment
from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers.transport import (
    ARM_SCOPE,
    AzureHttpTransport,
    JsonClient,
    Transport,
    segment,
)


async def resolve_environment(
    *,
    profile: str,
    account_resource_id: str,
    project_name: str,
    application_insights_resource_id: str,
    storage_account_name: str,
    registry_name: str,
    transport: Transport | None = None,
) -> Environment:
    """Resolve only explicitly selected resources; never choose another account or profile."""
    match = re.fullmatch(
        r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/"
        r"Microsoft\.CognitiveServices/accounts/([a-zA-Z0-9-]+)",
        account_resource_id,
        flags=re.IGNORECASE,
    )
    if profile not in {"daily", "staging"} or not match:
        raise QualityError("environment_binding_invalid")
    if not all(
        (
            project_name,
            application_insights_resource_id,
            storage_account_name,
            registry_name,
        )
    ):
        raise QualityError("environment_binding_missing")
    subscription, _, account = match.groups()
    client = JsonClient(
        "https://management.azure.com",
        transport if transport is not None else AzureHttpTransport(),
        scope=ARM_SCOPE,
    )
    project = await client.object(
        "GET",
        account_resource_id + "/projects/" + segment(project_name),
        api_version="2025-06-01",
    )
    location = project.get("location")
    if not isinstance(location, str) or not location:
        raise QualityError("project_location_missing")
    locations = await client.object(
        "GET",
        "/subscriptions/" + segment(subscription) + "/locations",
        api_version="2022-12-01",
    )
    metadata = locations.get("value")
    if not isinstance(metadata, list) or any(
        not isinstance(item, dict) for item in metadata
    ):
        raise QualityError("region_metadata_unavailable")
    display = location_display_name(location, metadata)
    endpoint = (
        f"https://{account}.services.ai.azure.com/api/projects/{segment(project_name)}"
    )
    return Environment(
        profile,
        account,
        project_name,
        endpoint,
        application_insights_resource_id,
        storage_account_name,
        registry_name,
        location,
        display,
        endpoint,
    )
