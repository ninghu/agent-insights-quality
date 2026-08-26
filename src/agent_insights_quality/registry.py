from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.util import ROOT, ContractError, read_json, read_yaml

PROFILE_PROJECTS = {
    "daily": "agent-insights-quality",
    "staging": "agent-insights-quality-staging",
}
REGISTRY_CONTAINER = "deployment-registries"


def sync_registry(profile: Any) -> None:
    account = str(profile.registry_storage_account_name or "").strip()
    if not account:
        raise ContractError("Private registry storage account could not be resolved")
    profile.registry_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{profile.name}-registry.",
        dir=profile.registry_path.parent,
    )
    os.close(descriptor)
    try:
        process = subprocess.run(
            [
                azure_cli(),
                "storage",
                "blob",
                "download",
                "--account-name",
                account,
                "--container-name",
                REGISTRY_CONTAINER,
                "--name",
                f"{profile.name}.json",
                "--file",
                temporary,
                "--auth-mode",
                "login",
                "--overwrite",
                "true",
                "--only-show-errors",
                "--output",
                "none",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if process.returncode != 0:
            raise ContractError("Private deployment registry download failed")
        os.replace(temporary, profile.registry_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def publish_registry(profile: Any) -> None:
    account = str(profile.registry_storage_account_name or "").strip()
    if not account:
        raise ContractError("Private registry storage account could not be resolved")
    process = subprocess.run(
        [
            azure_cli(),
            "storage",
            "blob",
            "upload",
            "--account-name",
            account,
            "--container-name",
            REGISTRY_CONTAINER,
            "--name",
            f"{profile.name}.json",
            "--file",
            str(profile.registry_path),
            "--auth-mode",
            "login",
            "--overwrite",
            "true",
            "--only-show-errors",
            "--output",
            "none",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if process.returncode != 0:
        raise ContractError("Private deployment registry upload failed")


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
    agent_catalog = read_yaml(ROOT / "catalogs" / "AGENT_CATALOG.yaml")
    expected = {
        agent["name"]: {"v0", *agent["issue_ids"]}
        for agent in agent_catalog["agents"]
    }
    if set(registry["agents"]) != set(expected) or any(
        set(registry["agents"][name]["versions"]) != logical_versions
        for name, logical_versions in expected.items()
    ):
        raise ContractError("Deployment registry version inventory is incomplete")
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
