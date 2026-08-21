from __future__ import annotations

import pytest

from agent_insights_quality.contracts import ContractError
from agent_insights_quality.generated_paths import (
    normalize_repo_path,
    path_is_allowed,
    validate_generated_paths,
)


ALLOWED = ["reports/daily/**", "reports/latest.json", "state/quality-memory.json"]


@pytest.mark.parametrize(
    "path",
    [
        "reports/daily/2026/08/20/plan.json",
        "reports/latest.json",
        "state/quality-memory.json",
    ],
)
def test_generated_path_allowlist_accepts_daily_outputs(path: str) -> None:
    assert path_is_allowed(path, ALLOWED)


@pytest.mark.parametrize(
    "path",
    [
        "config/reporting.yaml",
        "agents/weather-prompt/manifest.yaml",
        ".github/copilot/daily-bootstrap-prompt.md",
        "schemas/judgment.schema.json",
    ],
)
def test_generated_path_allowlist_rejects_contract_changes(path: str) -> None:
    assert not path_is_allowed(path, ALLOWED)
    with pytest.raises(ContractError, match="protected paths"):
        validate_generated_paths([path])


def test_generated_path_rejects_traversal() -> None:
    with pytest.raises(ContractError, match="Unsafe repository path"):
        normalize_repo_path("../config/reporting.yaml")
