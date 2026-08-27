from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_insights_quality.telemetry_cleanup import (
    apply_cleanup_plan,
    build_cleanup_plan,
)
from agent_insights_quality.util import ContractError


def _resource(
    resource_type: str,
    generation: str,
    profile: str,
) -> dict:
    return {
        "id": f"/subscriptions/hidden/{generation}/{profile}/{resource_type}",
        "type": resource_type,
        "tags": {
            "purpose": "agent-insights-quality",
            "generation": generation,
            "profile": profile,
        },
    }


def _inventory() -> list[dict]:
    return [
        _resource(resource_type, generation, profile)
        for generation in ("g28", "g29")
        for profile in ("daily", "staging")
        for resource_type in (
            "Microsoft.Insights/components",
            "Microsoft.OperationalInsights/workspaces",
        )
    ]


def test_cleanup_plan_keeps_only_active_pairs() -> None:
    plan = build_cleanup_plan(_inventory(), "g29")
    assert len(plan["resources"]) == 4
    assert {item["generation"] for item in plan["resources"]} == {"g28"}
    assert plan["plan_hash"].startswith("sha256:")


def test_cleanup_plan_rejects_unowned_telemetry() -> None:
    inventory = _inventory()
    inventory[0]["tags"]["purpose"] = "unrelated"
    with pytest.raises(ContractError, match="without owned tags"):
        build_cleanup_plan(inventory, "g29")


def test_cleanup_apply_plan_allows_reviewed_half_pair_after_partial_delete() -> None:
    inventory = _inventory()
    inventory = [
        item
        for item in inventory
        if not (
            item["tags"]["generation"] == "g28"
            and item["tags"]["profile"] == "daily"
            and item["type"] == "Microsoft.Insights/components"
        )
    ]
    plan = build_cleanup_plan(
        inventory,
        "g29",
        require_complete_retired_pairs=False,
    )
    assert len(plan["resources"]) == 3


def test_cleanup_apply_is_idempotent_after_reviewed_resources_are_absent(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = tmp_path / ".aiq-runtime" / "test"
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", str(runtime))
    plan_path = runtime / "telemetry-cleanup" / "plan.json"
    receipt_path = runtime / "telemetry-cleanup" / "receipt.json"
    plan_path.parent.mkdir(parents=True)
    plan = build_cleanup_plan(_inventory(), "g29")
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    stable = [
        {
            "id": f"/subscriptions/hidden/stable/{index}",
            "type": resource_type,
            "tags": {"purpose": "agent-insights-quality"},
        }
        for resource_type, count in (
            ("Microsoft.CognitiveServices/accounts", 2),
            ("Microsoft.CognitiveServices/accounts/projects", 2),
            ("Microsoft.ContainerRegistry/registries", 1),
            ("Microsoft.Storage/storageAccounts", 1),
        )
        for index in range(count)
    ]
    active = [
        item for item in _inventory() if item["tags"]["generation"] == "g29"
    ]
    monkeypatch.setattr(
        "agent_insights_quality.telemetry_cleanup.load_automation_policy",
        lambda: SimpleNamespace(telemetry_resource_set="g29"),
    )
    monkeypatch.setattr(
        "agent_insights_quality.telemetry_cleanup.RuntimeProfile.from_env",
        lambda *_args: SimpleNamespace(assert_insights_connection=lambda: None),
    )
    monkeypatch.setattr(
        "agent_insights_quality.telemetry_cleanup._inventory",
        lambda: [*active, *stable],
    )
    receipt = apply_cleanup_plan(plan_path, receipt_path)
    assert receipt["deleted_resource_count"] == 0
    assert receipt["already_absent_resource_count"] == 4
    assert receipt_path.exists()
