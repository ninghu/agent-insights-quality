from __future__ import annotations

from collections import Counter
from copy import deepcopy

import pytest

from agent_insights_quality.catalogs import (
    _validate_prompt_definition,
    _validate_prompt_issue_delta,
    _validate_prompt_traffic,
    catalog_hashes,
    load_catalogs,
    render_agent_catalog,
    render_issue_catalog,
    validate_semantics,
)
from agent_insights_quality.util import ROOT, ContractError, read_json


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


def test_catalog_semantics_bind_each_fixed_agent_contract() -> None:
    agents, issues = load_catalogs(require_paths=False)
    reclassified = deepcopy(agents)
    finance = next(
        item for item in reclassified["agents"] if item["name"] == "finance-agent"
    )
    finance["type"] = "hosted_custom_container"
    with pytest.raises(ContractError, match="type, framework, or baseline contract"):
        validate_semantics(reclassified, issues, require_paths=False)


@pytest.mark.parametrize(
    ("baseline_value", "issue_value"),
    [(True, 1), (1, 1.0)],
)
def test_prompt_delta_equality_distinguishes_json_types(
    baseline_value: object,
    issue_value: object,
) -> None:
    baseline = {
        "definition": {"instructions": "Healthy synthetic instructions."},
        "metadata": {"logical_version": "v0"},
        "typed_value": baseline_value,
    }
    issue = deepcopy(baseline)
    issue["definition"]["instructions"] += "\nInject one synthetic defect."
    issue["metadata"]["logical_version"] = "issue-001"
    issue["typed_value"] = issue_value
    with pytest.raises(ContractError, match="differs outside"):
        _validate_prompt_issue_delta(baseline, issue, "issue-001")


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_output_tokens", 0),
        ("max_output_tokens", 4097),
        ("max_output_tokens", True),
        ("max_output_tokens", "400"),
    ],
)
def test_prompt_traffic_closes_and_bounds_request_body(
    field: str,
    value: object,
) -> None:
    traffic = read_json(ROOT / "agents" / "weather-agent" / "v0" / "traffic.json")
    traffic["requests"][0]["request"]["body"][field] = value
    with pytest.raises(ContractError, match="schema error"):
        _validate_prompt_traffic(
            traffic,
            "synthetic traffic",
            require_activation=False,
            require_all_assertions=True,
        )


@pytest.mark.parametrize("key", ["tools", "tool_choice", "tool_configuration"])
def test_prompt_traffic_forbids_nested_tool_configuration(key: str) -> None:
    traffic = read_json(ROOT / "agents" / "weather-agent" / "v0" / "traffic.json")
    traffic["requests"][0]["request"]["body"][key] = []
    with pytest.raises(ContractError, match="cannot contain tool configuration"):
        _validate_prompt_traffic(
            traffic,
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
