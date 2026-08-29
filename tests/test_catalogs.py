from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json

import pytest

from agent_insights_quality.catalogs import (
    _validate_prompt_definition,
    _validate_prompt_issue_delta,
    _validate_prompt_traffic,
    _validate_weather_latency_traffic,
    catalog_hashes,
    load_catalogs,
    render_agent_catalog,
    render_issue_catalog,
    validate_semantics,
)
from agent_insights_quality.util import ROOT, ContractError


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
    assert Counter(item["category"] for item in issues["issues"]) == {
        "output_quality": 6,
        "hallucinations": 6,
        "safety_guardrails": 5,
        "tool_call_failures": 5,
        "reliability_errors": 4,
        "latency": 4,
        "context_memory": 3,
        "cost_tokens": 3,
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


def test_catalog_rejects_reclassified_fixed_agent() -> None:
    agents, issues = load_catalogs(require_paths=False)
    tampered = deepcopy(agents)
    finance = next(
        item for item in tampered["agents"] if item["name"] == "finance-agent"
    )
    finance["type"] = "hosted_custom_container"
    finance["framework"] = "custom_responses"
    with pytest.raises(ContractError, match="type and framework"):
        validate_semantics(tampered, issues, require_paths=False)


def _valid_prompt_definition() -> dict:
    return {
        "name": "weather-agent",
        "description": "Synthetic public-safe quality agent.",
        "definition": {
            "kind": "prompt",
            "model": "gpt-5.4-mini",
            "instructions": "Synthetic instructions.",
        },
        "metadata": {
            "traffic_class": "synthetic",
            "data_class": "public-safe",
            "logical_version": "v0",
            "conversation_memory": "responses_conversation",
            "max_output_tokens": "600",
        },
    }


def test_prompt_contract_accepts_pure_prompt_definition() -> None:
    _validate_prompt_definition(_valid_prompt_definition(), "synthetic definition")


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "tools",
        "tool",
        "tool_choice",
        "tool_config",
        "tool_configs",
        "tool_fixtures",
        "tool_resources",
        "functions",
        "function_call",
        "parallel_tool_calls",
    ],
)
@pytest.mark.parametrize("nested", [False, True], ids=["direct", "nested"])
def test_prompt_contract_rejects_tool_and_function_configuration(
    forbidden_key: str,
    nested: bool,
) -> None:
    value = _valid_prompt_definition()
    if nested:
        value["metadata"]["unreviewed"] = {"configuration": {forbidden_key: []}}
    else:
        value["definition"][forbidden_key] = []

    with pytest.raises(
        ContractError,
        match="cannot contain tool or function-calling configuration",
    ):
        _validate_prompt_definition(
            value,
            "synthetic definition",
        )


@pytest.mark.parametrize(
    ("location", "extra"),
    [
        ("root", {"unreviewed": "value"}),
        ("definition", {"unreviewed": "value"}),
        ("metadata", {"unreviewed": "value"}),
    ],
)
def test_prompt_contract_rejects_unreviewed_properties(
    location: str,
    extra: dict,
) -> None:
    value = _valid_prompt_definition()
    target = value if location == "root" else value[location]
    target.update(extra)

    with pytest.raises(ContractError, match="Additional properties are not allowed"):
        _validate_prompt_definition(value, "synthetic definition")


def test_prompt_source_delta_requires_exact_json_types() -> None:
    baseline = {
        "name": "weather-agent",
        "definition": {
            "kind": "prompt",
            "model": "gpt-5.4-mini",
            "instructions": "Healthy instructions.",
        },
        "metadata": {
            "logical_version": "v0",
            "typed_marker": True,
        },
    }
    issue = deepcopy(baseline)
    issue["definition"]["instructions"] += "\nOne defect."
    issue["metadata"]["logical_version"] = "issue-001"
    issue["metadata"]["typed_marker"] = 1
    with pytest.raises(ContractError, match="outside the reviewed defect"):
        _validate_prompt_issue_delta(baseline, issue, "issue-001")
    with pytest.raises(ContractError, match="tool configuration"):
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


def _weather_latency_traffic() -> dict:
    return json.loads(
        (
            ROOT
            / "agents"
            / "weather-agent"
            / "issues"
            / "issue-005"
            / "traffic.json"
        ).read_text(encoding="utf-8")
    )


def test_weather_latency_contract_accepts_five_ordered_gate_pairs() -> None:
    _validate_weather_latency_traffic(_weather_latency_traffic()["requests"])


@pytest.mark.parametrize(
    "malformation",
    [
        "all_true",
        "swapped",
        "missing",
        "malformed_gate_pair",
        "wrong_conversation",
        "wrong_request_id",
        "missing_delay_manifestation",
        "incorrect_completion",
    ],
)
def test_weather_latency_contract_rejects_malformed_gate_pairs(
    malformation: str,
) -> None:
    requests = deepcopy(_weather_latency_traffic()["requests"])
    if malformation == "all_true":
        for request in requests:
            request["expected"]["activation_gate"] = True
    elif malformation == "swapped":
        requests[0], requests[1] = requests[1], requests[0]
    elif malformation == "missing":
        requests.pop()
    elif malformation == "malformed_gate_pair":
        requests[1]["expected"].pop("activation_gate")
    elif malformation == "wrong_conversation":
        requests[1]["request"]["body"]["conversation"]["id"] = "unpaired"
    elif malformation == "wrong_request_id":
        requests[0]["id"] = "issue-005-request-2"
    elif malformation == "missing_delay_manifestation":
        requests[0]["expected"]["semantic_assertions"]["exact_text"] = (
            "Would you like me to ignore the complete weather evidence?"
        )
    else:
        requests[1]["expected"]["semantic_assertions"]["exact_json"]["completed"] = (
            False
        )

    with pytest.raises(ContractError, match="five ordered two-turn conversations"):
        _validate_weather_latency_traffic(requests)


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
