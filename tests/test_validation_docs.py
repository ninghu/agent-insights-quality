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
        "6/10",
        "6/10",
        "paired `v0`",
        "report-free",
        "READY",
        "OS file lock",
        "advisory",
    ):
        assert requirement.casefold() in combined.casefold()
    assert "Validation never runs monitors, Agent Insights, assessment" in combined


def test_cutover_docs_are_new_only_and_keep_daily_test_external() -> None:
    operations = _text("docs/OPERATIONS.md")
    normalized = " ".join(operations.split())
    for requirement in (
        "new-only",
        "read-only readiness",
        "old environment is never a staging fallback",
    ):
        assert requirement in normalized


def test_staging_skill_routes_current_durable_sweden_qualification() -> None:
    staging = _text(".github/skills/staging-qualification/SKILL.md")
    assert "human review" in staging
    assert "aiq-staging-swedencentral" in staging
    assert "immutable disjoint deployment assignments" in staging
    assert "at most eight" in staging
    assert "visible Copilot sub-sessions" in staging
    assert "one to eight deterministic" in staging
    assert "Sweden Central `g30`" in staging
    assert "Never create or delete the Project" in staging
    assert ".github/skills/test-agent-validation/SKILL.md" in staging
    assert "`r03`" not in staging
    assert "`r04`" not in staging


def test_active_docs_require_dedicated_sweden_storage_without_legacy_fallback() -> None:
    combined = "\n".join(
        _text(path)
        for path in (
            "README.md",
            "AGENTS.md",
            "docs/AUTOMATION_SETUP.md",
            "docs/OPERATIONS.md",
            ".github/skills/staging-qualification/SKILL.md",
        )
    )
    normalized = " ".join(combined.split()).casefold()
    for requirement in (
        "dedicated sweden",
        "deployment-registries",
        "quality-artifacts",
        "legacy storage",
        "never modified",
    ):
        assert requirement in normalized
    assert "existing shared acr and blob storage" not in normalized
    assert "shared acr/storage" not in normalized
