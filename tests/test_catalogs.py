from __future__ import annotations

from collections import Counter

from agent_insights_quality.catalogs import (
    catalog_hashes,
    load_catalogs,
    render_agent_catalog,
    render_issue_catalog,
)


def test_catalogs_define_fixed_inventory() -> None:
    agents, issues = load_catalogs()
    assert len(agents["agents"]) == 5
    assert len(issues["issues"]) == 36
    assert [item["id"] for item in issues["issues"]] == [
        f"issue-{number:03d}" for number in range(1, 37)
    ]
    assert Counter(item["agent"] for item in issues["issues"]) == {
        "weather-agent": 6,
        "healthcare-agent": 6,
        "finance-agent": 8,
        "travel-agent": 8,
        "support-ticket-agent": 8,
    }
    assert set(catalog_hashes(agents, issues)) == {"agents", "issues", "artifacts"}


def test_generated_catalog_views_are_complete() -> None:
    agents, issues = load_catalogs()
    agent_doc = render_agent_catalog(agents)
    issue_doc = render_issue_catalog(issues)
    assert len(
        [line for line in agent_doc.splitlines() if line.startswith("| `")]
    ) == 5
    assert len(
        [
            line
            for line in issue_doc.splitlines()
            if line.startswith('| <a id="issue-')
        ]
    ) == 36
