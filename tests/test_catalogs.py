from __future__ import annotations

from collections import Counter
from copy import deepcopy

import pytest

from agent_insights_quality.catalogs import (
    _validate_prompt_definition,
    _validate_prompt_traffic,
    catalog_hashes,
    load_catalogs,
    render_agent_catalog,
    render_issue_catalog,
    validate_semantics,
)
from agent_insights_quality.util import ContractError


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
    assert {item["name"]: item["owner"] for item in agents["agents"]} == {
        "weather-agent": "Billy Hu",
        "healthcare-agent": "Ilya Matiach",
        "finance-agent": "Han Che",
        "travel-agent": "Sean Gayler",
        "support-ticket-agent": "Nishal Dsilva",
    }
    assert set(catalog_hashes(agents, issues)) == {"agents", "issues", "artifacts"}
    assert {
        item["name"]: item["baseline_contract"]["function_calling"]
        for item in agents["agents"]
        if item["type"] == "prompt"
    } == {
        "weather-agent": "forbidden",
        "healthcare-agent": "forbidden",
    }
    assert len(issues["source_delta_contracts"]) == 36


def test_catalog_semantics_reject_old_source_delta_shape_cleanly() -> None:
    agents, issues = load_catalogs(require_paths=False)
    historical = deepcopy(issues)
    historical.pop("source_delta_contracts")
    with pytest.raises(ContractError, match="source delta contracts"):
        validate_semantics(agents, historical, require_paths=False)


def test_prompt_contract_rejects_tools_and_tool_fixtures() -> None:
    with pytest.raises(ContractError, match="cannot contain tools"):
        _validate_prompt_definition(
            {
                "name": "weather-agent",
                "definition": {
                    "kind": "prompt",
                    "model": "gpt-5.4-mini",
                    "instructions": "Synthetic instructions.",
                    "tools": [],
                },
                "metadata": {"logical_version": "v0"},
            },
            "synthetic definition",
        )
    with pytest.raises(ContractError, match="cannot contain tool fixtures"):
        _validate_prompt_traffic(
            {
                "agent_name": "weather-agent",
                "logical_version": "v0",
                "requests": [
                    {
                        "id": f"request-{index}",
                        "request": {
                            "method": "POST",
                            "path": "/responses",
                            "body": {"input": "Synthetic evidence."},
                        },
                        "expected": {
                            "semantic_assertions": {
                                "required_claims": ["synthetic"]
                            }
                        },
                        "tool_fixtures": [],
                    }
                    for index in range(5)
                ],
            },
            "synthetic traffic",
            require_activation=False,
            require_all_assertions=True,
        )


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
