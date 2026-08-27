from __future__ import annotations

from pathlib import Path

import pytest

from agent_insights_quality.automation_policy import (
    FIXED_TELEMETRY_RESOURCE_SET,
    load_automation_policy,
)
from agent_insights_quality.util import ROOT, ContractError


def test_repository_uses_fractional_fixed_telemetry_policy() -> None:
    policy = load_automation_policy()
    assert policy.insight_lookback_hours == 3.0
    assert policy.telemetry_resource_set == FIXED_TELEMETRY_RESOURCE_SET == "g29"
    assert policy.max_recovery_versions == 3
    assert policy.clean_window_max_wait_seconds >= 11430


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("insight_lookback_hours", '"0.1"', "numeric"),
        ("insight_lookback_hours", "0.09", "reviewed minimum"),
        ("max_recovery_versions", "4", "reviewed maximum"),
        ("telemetry_resource_set", "g30", "fixed reviewed set"),
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
