from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_insights_quality.automation_policy import load_automation_policy
from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.profiles import RESOURCE_GROUP, RuntimeProfile
from agent_insights_quality.util import (
    ContractError,
    atomic_json,
    content_hash,
    read_json,
    runtime_root,
)

_COMPONENT = "microsoft.insights/components"
_WORKSPACE = "microsoft.operationalinsights/workspaces"
_TELEMETRY_TYPES = {_COMPONENT, _WORKSPACE}
_STABLE_COUNTS = {
    "microsoft.cognitiveservices/accounts": 2,
    "microsoft.cognitiveservices/accounts/projects": 2,
    "microsoft.containerregistry/registries": 1,
    "microsoft.storage/storageaccounts": 1,
}


def write_cleanup_plan(path: Path) -> dict[str, Any]:
    _require_private_path(path)
    active = load_automation_policy().telemetry_resource_set
    for profile_name in ("daily", "staging"):
        profile = RuntimeProfile.from_env(profile_name, active)
        profile.assert_insights_connection()
    plan = build_cleanup_plan(_inventory(), active)
    atomic_json(path, plan)
    return plan


def apply_cleanup_plan(path: Path, receipt_path: Path) -> dict[str, Any]:
    _require_private_path(path)
    _require_private_path(receipt_path)
    plan = read_json(path)
    _validate_plan_hash(plan)
    active = load_automation_policy().telemetry_resource_set
    if plan.get("active_resource_set") != active:
        raise ContractError("Telemetry cleanup plan does not match the active resource set")
    live_plan = build_cleanup_plan(
        _inventory(),
        active,
        require_complete_retired_pairs=False,
    )
    reviewed = {
        item["resource_id"]: item
        for item in plan["resources"]
    }
    live = {
        item["resource_id"]: item
        for item in live_plan["resources"]
    }
    if any(
        resource_id not in reviewed or reviewed[resource_id] != item
        for resource_id, item in live.items()
    ):
        raise ContractError("Telemetry cleanup inventory changed after review")
    for profile_name in ("daily", "staging"):
        profile = RuntimeProfile.from_env(profile_name, active)
        profile.assert_insights_connection()
    resources = list(live.values())
    ordered = sorted(
        resources,
        key=lambda item: (
            0 if item["resource_type"] == _COMPONENT else 1,
            item["generation"],
            item["profile"],
        ),
    )
    for item in ordered:
        process = subprocess.run(
            [
                azure_cli(),
                "resource",
                "delete",
                "--ids",
                item["resource_id"],
                "--only-show-errors",
            ],
            capture_output=True,
            text=True,
            timeout=15 * 60,
            check=False,
        )
        if process.returncode != 0:
            raise ContractError("Exact telemetry resource deletion failed")
    deadline = time.monotonic() + 15 * 60
    while time.monotonic() < deadline:
        remaining = build_cleanup_plan(
            _inventory(),
            active,
            require_complete_retired_pairs=False,
        )
        if not remaining["resources"]:
            break
        time.sleep(10)
    else:
        raise ContractError("Telemetry cleanup did not converge before the deadline")
    inventory = _inventory()
    _validate_final_inventory(inventory, active)
    receipt = {
        "schema_version": "1.0.0",
        "active_resource_set": active,
        "plan_hash": plan["plan_hash"],
        "deleted_resource_count": len(resources),
        "already_absent_resource_count": len(plan["resources"]) - len(resources),
        "completed_at": datetime.now(UTC).isoformat(),
        "remaining_owned_resource_count": 10,
    }
    receipt["receipt_hash"] = content_hash(receipt)
    atomic_json(receipt_path, receipt)
    return receipt


def build_cleanup_plan(
    resources: list[dict[str, Any]],
    active_resource_set: str,
    *,
    require_complete_retired_pairs: bool = True,
) -> dict[str, Any]:
    telemetry = []
    for item in resources:
        resource_type = str(item.get("type") or "").casefold()
        if resource_type not in _TELEMETRY_TYPES:
            continue
        tags = item.get("tags")
        if not isinstance(tags, dict) or tags.get("purpose") != "agent-insights-quality":
            raise ContractError("Telemetry inventory contains a resource without owned tags")
        generation = str(tags.get("generation") or "")
        profile = str(tags.get("profile") or "")
        resource_id = str(item.get("id") or "")
        if (
            profile not in {"daily", "staging"}
            or not generation
            or not resource_id.startswith("/")
        ):
            raise ContractError("Telemetry inventory contains an invalid owned resource")
        telemetry.append(
            {
                "resource_id": resource_id,
                "resource_type": resource_type,
                "generation": generation,
                "profile": profile,
            }
        )
    for profile in ("daily", "staging"):
        for resource_type in _TELEMETRY_TYPES:
            matches = [
                item
                for item in telemetry
                if item["generation"] == active_resource_set
                and item["profile"] == profile
                and item["resource_type"] == resource_type
            ]
            if len(matches) != 1:
                raise ContractError("Active telemetry resource set is incomplete or ambiguous")
    retired = [
        item for item in telemetry if item["generation"] != active_resource_set
    ]
    grouped: dict[tuple[str, str], set[str]] = {}
    for item in retired:
        grouped.setdefault(
            (item["generation"], item["profile"]),
            set(),
        ).add(item["resource_type"])
    if require_complete_retired_pairs and any(
        types != _TELEMETRY_TYPES for types in grouped.values()
    ):
        raise ContractError("Retired telemetry resource pairs are incomplete")
    values = sorted(
        retired,
        key=lambda item: (
            item["generation"],
            item["profile"],
            item["resource_type"],
        ),
    )
    plan = {
        "schema_version": "1.0.0",
        "active_resource_set": active_resource_set,
        "resources": values,
    }
    plan["plan_hash"] = content_hash(plan)
    return plan


def _validate_plan_hash(plan: dict[str, Any]) -> None:
    expected = content_hash(
        {key: value for key, value in plan.items() if key != "plan_hash"}
    )
    if plan.get("plan_hash") != expected:
        raise ContractError("Telemetry cleanup plan hash does not match its content")


def _validate_final_inventory(
    resources: list[dict[str, Any]],
    active_resource_set: str,
) -> None:
    owned = [
        item
        for item in resources
        if isinstance(item.get("tags"), dict)
        and item["tags"].get("purpose") == "agent-insights-quality"
    ]
    counts: dict[str, int] = {}
    for item in owned:
        resource_type = str(item.get("type") or "").casefold()
        counts[resource_type] = counts.get(resource_type, 0) + 1
        if resource_type in _TELEMETRY_TYPES and item["tags"].get(
            "generation"
        ) != active_resource_set:
            raise ContractError("Superseded telemetry remains after cleanup")
    expected = {**_STABLE_COUNTS, _COMPONENT: 2, _WORKSPACE: 2}
    if counts != expected:
        raise ContractError("Post-cleanup resource inventory is not the fixed topology")


def _inventory() -> list[dict[str, Any]]:
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
        raise ContractError("Quality-test Resource Group inventory failed")
    value = json.loads(process.stdout)
    if not isinstance(value, list):
        raise ContractError("Quality-test Resource Group inventory is invalid")
    return [item for item in value if isinstance(item, dict)]


def _require_private_path(path: Path) -> None:
    private_root = runtime_root().resolve()
    try:
        path.resolve().relative_to(private_root)
    except ValueError as error:
        raise ContractError("Telemetry cleanup artifacts must remain private") from error
