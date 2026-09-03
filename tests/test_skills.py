from __future__ import annotations

import json

import yaml

from agent_insights_quality.util import ROOT


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_repository_skills_have_discoverable_frontmatter() -> None:
    paths = sorted((ROOT / ".github" / "skills").glob("*/SKILL.md"))
    assert len(paths) == 5
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
    assert ".github/skills/test-agent-validation/SKILL.md" in contributing
    for catalog in (
        "AGENT_CATALOG.md",
        "ISSUE_CATALOG.md",
        "catalogs/AGENT_CATALOG.yaml",
        "catalogs/ISSUE_CATALOG.yaml",
    ):
        assert catalog in contributing
    assert "Stop for human review" in contributing
    assert "all 41" in contributing
    assert "Test Agent Validation" in contributing
    assert "at least one reviewed single-root issue" in contributing
    for requirement in (
        "`v0` is a complete, deployable healthy version",
        "Every non-baseline logical version represents exactly one `issue-NNN`",
        "each issue is assigned exactly once to one permanent Test Agent",
        "exactly one independently fixable, reviewed root cause",
    ):
        assert requirement in normalized
    assert contributing.index("- Test Agent:") < contributing.index("- Issue:")
    assert contributing.index("## Onboard a new Test Agent") < contributing.index(
        "## Onboard a new issue"
    )
    for command in (
        'python -m pip install -e ".[dev]"',
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
        "min(4, assigned issues)",
        "deterministic packaging",
        "catalogs/AGENT_CATALOG.yaml",
        "previous Agent count, issue count, version count",
        "content and stale or missing evidence",
        "single create-once approved validation record",
        "Never use the preserved old West US 2 environment as a fallback",
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
        "daily_issue_count = sum(min(4, assigned_issue_count))",
        "implementation.yaml",
        "deterministic `package.py`",
        "privacy-safe trace proof",
        "baseline ownership",
        "fixed validation scenarios",
        "single approved validation record",
        "never use the preserved old West US 2 environment as a fallback",
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


def test_validation_skill_enforces_local_report_free_approval() -> None:
    skill = _text(".github/skills/test-agent-validation/SKILL.md")
    normalized = " ".join(skill.split())
    for requirement in (
        "report-free Sweden Central staging gate",
        "aiq-staging-swedencentral",
        "unique runtime Agent identity",
        "up to eight visible GPT-5.6 Sol evaluator sessions",
        "visible Copilot sub-sessions",
        "one to eight deterministic",
        "test-agent-validation-copilot-evaluation.schema.json",
        "status and next-action guidance",
        "server-assigned",
        "never select `latest`",
        "paired `v0`",
        "response-bound traces",
        "all 41 exact versions",
        "`SUPERSEDED`",
        "approve-test-agent-validation",
        "explicit approval",
    ):
        assert requirement in normalized
    staging = " ".join(
        _text(".github/skills/staging-qualification/SKILL.md").split()
    )
    for requirement in (
        "human review",
        "aiq-staging-swedencentral",
        "all 41 unique catalog authorities",
        "immutable disjoint deployment assignments",
        "at most eight",
        "visible Copilot coordinator",
        "one to eight deterministic",
        "server-assigned versions",
        "no command floats `latest`",
        "Retain sessions",
        "send Daily smoke traffic",
    ):
        assert requirement in staging


def test_active_contract_docs_exclude_superseded_environment_rules() -> None:
    paths = [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / "docs" / "OPERATIONS.md",
        ROOT / "docs" / "AUTOMATION_SETUP.md",
        ROOT / "docs" / "FRAMEWORK_OVERVIEW.md",
        ROOT / ".github" / "skills" / "test-agent-validation" / "SKILL.md",
        ROOT / ".github" / "skills" / "agent-insights-quality-daily" / "SKILL.md",
        ROOT / ".github" / "copilot" / "test-agent-validation-prompt.md",
        ROOT / ".github" / "github-app.yml",
        ROOT / "catalogs" / "AGENT_CATALOG.yaml",
        ROOT / "config" / "test-agent-validation.yaml",
        ROOT / "schemas" / "agent-catalog.schema.json",
        ROOT / "schemas" / "deployment-registry.schema.json",
        ROOT / "schemas" / "test-agent-validation-lifecycle.schema.json",
        ROOT / "schemas" / "test-agent-validation-evidence.schema.json",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    folded = " ".join(text.casefold().split())
    for stale in (
        "agent-insights-quality-staging",
        "one opaque temporary project",
        "ephemeral project",
        "wait the reviewed clean interval",
        "wait a second clean interval",
        "latest gpt-5.6",
        "aiq-validation-",
        "`r03` is the final staging run",
        "create `r04`",
        "must not be invoked after r03",
    ):
        assert stale not in folded
    for required in (
        "aiq-staging-swedencentral",
        "aiq-daily-swedencentral",
        "swedencentral-g30",
        "server-assigned",
        "run-scoped",
        "noautoupgrade",
    ):
        assert required in folded


def test_daily_skill_publishes_adx_without_blocking_email() -> None:
    skill = _text(".github/skills/agent-insights-quality-daily/SKILL.md")
    normalized = " ".join(skill.split())
    for requirement in (
        "public-safe daily ADX publication",
        "full reasoning already present in the committed sanitized report",
        "never private assessment packages",
        "work-item context",
        "quality-trend dashboard link",
        "continue the email and pull-request flow",
        "email warning",
        "pull-request description",
        "ADX is the only authorized analytics write",
        "Application Insights remains read-only",
        "Never write telemetry",
        "one focused GPT-5.6 Sol recheck",
        "Never send new traffic for this recheck",
        "never force a conclusive verdict",
        "`--test-run`",
        "nonzero `--rerun N`",
        "must not contact ADX",
        "create a pull request",
        "Scheduled official runs never pass `--test-run`",
    ):
        assert requirement in normalized
    readiness = _text(".github/copilot/daily-readiness-prompt.md")
    for requirement in (
        "unique ADX quality-cluster resolution",
        "all seven logical quality views",
        "Viewer/Ingestor principal assignments",
        "reviewed `https://aka.ms/agent-insights/quality` short link",
        "without writing data",
    ):
        assert requirement in " ".join(readiness.split())
