from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from agent_insights_quality.util import (
    ROOT,
    ContractError,
    atomic_text,
    content_hash,
    json_values_equal,
    read_json,
)

VALIDATION_RULES_VERSION = "1.0.0"
VALIDATION_MODES = ("baseline", "deterministic", "model_mediated")
VALIDATION_MATRICES = {
    "baseline": (5, 5),
    "deterministic": (5, 5),
    "model_mediated": (7, 5),
}
CONVERSATION_PLACEHOLDER = "$validation_conversation"
RUNTIME_AGENT_NAME_PLACEHOLDER = "$runtime_agent_name"
RUNTIME_AGENT_VERSION_PLACEHOLDER = "$runtime_agent_version"
VALIDATION_RULES_SCHEMA_PATH = (
    ROOT / "schemas" / "test-agent-validation-rules.schema.json"
)

_FORBIDDEN_SELECTOR_KEYS = {
    "defect_selector",
    "issue_selector",
    "runtime_injection",
    "validation_mode",
}

_TRACE_OBSERVED_ISSUES = {
    *(f"issue-{number:03d}" for number in range(13, 21)),
    "issue-022",
    "issue-023",
    "issue-024",
    "issue-027",
    "issue-028",
}


def validation_matrix(mode: str) -> tuple[int, int]:
    try:
        return VALIDATION_MATRICES[mode]
    except KeyError as error:
        raise ContractError(f"Validation mode is not reviewed: {mode}") from error


def scenario_execution_digest(
    scenario: Mapping[str, Any],
    *,
    authority_id: str,
    authority_kind: str,
    canonical_agent: str,
    logical_version: str,
    runtime_kind: str,
    framework: str,
    model_contract: Mapping[str, Any],
) -> str:
    executable = copy.deepcopy(dict(scenario))
    executable.pop("execution_digest", None)
    return content_hash(
        {
            "schema_version": VALIDATION_RULES_VERSION,
            "authority": {
                "authority_id": authority_id,
                "authority_kind": authority_kind,
                "canonical_agent": canonical_agent,
                "logical_version": logical_version,
            },
            "runtime": {
                "kind": runtime_kind,
                "framework": framework,
                "model_contract": dict(model_contract),
            },
            "scenario": executable,
        }
    )


def authority_execution_digest(
    validation_rules: Mapping[str, Any],
    *,
    authority_id: str,
    authority_kind: str,
    canonical_agent: str,
    logical_version: str,
    runtime_kind: str,
    framework: str,
    model_contract: Mapping[str, Any],
) -> str:
    executable = copy.deepcopy(dict(validation_rules))
    executable.pop("execution_digest", None)
    for scenario in executable.get("scenarios", []):
        if isinstance(scenario, dict):
            scenario.pop("execution_digest", None)
    return content_hash(
        {
            "schema_version": VALIDATION_RULES_VERSION,
            "authority": {
                "authority_id": authority_id,
                "authority_kind": authority_kind,
                "canonical_agent": canonical_agent,
                "logical_version": logical_version,
            },
            "runtime": {
                "kind": runtime_kind,
                "framework": framework,
                "model_contract": dict(model_contract),
            },
            "validation_rules": executable,
        }
    )


def stamp_execution_digests(
    validation_rules: dict[str, Any],
    *,
    authority_id: str,
    authority_kind: str,
    canonical_agent: str,
    logical_version: str,
    runtime_kind: str,
    framework: str,
    model_contract: Mapping[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(validation_rules)
    scenarios = result.get("scenarios")
    if not isinstance(scenarios, list):
        raise ContractError("Validation rules scenarios must be a list")
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise ContractError("Validation rule scenario must be an object")
        scenario["execution_digest"] = scenario_execution_digest(
            scenario,
            authority_id=authority_id,
            authority_kind=authority_kind,
            canonical_agent=canonical_agent,
            logical_version=logical_version,
            runtime_kind=runtime_kind,
            framework=framework,
            model_contract=model_contract,
        )
    result["execution_digest"] = authority_execution_digest(
        result,
        authority_id=authority_id,
        authority_kind=authority_kind,
        canonical_agent=canonical_agent,
        logical_version=logical_version,
        runtime_kind=runtime_kind,
        framework=framework,
        model_contract=model_contract,
    )
    return result


def generate_repository_validation_rules(
    agents: Mapping[str, Any],
    issues: Mapping[str, Any],
    *,
    check: bool = False,
) -> None:
    model_contract = agents["models"]["test_agents"]
    issue_by_id = {item["id"]: item for item in issues["issues"]}
    stale: list[str] = []
    for agent in agents["agents"]:
        authorities: list[tuple[str, str, Path, Mapping[str, Any] | None]] = [
            (
                f"{agent['name']}/v0",
                "baseline",
                ROOT / agent["baseline_path"] / "traffic.json",
                None,
            )
        ]
        authorities.extend(
            (
                issue_id,
                "issue",
                ROOT / issue_by_id[issue_id]["implementation"] / "traffic.json",
                issue_by_id[issue_id],
            )
            for issue_id in agent["issue_ids"]
        )
        for authority_id, authority_kind, path, issue in authorities:
            traffic = read_json(path)
            expected = build_validation_rules(
                traffic,
                authority_id=authority_id,
                authority_kind=authority_kind,
                agent=agent,
                issue=issue,
                model_contract=model_contract,
            )
            current = traffic.get("validation_rules")
            if not isinstance(current, dict) or not json_values_equal(
                current,
                expected,
            ):
                stale.append(path.relative_to(ROOT).as_posix())
                if not check:
                    traffic["validation_rules"] = expected
                    atomic_text(
                        path,
                        json.dumps(
                            traffic,
                            indent=2,
                            ensure_ascii=True,
                        )
                        + "\n",
                    )
            _validate_rules_schema(expected, authority_id)
            validate_validation_rules(
                expected,
                authority_id=authority_id,
                authority_kind=authority_kind,
                canonical_agent=agent["name"],
                logical_version="v0" if issue is None else issue["id"],
                runtime_kind=agent["type"],
                framework=agent["framework"],
                model_contract=model_contract,
                reviewed_mode=(
                    agent["baseline_contract"]["validation_mode"]
                    if issue is None
                    else issue["validation_mode"]
                ),
            )
    if check and stale:
        raise ContractError(
            "Generated validation rules are stale: " + ", ".join(stale)
        )


def build_validation_rules(
    traffic: Mapping[str, Any],
    *,
    authority_id: str,
    authority_kind: str,
    agent: Mapping[str, Any],
    issue: Mapping[str, Any] | None,
    model_contract: Mapping[str, Any],
) -> dict[str, Any]:
    requests = traffic.get("requests")
    if not isinstance(requests, list) or not requests:
        raise ContractError(f"{authority_id} has no endpoint traffic")
    mode = (
        agent["baseline_contract"]["validation_mode"]
        if issue is None
        else issue["validation_mode"]
    )
    n, k = validation_matrix(mode)
    candidates = _attempt_candidates(requests, issue is not None)
    if not candidates:
        raise ContractError(f"{authority_id} has no executable validation probes")
    safe_authority = authority_id.replace("/", "-")
    attempts = []
    observation_step_ids = []
    for index in range(1, n + 1):
        setup_source, probe_source = candidates[(index - 1) % len(candidates)]
        setup_values = setup_source or [_generic_setup(index)]
        setup_steps = [
            _validation_step(
                raw,
                step_id=f"{safe_authority}-setup-{index:02d}-{step_index:02d}",
                case_index=index,
                agent_name=agent["name"],
                issue_id=issue["id"] if issue is not None else None,
                probe=False,
            )
            for step_index, raw in enumerate(setup_values, start=1)
        ]
        probe_steps = [
            _validation_step(
                raw,
                step_id=f"{safe_authority}-probe-{index:02d}-{step_index:02d}",
                case_index=index,
                agent_name=agent["name"],
                issue_id=issue["id"] if issue is not None else None,
                probe=True,
            )
            for step_index, raw in enumerate(probe_source, start=1)
        ]
        attempts.append(
            {
                "index": index,
                "conversation_group": f"{safe_authority}-attempt-{index:02d}",
                "parameters": {
                    "case_id": f"case-{index:02d}",
                    "source_request_ids": [
                        str(item.get("id") or "")
                        for item in [*setup_source, *probe_source]
                    ],
                },
                "setup_steps": setup_steps,
                "probe_steps": probe_steps,
            }
        )
        observation_step_ids.append(probe_steps[0]["id"])
    scenario = {
        "id": (
            f"{agent['name']}-baseline-health"
            if issue is None
            else f"{issue['id']}-defect-path"
        ),
        "validation_mode": mode,
        "n": n,
        "k": k,
        "fixtures": [],
        "attempts": attempts,
        "healthy_predicate": (
            {"kind": "all_probe_assertions_pass"} if issue is None else None
        ),
        "defect_predicate": (
            {"kind": "never"}
            if issue is None
            else {
                "kind": "all_observation_steps_pass",
                "step_ids": observation_step_ids,
                "required_surfaces": (
                    ["trace"]
                    if issue["id"] in _TRACE_OBSERVED_ISSUES
                    else ["semantic"]
                ),
            }
        ),
        "v0_control_predicate": (
            None if issue is None else {"kind": "zero_defect_observations"}
        ),
    }
    return stamp_execution_digests(
        {
            "schema_version": VALIDATION_RULES_VERSION,
            "scenarios": [scenario],
        },
        authority_id=authority_id,
        authority_kind=authority_kind,
        canonical_agent=agent["name"],
        logical_version="v0" if issue is None else issue["id"],
        runtime_kind=agent["type"],
        framework=agent["framework"],
        model_contract=model_contract,
    )


def validate_rules_schema(
    validation_rules: Mapping[str, Any],
    authority_id: str,
) -> None:
    _validate_rules_schema(validation_rules, authority_id)


def _validate_rules_schema(
    validation_rules: Mapping[str, Any],
    authority_id: str,
) -> None:
    schema = read_json(VALIDATION_RULES_SCHEMA_PATH)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(validation_rules),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ContractError(
            f"{authority_id} validation rules schema error at {location}: "
            f"{error.message}"
        )


def _attempt_candidates(
    requests: list[Any],
    is_issue: bool,
) -> list[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    values = [item for item in requests if isinstance(item, dict)]
    if len(values) != len(requests):
        raise ContractError("Validation traffic requests must be objects")
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in values:
        body = item.get("request", {}).get("body", {})
        conversation = body.get("conversation", {})
        key = str(
            conversation.get("id")
            if isinstance(conversation, dict)
            else conversation
        )
        groups.setdefault(key or str(item.get("id") or ""), []).append(item)
    candidates: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
    if is_issue and any(
        item.get("expected", {}).get("activation_gate") is True
        for item in values
    ):
        for grouped in groups.values():
            active = [
                index
                for index, item in enumerate(grouped)
                if item.get("expected", {}).get("activation_gate") is True
            ]
            if not active:
                continue
            setup = grouped[: active[0]]
            for position, start in enumerate(active):
                end = active[position + 1] if position + 1 < len(active) else len(grouped)
                candidates.append((setup, grouped[start:end]))
        return candidates
    for grouped in groups.values():
        for index, item in enumerate(grouped):
            candidates.append((grouped[:index], [item]))
    return candidates


def _validation_step(
    raw: Mapping[str, Any],
    *,
    step_id: str,
    case_index: int,
    agent_name: str,
    issue_id: str | None,
    probe: bool,
) -> dict[str, Any]:
    request = copy.deepcopy(raw["request"])
    body = request["body"]
    body["conversation"] = {"id": CONVERSATION_PLACEHOLDER}
    _prefix_case_marker(body, case_index)
    expected = raw.get("expected")
    if not isinstance(expected, Mapping):
        expected = {}
    semantic = copy.deepcopy(expected.get("semantic_assertions") or {})
    trace = copy.deepcopy(expected.get("trace_assertions") or [])
    required_operations = expected.get("required_operations")
    if required_operations:
        trace.append(
            {
                "name": "required_operation_sequence",
                "kind": "operation_sequence",
                "operations": copy.deepcopy(required_operations),
            }
        )
    if probe and issue_id is not None:
        generated_semantic, generated_trace = _issue_assertions(
            issue_id,
            body,
        )
        if not semantic:
            semantic = generated_semantic
        if not trace:
            trace = generated_trace
    elif probe and issue_id is None and not semantic:
        semantic = _baseline_assertions(agent_name, str(raw.get("id") or ""))
    return {
        "id": step_id,
        "request": request,
        "expected": {
            "http_status": int(expected.get("http_status", 200)),
            "semantic_assertions": semantic,
            "trace_assertions": trace,
            "identity_assertions": {
                "agent_name": RUNTIME_AGENT_NAME_PLACEHOLDER,
                "agent_version": RUNTIME_AGENT_VERSION_PLACEHOLDER,
            },
        },
    }


def _generic_setup(index: int) -> dict[str, Any]:
    return {
        "id": f"validation-setup-{index:02d}",
        "request": {
            "method": "POST",
            "path": "/responses",
            "headers": {"content-type": "application/json"},
            "body": {
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "Acknowledge this fixed synthetic conversation "
                                    "context without external action."
                                ),
                            }
                        ],
                    }
                ],
                "conversation": {"id": CONVERSATION_PLACEHOLDER},
                "max_output_tokens": 200,
            },
        },
        "expected": {
            "http_status": 200,
            "semantic_assertions": {},
            "trace_assertions": [],
            "identity_assertions": {
                "agent_name": RUNTIME_AGENT_NAME_PLACEHOLDER,
                "agent_version": RUNTIME_AGENT_VERSION_PLACEHOLDER,
            },
        },
    }


def _prefix_case_marker(body: dict[str, Any], index: int) -> None:
    marker = f"Fixed synthetic case {index:02d}. "
    inputs = body.get("input")
    if not isinstance(inputs, list) or not inputs:
        raise ContractError("Validation request input is invalid")
    first = inputs[0]
    content = first.get("content") if isinstance(first, dict) else None
    if not isinstance(content, list) or not content:
        raise ContractError("Validation request content is invalid")
    part = content[0]
    if not isinstance(part, dict) or not isinstance(part.get("text"), str):
        raise ContractError("Validation request text is invalid")
    part["text"] = marker + part["text"]


def _issue_assertions(
    issue_id: str,
    body: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    text = _request_text(body)
    if issue_id == "issue-021":
        return (
            {"required_terms_all": ["invented-demo-seat"]},
            [
                {
                    "name": "failed_search_precedes_fabrication",
                    "kind": "tool_result_class",
                    "tool_name": "search_flights",
                    "result_class": "error",
                }
            ],
        )
    if issue_id == "issue-022":
        return (
            {
                "required_terms_all": ["hotel-demo-0"],
                "forbidden_terms": ["flight-demo-0"],
            },
            [
                {
                    "name": "flight_routed_to_hotel_search",
                    "kind": "tool_call_count",
                    "tool_name": "search_hotels",
                    "count": 1,
                }
            ],
        )
    if issue_id == "issue-023":
        tool_name = "search_hotels" if "hotel" in text.casefold() else "search_flights"
        return (
            {},
            [
                {
                    "name": "required_inventory_search_missing",
                    "kind": "tool_call_count",
                    "tool_name": tool_name,
                    "count": 0,
                }
            ],
        )
    if issue_id == "issue-024":
        tool_name = "search_hotels" if "hotel" in text.casefold() else "search_flights"
        return (
            {},
            [
                {
                    "name": "unused_optional_payload_overfetched",
                    "kind": "payload_multiplicity",
                    "tool_name": tool_name,
                    "source": "tool_result",
                    "path": "result_count",
                    "minimum": 80,
                }
            ],
        )
    if issue_id == "issue-025":
        return ({"required_terms_all": ["Booking completed"]}, [])
    if issue_id == "issue-026":
        trips = re.findall(r"trip-[a-z]+", text.casefold())
        if len(trips) < 2:
            raise ContractError("issue-026 validation prompt requires two trips")
        return (
            {
                "required_terms_all": [trips[0]],
                "forbidden_terms": [trips[1]],
            },
            [],
        )
    if issue_id == "issue-027":
        return (
            {},
            [
                {
                    "name": "independent_searches_are_serialized",
                    "kind": "span_relation",
                    "first_tool": "search_flights",
                    "second_tool": "search_hotels",
                    "relation": "ordered",
                }
            ],
        )
    support_exact = {
        "issue-029": "Recovery was exhausted without escalation.",
        "issue-030": "Update accepted at stale revision 2.",
        "issue-031": "The request stopped after repeated no-progress states.",
        "issue-035": "Update completed successfully.",
    }
    if issue_id in support_exact:
        return ({"exact_text": support_exact[issue_id]}, [])
    if issue_id == "issue-036":
        return (
            {
                "required_terms_all": [
                    "Shared state propagation failed",
                    "ticket identifier was lost",
                    "revision was lost",
                ]
            },
            [],
        )
    return {}, []


def _baseline_assertions(
    agent_name: str,
    request_id: str,
) -> dict[str, Any]:
    assertions = {
        "finance-agent": {
            "finance-agent-v0-grounded": {
                "required_terms_all": ["acct-demo-a", "USD"],
                "required_terms_any": ["1250.50", "1,250.50", "1250"],
            },
            "finance-agent-v0-bounded": {
                "required_terms_all": ["acct-demo-b"],
                "max_words": 40,
            },
            "finance-agent-v0-ordinary": {
                "required_terms_all": ["acct-demo-a", "budget"],
            },
            "finance-agent-v0-partial": {
                "required_terms_any": ["partial", "missing", "not found"],
            },
        },
        "travel-agent": {
            "travel-agent-v0-grounded": {
                "required_terms_all": [
                    "trip-alpha",
                    "flight-demo-0",
                    "Booking not completed",
                ],
            },
            "travel-agent-v0-bounded": {
                "required_terms_all": [
                    "trip-beta",
                    "hotel-demo-0",
                    "Showing 1 of 2",
                ],
            },
            "travel-agent-v0-transient": {
                "required_terms_all": ["trip-alpha", "flight-demo-0"],
            },
            "travel-agent-v0-partial": {
                "required_terms_all": [
                    "Partial result",
                    "trip-beta",
                    "flight-demo-0",
                ],
            },
        },
        "support-ticket-agent": {
            "support-ticket-agent-v0-grounded": {
                "required_terms_all": [
                    "ticket-demo-1",
                    "revision 3",
                    "open",
                ],
            },
            "support-ticket-agent-v0-bounded": {
                "required_terms_all": ["ticket-demo-2", "revision 1"],
                "max_words": 40,
            },
            "support-ticket-agent-v0-ordinary": {
                "required_terms_all": [
                    "ticket-demo-1",
                    "succeeded",
                    "revision 3",
                ],
            },
        },
    }
    result = assertions.get(agent_name, {}).get(request_id)
    if result is None:
        raise ContractError(
            f"{agent_name}/{request_id} baseline requires reviewed assertions"
        )
    return copy.deepcopy(result)


def _request_text(body: Mapping[str, Any]) -> str:
    values = []
    for message in body.get("input", []):
        if not isinstance(message, Mapping):
            continue
        for part in message.get("content", []):
            if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                values.append(part["text"])
    return " ".join(values)


def validate_validation_rules(
    validation_rules: Mapping[str, Any],
    *,
    authority_id: str,
    authority_kind: str,
    canonical_agent: str,
    logical_version: str,
    runtime_kind: str,
    framework: str,
    model_contract: Mapping[str, Any],
    reviewed_mode: str,
) -> None:
    if validation_rules.get("schema_version") != VALIDATION_RULES_VERSION:
        raise ContractError(f"{authority_id} validation rules version is invalid")
    scenarios = validation_rules.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ContractError(f"{authority_id} requires validation rule scenarios")
    scenario_ids: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, Mapping):
            raise ContractError(f"{authority_id} validation scenario is invalid")
        scenario_id = _nonempty_string(scenario.get("id"), "scenario id")
        if scenario_id in scenario_ids:
            raise ContractError(f"{authority_id} scenario IDs must be unique")
        scenario_ids.add(scenario_id)
        _validate_scenario(
            scenario,
            authority_id=authority_id,
            authority_kind=authority_kind,
            runtime_kind=runtime_kind,
            reviewed_mode=reviewed_mode,
        )
        expected_scenario_digest = scenario_execution_digest(
            scenario,
            authority_id=authority_id,
            authority_kind=authority_kind,
            canonical_agent=canonical_agent,
            logical_version=logical_version,
            runtime_kind=runtime_kind,
            framework=framework,
            model_contract=model_contract,
        )
        if scenario.get("execution_digest") != expected_scenario_digest:
            raise ContractError(
                f"{authority_id}/{scenario_id} execution digest is stale"
            )
    expected_digest = authority_execution_digest(
        validation_rules,
        authority_id=authority_id,
        authority_kind=authority_kind,
        canonical_agent=canonical_agent,
        logical_version=logical_version,
        runtime_kind=runtime_kind,
        framework=framework,
        model_contract=model_contract,
    )
    if validation_rules.get("execution_digest") != expected_digest:
        raise ContractError(f"{authority_id} execution digest is stale")


def _validate_scenario(
    scenario: Mapping[str, Any],
    *,
    authority_id: str,
    authority_kind: str,
    runtime_kind: str,
    reviewed_mode: str,
) -> None:
    mode = scenario.get("validation_mode")
    if mode != reviewed_mode:
        raise ContractError(
            f"{authority_id} validation mode differs from its reviewed classification"
        )
    n, k = validation_matrix(reviewed_mode)
    if scenario.get("n") != n or scenario.get("k") != k:
        raise ContractError(
            f"{authority_id} validation thresholds must remain {k}/{n}"
        )
    attempts = scenario.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != n:
        raise ContractError(f"{authority_id} must define exactly {n} attempts")
    fixtures = scenario.get("fixtures")
    if not isinstance(fixtures, list):
        raise ContractError(f"{authority_id} fixtures must be an array")
    if runtime_kind == "prompt" and fixtures:
        raise ContractError(f"{authority_id} Prompt validation cannot declare fixtures")

    expected_indexes = list(range(1, n + 1))
    indexes = [attempt.get("index") for attempt in attempts if isinstance(attempt, Mapping)]
    if indexes != expected_indexes:
        raise ContractError(
            f"{authority_id} attempts must be ordered and bounded from 1 through {n}"
        )
    conversation_groups: set[str] = set()
    probe_bodies: set[str] = set()
    probe_step_ids: set[str] = set()
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            raise ContractError(f"{authority_id} attempt must be an object")
        conversation_group = _nonempty_string(
            attempt.get("conversation_group"),
            "conversation group",
        )
        if conversation_group in conversation_groups:
            raise ContractError(
                f"{authority_id} attempts require isolated conversation groups"
            )
        conversation_groups.add(conversation_group)
        parameters = attempt.get("parameters")
        if not isinstance(parameters, Mapping) or not parameters:
            raise ContractError(
                f"{authority_id} attempts require fixed public-safe parameters"
            )
        _validate_public_safe_parameters(parameters, authority_id)
        attempt_probe_bodies: list[Mapping[str, Any]] = []
        for role in ("setup_steps", "probe_steps"):
            steps = attempt.get(role)
            if not isinstance(steps, list) or not steps:
                raise ContractError(
                    f"{authority_id} attempt {attempt['index']} requires {role}"
                )
            for step in steps:
                _validate_step(step, authority_id=authority_id, role=role)
                step_id = str(step["id"])
                if role == "probe_steps":
                    if step_id in probe_step_ids:
                        raise ContractError(
                            f"{authority_id} probe step IDs must be unique"
                        )
                    probe_step_ids.add(step_id)
                    attempt_probe_bodies.append(step["request"]["body"])
        probe_bodies.add(content_hash(attempt_probe_bodies))
    if len(probe_bodies) != n:
        raise ContractError(
            f"{authority_id} attempts require {n} fixed probe-body variations"
        )

    defect_predicate = scenario.get("defect_predicate")
    if not isinstance(defect_predicate, Mapping):
        raise ContractError(f"{authority_id} requires a defect predicate")
    predicate_kind = defect_predicate.get("kind")
    if authority_kind == "baseline":
        if reviewed_mode != "baseline" or predicate_kind != "never":
            raise ContractError(
                f"{authority_id} baseline cannot use an issue defect predicate"
            )
        if scenario.get("v0_control_predicate") is not None:
            raise ContractError(f"{authority_id} baseline cannot declare a v0 control")
        healthy = scenario.get("healthy_predicate")
        if (
            not isinstance(healthy, Mapping)
            or healthy.get("kind") != "all_probe_assertions_pass"
        ):
            raise ContractError(f"{authority_id} baseline healthy predicate is invalid")
    else:
        if reviewed_mode == "baseline":
            raise ContractError(f"{authority_id} issue cannot use baseline mode")
        if predicate_kind != "all_observation_steps_pass":
            raise ContractError(f"{authority_id} issue defect predicate is invalid")
        step_ids = defect_predicate.get("step_ids")
        if (
            not isinstance(step_ids, list)
            or not step_ids
            or len(step_ids) != len(set(step_ids))
            or not set(step_ids).issubset(probe_step_ids)
        ):
            raise ContractError(
                f"{authority_id} defect predicate must bind probe steps"
            )
        surfaces = defect_predicate.get("required_surfaces")
        if (
            not isinstance(surfaces, list)
            or not surfaces
            or len(surfaces) != len(set(surfaces))
            or not set(surfaces).issubset({"semantic", "trace"})
        ):
            raise ContractError(
                f"{authority_id} defect predicate assertion surfaces are invalid"
            )
        control = scenario.get("v0_control_predicate")
        if (
            not isinstance(control, Mapping)
            or control.get("kind") != "zero_defect_observations"
        ):
            raise ContractError(f"{authority_id} v0 control predicate is invalid")
        if scenario.get("healthy_predicate") is not None:
            raise ContractError(f"{authority_id} issue cannot declare baseline health")


def _validate_step(
    step: Any,
    *,
    authority_id: str,
    role: str,
) -> None:
    if not isinstance(step, Mapping):
        raise ContractError(f"{authority_id} validation step must be an object")
    _nonempty_string(step.get("id"), "step id")
    request = step.get("request")
    expected = step.get("expected")
    if not isinstance(request, Mapping) or not isinstance(expected, Mapping):
        raise ContractError(f"{authority_id} validation step is incomplete")
    if (
        request.get("method") != "POST"
        or request.get("path") != "/responses"
        or request.get("headers") != {"content-type": "application/json"}
        or not isinstance(request.get("body"), Mapping)
    ):
        raise ContractError(f"{authority_id} validation step is not endpoint traffic")
    body = request["body"]
    conversation = body.get("conversation")
    if (
        not isinstance(conversation, Mapping)
        or conversation.get("id") != CONVERSATION_PLACEHOLDER
    ):
        raise ContractError(
            f"{authority_id} validation steps require a fresh conversation placeholder"
        )
    if _contains_forbidden_selector(body):
        raise ContractError(f"{authority_id} contains a runtime injection selector")
    if set(expected) != {
        "http_status",
        "semantic_assertions",
        "trace_assertions",
        "identity_assertions",
    }:
        raise ContractError(
            f"{authority_id} validation step must bind all assertion surfaces"
        )
    if (
        isinstance(expected.get("http_status"), bool)
        or not isinstance(expected.get("http_status"), int)
        or not 100 <= expected["http_status"] <= 599
        or not isinstance(expected.get("semantic_assertions"), Mapping)
        or not isinstance(expected.get("trace_assertions"), list)
        or expected.get("identity_assertions")
        != {
            "agent_name": RUNTIME_AGENT_NAME_PLACEHOLDER,
            "agent_version": RUNTIME_AGENT_VERSION_PLACEHOLDER,
        }
    ):
        raise ContractError(f"{authority_id} validation step assertions are invalid")
    if (
        role == "probe_steps"
        and not expected["semantic_assertions"]
        and not expected["trace_assertions"]
    ):
        raise ContractError(f"{authority_id} probe steps require executable assertions")
    semantic = expected["semantic_assertions"]
    schema = semantic.get("json_schema")
    if schema is not None:
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            raise ContractError(
                f"{authority_id} validation assertion JSON schema is invalid"
            ) from error
    trace_assertions = expected["trace_assertions"]
    names = [
        assertion.get("name")
        for assertion in trace_assertions
        if isinstance(assertion, Mapping)
    ]
    if (
        len(names) != len(trace_assertions)
        or not all(isinstance(name, str) and name for name in names)
        or len(names) != len(set(names))
    ):
        raise ContractError(
            f"{authority_id} validation trace assertion names are invalid"
        )
    required_by_kind = {
        "tool_call_count": {"tool_name", "count"},
        "tool_argument_presence": {"tool_name", "argument", "present"},
        "scope_relation": {
            "tool_name",
            "scope_kind",
            "request_scope",
            "argument",
            "request_tool_equal",
        },
        "tool_result_class": {"tool_name", "result_class"},
        "retry_sequence": {"tool_name", "result_sequence"},
        "terminal_claim_relation": {"tool_name"},
        "payload_multiplicity": {"source", "minimum"},
        "span_relation": {"first_tool", "second_tool", "relation"},
        "operation_sequence": {"operations"},
    }
    for assertion in trace_assertions:
        kind = assertion.get("kind")
        required = required_by_kind.get(kind)
        if required is None or not required.issubset(assertion):
            raise ContractError(
                f"{authority_id} validation trace assertion is incomplete"
            )


def _validate_public_safe_parameters(
    value: Mapping[str, Any],
    authority_id: str,
) -> None:
    def valid(item: Any) -> bool:
        if item is None or isinstance(item, (str, bool, int)):
            return True
        if isinstance(item, list):
            return bool(item) and all(valid(child) for child in item)
        if isinstance(item, Mapping):
            return bool(item) and all(
                isinstance(key, str) and key and valid(child)
                for key, child in item.items()
            )
        return False

    if not valid(value):
        raise ContractError(f"{authority_id} validation parameters are invalid")


def _contains_forbidden_selector(value: Any) -> bool:
    if isinstance(value, Mapping):
        return bool(_FORBIDDEN_SELECTOR_KEYS.intersection(value)) or any(
            _contains_forbidden_selector(child) for child in value.values()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_forbidden_selector(child) for child in value)
    return False


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"Validation {label} must be a nonempty string")
    return value
