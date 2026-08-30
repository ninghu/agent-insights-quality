from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json

import pytest

from agent_insights_quality.catalogs import (
    MODEL_MEDIATED_ISSUES,
    _activation_contract_digest,
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
from agent_insights_quality.validation_rules import validation_matrix


def test_source_integrity_binds_activation_assertion_definitions(tmp_path) -> None:
    traffic = {
        "requests": [
            {
                "id": "synthetic-request",
                "expected": {
                    "activation_gate": True,
                    "trace_assertions": [
                        {
                            "name": "one_tool_call",
                            "kind": "tool_call_count",
                            "tool_name": "synthetic_tool",
                            "count": 1,
                        }
                    ],
                },
            }
        ]
    }
    (tmp_path / "traffic.json").write_text(
        json.dumps(traffic),
        encoding="utf-8",
    )
    first = _activation_contract_digest(tmp_path)
    traffic["requests"][0]["expected"]["trace_assertions"][0]["count"] = 2
    (tmp_path / "traffic.json").write_text(
        json.dumps(traffic),
        encoding="utf-8",
    )
    assert _activation_contract_digest(tmp_path) != first


def test_catalogs_define_fixed_inventory() -> None:
    agents, issues = load_catalogs()
    assert len(agents["agents"]) == 5
    assert len(issues["issues"]) == 36
    assert issues["selection"]["issues_per_agent_daily"] == 4
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
    assert {
        item["name"]: item["baseline_contract"]["trace_operations"]
        for item in agents["agents"]
    } == {
        "weather-agent": "uniform",
        "healthcare-agent": "uniform",
        "finance-agent": "uniform",
        "travel-agent": "uniform",
        "support-ticket-agent": "required_per_request",
    }
    support_traffic = json.loads(
        (
            ROOT
            / "agents"
            / "support-ticket-agent"
            / "v0"
            / "traffic.json"
        ).read_text(encoding="utf-8")
    )
    assert [
        request["expected"]["required_operations"]
        for request in support_traffic["requests"]
    ] == [
        ["invoke_agent", "execute_tool", "chat"],
        ["invoke_agent", "execute_tool", "chat"],
        ["invoke_agent", "execute_tool", "chat"],
        ["invoke_agent", "execute_tool", "chat"],
        ["invoke_agent", "execute_tool"],
    ]
    assert len(issues["source_delta_contracts"]) == 36
    assert {
        item["id"]
        for item in issues["issues"]
        if item["validation_mode"] == "model_mediated"
    } == MODEL_MEDIATED_ISSUES
    assert all(
        item["baseline_contract"]["validation_mode"] == "baseline"
        for item in agents["agents"]
    )
    assert "Daily qualification rotates 4 issues per Agent" in render_issue_catalog(
        issues
    )


def test_all_authorities_have_fixed_versioned_validation_rules() -> None:
    agents, issues = load_catalogs()
    issue_by_id = {item["id"]: item for item in issues["issues"]}
    seen = set()
    for agent in agents["agents"]:
        authorities = [
            (
                f"{agent['name']}/v0",
                agent["baseline_path"],
                "baseline",
            ),
            *[
                (
                    issue_id,
                    issue_by_id[issue_id]["implementation"],
                    issue_by_id[issue_id]["validation_mode"],
                )
                for issue_id in agent["issue_ids"]
            ],
        ]
        for authority_id, root, mode in authorities:
            traffic = json.loads(
                (ROOT / root / "traffic.json").read_text(encoding="utf-8")
            )
            rules = traffic["validation_rules"]
            scenario = rules["scenarios"][0]
            n, k = validation_matrix(mode)
            assert rules["schema_version"] == "1.0.0"
            assert rules["execution_digest"].startswith("sha256:")
            assert scenario["execution_digest"].startswith("sha256:")
            assert (scenario["n"], scenario["k"]) == (n, k)
            assert len(scenario["attempts"]) == n
            assert all(
                attempt["setup_steps"] and attempt["probe_steps"]
                for attempt in scenario["attempts"]
            )
            if mode == "baseline":
                assert scenario["v0_control_predicate"] is None
            else:
                assert scenario["v0_control_predicate"] == {
                    "kind": "zero_defect_observations"
                }
            seen.add(authority_id)
    assert seen == {
        *(f"{agent['name']}/v0" for agent in agents["agents"]),
        *(f"issue-{number:03d}" for number in range(1, 37)),
    }


def test_validation_mode_reclassification_is_rejected() -> None:
    agents, issues = load_catalogs(require_paths=False)
    tampered = deepcopy(issues)
    tampered["issues"][0]["validation_mode"] = "deterministic"
    with pytest.raises(ContractError, match="reviewed defect mechanism"):
        validate_semantics(agents, tampered, require_paths=False)


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


def test_prompt_json_assertions_are_evaluator_side_and_tool_free() -> None:
    traffic = json.loads(
        (
            ROOT / "agents" / "weather-agent" / "v0" / "traffic.json"
        ).read_text(encoding="utf-8")
    )
    _validate_prompt_traffic(
        traffic,
        "weather baseline",
        require_activation=False,
        require_all_assertions=True,
    )

    unsupported_text_format = deepcopy(traffic)
    unsupported_text_format["requests"][3]["request"]["body"]["text"] = {
        "format": {"type": "json_schema"}
    }
    with pytest.raises(ContractError, match="unsupported request-side text formatting"):
        _validate_prompt_traffic(
            unsupported_text_format,
            "weather baseline",
            require_activation=False,
            require_all_assertions=True,
        )

    non_object_exact_json = deepcopy(traffic)
    non_object_exact_json["requests"][3]["expected"]["semantic_assertions"][
        "exact_json"
    ] = ["clear", 21, "celsius"]
    with pytest.raises(ContractError, match="schema error"):
        _validate_prompt_traffic(
            non_object_exact_json,
            "weather baseline",
            require_activation=False,
            require_all_assertions=True,
        )

    tool_configured = deepcopy(traffic)
    tool_configured["requests"][3]["request"]["body"]["tools"] = []
    with pytest.raises(ContractError, match="cannot contain tool configuration"):
        _validate_prompt_traffic(
            tool_configured,
            "weather baseline",
            require_activation=False,
            require_all_assertions=True,
        )


def test_prompt_activation_output_is_not_constrained_by_request_schema() -> None:
    for agent_name in ("weather-agent", "healthcare-agent"):
        for traffic_path in sorted(
            (ROOT / "agents" / agent_name / "issues").glob("issue-*/traffic.json")
        ):
            issue_traffic = json.loads(traffic_path.read_text(encoding="utf-8"))
            for request in issue_traffic["requests"]:
                if request["expected"].get("activation_gate") is True:
                    assert "text" not in request["request"]["body"]

    traffic = json.loads(
        (
            ROOT
            / "agents"
            / "weather-agent"
            / "issues"
            / "issue-001"
            / "traffic.json"
        ).read_text(encoding="utf-8")
    )
    activation = traffic["requests"][0]
    assertions = activation["expected"]["semantic_assertions"]
    healthy_output = {
        "condition": "unknown",
        "evidence": "missing",
        "status": "unavailable",
    }
    assert assertions["exact_json"] != healthy_output
    _validate_prompt_traffic(
        traffic,
        "weather issue activation",
        require_activation=True,
        require_all_assertions=True,
    )


def test_prompt_activation_rejects_unsupported_text_format() -> None:
    traffic = json.loads(
        (
            ROOT
            / "agents"
            / "weather-agent"
            / "issues"
            / "issue-001"
            / "traffic.json"
        ).read_text(encoding="utf-8")
    )
    activation = traffic["requests"][0]
    exact_json = activation["expected"]["semantic_assertions"]["exact_json"]
    activation["request"]["body"]["text"] = {
        "format": {
            "type": "json_schema",
            "name": "defect_forcing_response",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": list(exact_json),
                "properties": {
                    key: {"type": "string", "enum": [value]}
                    for key, value in exact_json.items()
                },
            },
        }
    }

    with pytest.raises(
        ContractError,
        match="unsupported request-side text formatting",
    ):
        _validate_prompt_traffic(
            traffic,
            "weather issue activation",
            require_activation=True,
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
        "second_turn_marked_observed",
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
        requests[0]["expected"]["semantic_assertions"]["question_only"] = False
    elif malformation == "second_turn_marked_observed":
        requests[1]["expected"]["defect_observed"] = True
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
