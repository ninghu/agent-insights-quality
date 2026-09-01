from __future__ import annotations

from agent_insights_quality.util import ROOT


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_validation_docs_preserve_report_free_boundary_and_fixed_matrix() -> None:
    combined = "\n".join(
        _text(path)
        for path in (
            "README.md",
            "AGENTS.md",
            "CONTRIBUTING.md",
            "docs/OPERATIONS.md",
            "docs/FRAMEWORK_OVERVIEW.md",
        )
    )
    for requirement in (
        "41 independent",
        "5/5",
        "5/7",
        "paired `v0`",
        "report-free",
        "CLEAN",
        "OS file lock",
        "explicit human approval",
    ):
        assert requirement.casefold() in combined.casefold()
    assert "Validation never runs monitors, Agent Insights, assessment" in combined


def test_cutover_docs_are_new_only_fail_forward_and_keep_daily_test_external() -> None:
    operations = _text("docs/OPERATIONS.md")
    normalized = " ".join(operations.split())
    for requirement in (
        "new-only",
        "no process may read both",
        "quiescence lock",
        "remain new-only on failure",
        "read-only readiness",
        "isolated `--test-run --rerun N` email-only Daily Test",
        "external, non-gating",
        "old environment is never a staging fallback",
        "approved-record Blob path",
    ):
        assert requirement in normalized


def test_staging_skill_routes_current_durable_sweden_qualification() -> None:
    staging = _text(".github/skills/staging-qualification/SKILL.md")
    assert "official human-reviewed gate" in staging
    assert "aiq-staging-swedencentral" in staging
    assert "Sweden Central `g30`" in staging
    assert "Never create or delete the Project" in staging
    assert ".github/skills/test-agent-validation/SKILL.md" in staging
    assert "`r03`" not in staging
    assert "`r04`" not in staging
