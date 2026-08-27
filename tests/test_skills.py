from __future__ import annotations

import json

import yaml

from agent_insights_quality.util import ROOT


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_repository_skills_have_discoverable_frontmatter() -> None:
    paths = sorted((ROOT / ".github" / "skills").glob("*/SKILL.md"))
    assert len(paths) == 4
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        _, frontmatter, body = text.split("---", 2)
        value = yaml.safe_load(frontmatter)
        assert value["name"] == path.parent.name
        assert value["description"].strip()
        assert value["license"] == "MIT"
        assert body.strip().startswith("# ")


def test_contributing_routes_onboarding_through_versioned_skills() -> None:
    contributing = _text("CONTRIBUTING.md")
    normalized = " ".join(contributing.split())
    assert ".github/skills/onboard-new-issue/SKILL.md" in contributing
    assert ".github/skills/onboard-test-agent/SKILL.md" in contributing
    for catalog in (
        "AGENT_CATALOG.md",
        "ISSUE_CATALOG.md",
        "catalogs/AGENT_CATALOG.yaml",
        "catalogs/ISSUE_CATALOG.yaml",
    ):
        assert catalog in contributing
    assert "Stop for human review" in contributing
    assert "full-catalog" in contributing
    assert "staging qualification" in contributing
    assert "at least one reviewed single-root issue" in contributing
    for requirement in (
        "`v0` is a complete, deployable healthy version",
        "Every non-baseline logical version represents exactly one `issue-NNN`",
        "each issue is assigned exactly once to one permanent Test Agent",
        "exactly one independently fixable, reviewed root cause",
    ):
        assert requirement in normalized
    assert contributing.index("- New Test Agent:") < contributing.index("- New issue:")
    assert contributing.index("## Onboard a new Test Agent") < contributing.index(
        "## Define a new issue"
    )
    for command in (
        "python -m agent_insights_quality generate-docs",
        "python -m agent_insights_quality validate",
        "python -m ruff check .",
        "python -m pytest",
        r"az bicep build --file infra\main.bicep --stdout",
    ):
        assert command in contributing


def test_new_issue_skill_covers_complete_reviewed_contract() -> None:
    skill = _text(".github/skills/onboard-new-issue/SKILL.md")
    normalized = " ".join(skill.split())
    for requirement in (
        "reviewable plan",
        "human approval",
        "title, root cause, category, severity, proposed fix",
        "complete deployable `definition.json`",
        "complete healthy `source/` tree",
        "differs by exactly one",
        "baseline maintenance",
        "min(5, assigned issues)",
        "enclosing new-Agent migration",
        "deterministic packaging",
        "catalogs/AGENT_CATALOG.yaml",
        "previous Agent count, issue count, version count",
        "affected Agent",
        "valid receipts for unchanged Agents",
        "human review before daily promotion",
        "Never promote or reuse INCOMPLETE",
    ):
        assert requirement in normalized


def test_onboard_test_agent_skill_covers_topology_and_baseline_safety() -> None:
    skill = _text(".github/skills/onboard-test-agent/SKILL.md")
    normalized = " ".join(skill.split())
    for requirement in (
        "reviewable plan",
        "human approval",
        "maintenance owner",
        "authorized maintainer",
        "at least five deterministic healthy traffic",
        "at least one independently fixable, single-root issue",
        ".github/skills/onboard-new-issue/SKILL.md",
        "complete `definition.json`",
        "`source/` tree containing only its defect",
        "update every fixed-count contract",
        "daily_issue_count = sum(min(5, assigned_issue_count))",
        "implementation.yaml",
        "deterministic `package.py`",
        "privacy-safe trace proof",
        "baseline ownership",
        "qualify only the new Agent",
        "human review",
        "Never promote or reuse INCOMPLETE",
    ):
        assert requirement in normalized


def test_onboarding_schema_minimums_match_one_issue_contract() -> None:
    agent_schema = json.loads(
        (ROOT / "schemas" / "agent-catalog.schema.json").read_text(
            encoding="utf-8"
        )
    )
    agents = agent_schema["properties"]["agents"]
    issue_ids = agents["items"]["properties"]["issue_ids"]
    registry_schema = json.loads(
        (ROOT / "schemas" / "deployment-registry.schema.json").read_text(
            encoding="utf-8"
        )
    )
    versions = (
        registry_schema["properties"]["agents"]["additionalProperties"][
            "properties"
        ]["versions"]
    )
    assert agents["minItems"] == agents["maxItems"] == 5
    assert issue_ids["minItems"] == 1
    assert versions["minProperties"] == 2


def test_staging_skill_uses_impact_based_qualification() -> None:
    skill = _text(".github/skills/staging-qualification/SKILL.md")
    normalized = " ".join(skill.split())
    for requirement in (
        "impact-based qualification",
        "qualify each affected Agent's `v0` and all assigned issues",
        "reuse reviewed evidence for unchanged Agents",
        "full-catalog qualification for shared runtime",
        "compose promotion",
        "Never promote or reuse `INCOMPLETE`",
        "do not send daily smoke traffic",
        "use full-catalog qualification",
        "never splice evidence or receipts manually",
    ):
        assert requirement in normalized
