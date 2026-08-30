from __future__ import annotations

import pytest

from agent_insights_quality.generated_paths import validate_generated_paths
from agent_insights_quality.util import ContractError


def test_generated_paths_allow_only_reports() -> None:
    validate_generated_paths(
        [
            "reports/daily/2026/08/24/report.json",
            "reports/daily/2026/08/24/report.md",
            "reports/daily/2026/08/24/agents/weather-agent.md",
            "reports/daily/2026/08/24/insight-engine-improvement.json",
            "reports/daily/2026/08/24/insight-engine-improvement.md",
            "reports/insight-engine-improvement.json",
            "reports/insight-engine-improvement.md",
            "reports/latest.json",
        ]
    )
    with pytest.raises(ContractError, match="protected"):
        validate_generated_paths(["catalogs/ISSUE_CATALOG.yaml"])
