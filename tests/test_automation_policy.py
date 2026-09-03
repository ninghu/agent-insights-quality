from __future__ import annotations

from pathlib import Path

import pytest

from agent_insights_quality.automation_policy import (
    FIXED_DEPLOYMENT_REGISTRY_CONTAINER,
    FIXED_QUALITY_ARTIFACT_CONTAINER,
    FIXED_STORAGE_ACCOUNT_PREFIX,
    FIXED_STORAGE_RESOURCE_ROLE,
    FIXED_TELEMETRY_RESOURCE_SET,
    load_automation_policy,
)
from agent_insights_quality.util import ROOT, ContractError


def test_repository_uses_fractional_fixed_telemetry_policy() -> None:
    policy = load_automation_policy()
    assert policy.issues_per_agent_daily == 4
    assert policy.max_parallel_agents == 5
    assert policy.insight_lookback_hours == 0.1
    assert policy.telemetry_resource_set == FIXED_TELEMETRY_RESOURCE_SET == "g30"
    assert (
        policy.approved_record_container
        == "test-agent-validation-approved-records-swedencentral-g30"
    )
    assert policy.storage_account_prefix == FIXED_STORAGE_ACCOUNT_PREFIX == "aiqsweart"
    assert (
        policy.storage_resource_role
        == FIXED_STORAGE_RESOURCE_ROLE
        == "qualification-storage"
    )
    assert (
        policy.quality_artifact_container
        == FIXED_QUALITY_ARTIFACT_CONTAINER
        == "quality-artifacts"
    )
    assert (
        policy.deployment_registry_container
        == FIXED_DEPLOYMENT_REGISTRY_CONTAINER
        == "deployment-registries"
    )
    assert policy.max_recovery_versions == 3
    assert policy.agent_start_stagger_seconds == 5
    assert policy.clean_window_max_wait_seconds >= 990
    assert policy.trace_assertion_stabilization_seconds == 180
    assert policy.insight_start_margin_seconds == 30


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("insight_lookback_hours", '"0.1"', "numeric"),
        ("insight_lookback_hours", "0.09", "reviewed minimum"),
        ("issues_per_agent_daily", "5", "daily issue count"),
        ("max_parallel_agents", "6", "fixed inventory"),
        ("max_recovery_versions", "4", "reviewed maximum"),
        ("agent_start_stagger_seconds", "31", "unreasonably long"),
        (
            "trace_assertion_stabilization_seconds",
            "45",
            "reviewed ingestion margin",
        ),
        (
            "trace_assertion_stabilization_seconds",
            "900",
            "bounded deadline",
        ),
        ("insight_start_margin_seconds", "345", "within lookback"),
        ("telemetry_resource_set", "g29", "fixed reviewed set"),
        ("storage_account_prefix", "aiqartifacts", "reviewed value"),
        ("storage_resource_role", "legacy-storage", "reviewed value"),
        ("quality_artifact_container", "legacy-artifacts", "not reviewed"),
        ("deployment_registry_container", "legacy-registry", "not reviewed"),
        (
            "approved_record_container",
            "test-agent-validation-approved-records",
            "reviewed environment namespace",
        ),
    ],
)
def test_automation_policy_rejects_unreviewed_values(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    source = (ROOT / "config" / "automation.yaml").read_text(encoding="utf-8")
    lines = [
        f"{field}: {value}" if line.startswith(f"{field}:") else line
        for line in source.splitlines()
    ]
    path = tmp_path / "automation.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ContractError, match=message):
        load_automation_policy(path)
