from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from agent_insights_quality.util import ROOT, ContractError, read_json

PROFILE_PROJECTS = {
    "daily": "agent-insights-quality",
    "staging": "agent-insights-quality-staging",
}


def load_registry(
    path: Path,
    *,
    profile: str,
    catalog_hashes: dict[str, str],
) -> dict[str, Any]:
    registry = read_json(path)
    schema = read_json(ROOT / "schemas" / "deployment-registry.schema.json")
    errors = sorted(
        Draft202012Validator(schema).iter_errors(registry),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise ContractError(f"Deployment registry is invalid: {errors[0].message}")
    if profile not in PROFILE_PROJECTS:
        raise ContractError("Profile must be daily or staging")
    if (
        registry["profile"] != profile
        or registry["project_name"] != PROFILE_PROJECTS[profile]
    ):
        raise ContractError("Deployment registry belongs to a different profile")
    if registry["catalog_hashes"] != catalog_hashes:
        raise ContractError("Deployment registry catalog hashes are stale")
    return registry


def version_entry(
    registry: dict[str, Any],
    agent_name: str,
    logical_version: str,
) -> dict[str, str]:
    try:
        value = registry["agents"][agent_name]["versions"][logical_version]
    except KeyError as error:
        raise ContractError(
            f"Deployment registry has no {agent_name}/{logical_version}"
        ) from error
    return {
        "foundry_version": str(value["foundry_version"]),
        "content_digest": str(value["content_digest"]),
    }
