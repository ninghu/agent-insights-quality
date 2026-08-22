from __future__ import annotations

import pytest

from agent_insights_quality.contracts import ContractError, ROOT
from agent_insights_quality.generated_paths import (
    normalize_repo_path,
    path_is_allowed,
    validate_generated_paths,
)


ALLOWED = [
    "reports/daily/*/*/*/plan.json",
    "reports/daily/*/*/*/readiness-failure.json",
    "reports/daily/*/*/*/email-handoff.json",
    "reports/daily/*/*/*/daily-status.json",
    "reports/daily/*/*/*/aiq-*-r??/plan.json",
    "reports/daily/*/*/*/aiq-*-r??/daily-status.json",
    "reports/latest.json",
    "state/quality-memory.json",
]


@pytest.mark.parametrize(
    "path",
    [
        "reports/daily/2026/08/20/plan.json",
        "reports/daily/2026/08/20/readiness-failure.json",
        "reports/daily/2026/08/20/email-handoff.json",
        "reports/daily/2026/08/20/daily-status.json",
        "reports/daily/2026/08/20/aiq-20260820-r01/plan.json",
        "reports/daily/2026/08/20/aiq-20260820-r01/daily-status.json",
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
        "config/ado-policy.yaml",
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


def test_generated_path_rejects_literal_backslash_filename() -> None:
    disguised = "reports\\daily\\2026\\08\\20\\plan.json"
    with pytest.raises(ContractError, match="Unsafe repository path"):
        normalize_repo_path(disguised)
    with pytest.raises(ContractError, match="Unsafe repository path"):
        validate_generated_paths([disguised])


def test_generated_path_pattern_does_not_match_malformed_daily_layout() -> None:
    assert not path_is_allowed("reports/daily/2026/08/20/nested/plan.json", ALLOWED)
    assert not path_is_allowed(
        "reports/daily/2026/08/20/aiq-20260820-r1/plan.json",
        ALLOWED,
    )


def test_generated_change_cannot_modify_its_own_authority() -> None:
    protected_authority = [
        "config/automation-policy.yaml",
        "config/ado-policy.yaml",
        "config/reporting.yaml",
        "config/runtime-readiness.yaml",
        "src/agent_insights_quality/generated_paths.py",
        ".github/workflows/validate-generated-change.yml",
    ]
    with pytest.raises(ContractError, match="protected paths"):
        validate_generated_paths(protected_authority)


def test_workflow_uses_base_branch_validator_and_policy() -> None:
    workflow = (ROOT / ".github" / "workflows" / "validate-generated-change.yml").read_text(
        encoding="ascii"
    )
    assert "pull_request_target:" in workflow
    assert "pull_request:" not in workflow
    guard = workflow.split("- name: Enforce paths with trusted base code and policy", 1)[1]
    assert 'git worktree add --detach "${base_dir}" "${BASE_SHA}"' in guard
    assert '"${base_venv}/bin/python" -m pip install -e "${base_dir}"' in guard
    assert 'cd "${base_dir}"' in guard
    assert "validate-generated-paths" in guard
    assert '"--path=${first_path}"' in guard
    assert "--diff-filter=ACMRTD" in guard
    assert "actions/checkout@v4" in workflow
    assert "ref: ${{ github.event.pull_request.base.sha }}" in workflow
    assert "pip install -e ." not in guard
