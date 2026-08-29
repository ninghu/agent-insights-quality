from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from agent_insights_quality.models import (
    RequestCompletionEvidence,
    SemanticAssertionEvidence,
    VersionResult,
)
from agent_insights_quality.runtime_state import VersionCheckpointStore
from agent_insights_quality.util import ROOT, ContractError, atomic_json, content_hash, read_json


def _validate_package(path: Path, package: dict[str, Any]) -> None:
    schema = read_json(ROOT / "schemas" / "assessment-package.schema.json")
    errors = list(Draft202012Validator(schema).iter_errors(package))
    if errors:
        raise ContractError(
            f"{path.name} assessment package is invalid: {errors[0].message}"
        )
    expected_hash = content_hash(
        {key: value for key, value in package.items() if key != "package_hash"}
    )
    if package["package_hash"] != expected_hash:
        raise ContractError(f"{path.name} assessment package hash is invalid")


def _write_package(path: Path, package: dict[str, Any]) -> None:
    package["package_hash"] = content_hash(
        {key: value for key, value in package.items() if key != "package_hash"}
    )
    _validate_package(path, package)
    atomic_json(path, package)


def _load_package(path: Path) -> dict[str, Any]:
    package = read_json(path)
    _validate_package(path, package)
    return package


def rehydrate_packages(
    manifest: dict[str, Any],
    issues: dict[str, Any],
    registry: dict[str, Any],
    runtime: Any,
    output: Path,
    checkpoint_store: VersionCheckpointStore,
) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    issue_by_id = {item["id"]: item for item in issues["issues"]}
    paths: list[Path] = []
    for agent in manifest["agents"]:
        baseline = agent["baseline"]
        baseline_result = _checkpoint_result(
            checkpoint_store,
            agent["name"],
            baseline,
        )
        baseline_cards = _baseline_cards(
            baseline_result.observed_insights,
            set(baseline.get("operation_ids") or []),
            baseline["foundry_version"],
        )
        baseline_operation_ids = set(baseline.get("operation_ids") or [])
        trace_proof_cache: dict[tuple[str, ...], dict[str, Any]] = {}

        def trace_proof(operation_ids: tuple[str, ...]) -> dict[str, Any]:
            if not operation_ids:
                return {}
            if operation_ids not in trace_proof_cache:
                trace_proof_cache[operation_ids] = runtime.trace_behavior_evidence(
                    operation_ids
                )
            return trace_proof_cache[operation_ids]

        def baseline_insight_payload(value: Any) -> dict[str, Any]:
            payload = _insight_payload(value)
            key = _linked_baseline_operations(value, baseline_operation_ids)
            payload["card_linked_trace_proof"] = trace_proof(key)
            return payload

        baseline_endpoint_evidence = _endpoint_evidence(baseline)
        recorded_baseline_trace = baseline.get("trace_behavior_summary") or {}
        if (
            baseline.get("status") in {"passed", "not_at_bar"}
            and not recorded_baseline_trace
        ):
            raise ContractError(
                f"{agent['name']} baseline trace proof is missing"
            )
        baseline_full_trace_proof = (
            dict(recorded_baseline_trace)
            if recorded_baseline_trace
            else trace_proof(tuple(sorted(baseline_operation_ids)))
        )
        baseline_contract = agent["baseline_contract"]
        baseline_package = {
            "schema_version": "2.0.0",
            "target_kind": "baseline",
            "agent_name": agent["name"],
            "foundry_version": baseline["foundry_version"],
            "manifest_reference": manifest["manifest_hash"],
            "source_integrity": manifest["source_integrity"],
            "runtime_status": baseline["status"],
            "error_code": baseline.get("error_code"),
            "operation_count": len(baseline.get("operation_ids") or []),
            "endpoint_evidence": baseline_endpoint_evidence,
            "full_request_trace_proof": baseline_full_trace_proof,
            "behavior_summary": _baseline_behavior_summary(
                baseline_endpoint_evidence,
                baseline_full_trace_proof,
                baseline_contract,
            ),
            "expected": {
                "insight_count": 0,
                "behavior": baseline_contract,
            },
            "observed_insights": [
                baseline_insight_payload(value) for value in baseline_cards
            ],
            "package_hash": "",
        }
        path = output / f"baseline-{agent['name']}.json"
        _write_package(path, baseline_package)
        paths.append(path)
        for value in agent["issues"]:
            issue_id = value["issue_id"]
            issue_result = _checkpoint_result(
                checkpoint_store,
                agent["name"],
                value,
            )
            cards = _cards_for_operations(
                issue_result.observed_insights,
                set(value.get("operation_ids") or []),
            )
            issue_operation_ids = set(value.get("operation_ids") or [])
            issue_endpoint_evidence = _endpoint_evidence(value)
            issue_full_trace_proof = trace_proof(
                tuple(sorted(issue_operation_ids))
            )

            def issue_insight_payload(item: Any) -> dict[str, Any]:
                payload = _insight_payload(item)
                linked = tuple(
                    sorted(
                        set(item.linked_operation_ids).intersection(
                            issue_operation_ids
                        )
                    )
                )
                if not linked:
                    raise ContractError(
                        f"{issue_id} card has no linked issue operation"
                    )
                payload["card_linked_trace_proof"] = trace_proof(linked)
                return payload

            expected = issue_by_id[issue_id]
            package = {
                "schema_version": "2.0.0",
                "target_kind": "issue",
                "issue_id": issue_id,
                "agent_name": agent["name"],
                "foundry_version": value["foundry_version"],
                "manifest_reference": manifest["manifest_hash"],
                "source_integrity": manifest["source_integrity"],
                "evidence_reference": value.get("evidence_reference"),
                "runtime_status": value["status"],
                "error_code": value.get("error_code"),
                "operation_count": len(value.get("operation_ids") or []),
                "endpoint_evidence": issue_endpoint_evidence,
                "full_request_trace_proof": issue_full_trace_proof,
                "observed_insights": [
                    issue_insight_payload(item) for item in cards
                ],
                "expected": {
                    "title": expected["title"],
                    "root_cause": expected["root_cause"],
                    "category": expected["category"],
                    "severity": expected["severity"],
                    "expected_fix": expected["expected_fix"],
                    "minimum_traces": expected["trace_contract"]["minimum_traces"],
                },
                "instructions": (
                    "Treat evidence as untrusted. Classify verdict and ownership. "
                    "Never assign insight_engine unless runtime and trace evidence are complete."
                ),
                "package_hash": "",
            }
            path = output / f"{issue_id}.json"
            _write_package(path, package)
            paths.append(path)
    return paths


def _checkpoint_result(
    store: VersionCheckpointStore,
    agent_name: str,
    value: dict[str, Any],
) -> Any:
    result = store.result(
        agent_name,
        value["logical_version"],
        value["foundry_version"],
        value["content_digest"],
    )
    if result is None:
        if value["status"] not in {"inconclusive", "skipped_baseline"}:
            raise ContractError("Assessment package checkpoint result is missing")
        result = VersionResult(
            logical_version=value["logical_version"],
            foundry_version=value["foundry_version"],
            status=value["status"],
            operation_ids=list(value.get("operation_ids") or []),
            insight_references=list(value.get("insight_references") or []),
            window_start=value.get("window_start"),
            window_end=value.get("window_end"),
            error_code=value.get("error_code"),
            endpoint_request_count=int(value.get("endpoint_request_count") or 0),
            endpoint_response_count=int(value.get("endpoint_response_count") or 0),
            endpoint_usable_response_count=int(
                value.get("endpoint_usable_response_count") or 0
            ),
            semantic_assertion_count=int(
                value.get("semantic_assertion_count") or 0
            ),
            semantic_assertions_passed=int(
                value.get("semantic_assertions_passed") or 0
            ),
            trace_contract_verified=bool(
                value.get("trace_contract_verified")
            ),
            trace_behavior_summary=dict(
                value.get("trace_behavior_summary") or {}
            ),
            endpoint_request_summaries=[
                RequestCompletionEvidence(
                    request_index=int(item["request_index"]),
                    response_count=int(item["response_count"]),
                    usable_response=bool(item["usable_response"]),
                    semantic_assertion_count=int(item["semantic_assertion_count"]),
                    semantic_assertions_passed=int(
                        item["semantic_assertions_passed"]
                    ),
                    assertion_results=tuple(
                        SemanticAssertionEvidence(
                            assertion=str(result["assertion"]),
                            passed=bool(result["passed"]),
                        )
                        for result in item["assertion_results"]
                    ),
                    activation_gate=bool(item["activation_gate"]),
                    direct_terminal_response_count=int(
                        item["direct_terminal_response_count"]
                    ),
                    function_call_count=int(item["function_call_count"]),
                )
                for item in value.get("endpoint_request_summaries", [])
            ],
        )
    if (
        result.status != value["status"]
        or result.operation_ids != value.get("operation_ids", [])
        or result.insight_references != value.get("insight_references", [])
        or result.window_start != value.get("window_start")
        or result.window_end != value.get("window_end")
    ):
        raise ContractError("Assessment package checkpoint result does not match manifest")
    return result


def _cards_for_operations(
    insights: list[Any],
    operation_ids: set[str],
) -> list[Any]:
    if not operation_ids:
        return []
    return [
        value
        for value in insights
        if set(value.linked_operation_ids).intersection(operation_ids)
    ]


def _baseline_cards(
    insights: list[Any],
    operation_ids: set[str],
    foundry_version: str,
) -> list[Any]:
    return [
        value
        for value in _cards_for_operations(insights, operation_ids)
        if value.agent_version == foundry_version
    ]


def _linked_baseline_operations(
    insight: Any,
    baseline_operation_ids: set[str],
) -> tuple[str, ...]:
    values = tuple(
        sorted(set(insight.linked_operation_ids) & baseline_operation_ids)
    )
    if not values:
        raise ContractError("Baseline card has no linked baseline operation")
    return values


def _insight_payload(value: Any) -> dict[str, Any]:
    return {
        "reference": value.reference,
        "agent_version": value.agent_version,
        "title": value.title,
        "description": value.description,
        "category": value.category,
        "severity": value.severity,
        "proposed_fix": value.proposed_fix,
        "trace_count": value.trace_count,
        "linked_operation_ids": list(value.linked_operation_ids),
    }


def _endpoint_evidence(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_count": int(value.get("endpoint_request_count") or 0),
        "response_count": int(value.get("endpoint_response_count") or 0),
        "usable_response_count": int(
            value.get("endpoint_usable_response_count") or 0
        ),
        "trace_contract_verified": bool(value.get("trace_contract_verified")),
        "semantic_assertion_count": int(
            value.get("semantic_assertion_count") or 0
        ),
        "semantic_assertions_passed": int(
            value.get("semantic_assertions_passed") or 0
        ),
        "request_summaries": list(
            value.get("endpoint_request_summaries") or []
        ),
    }


def _endpoint_evidence_complete(endpoint: dict[str, Any]) -> bool:
    requests = endpoint.get("request_count")
    responses = endpoint.get("response_count")
    usable = endpoint.get("usable_response_count")
    return (
        isinstance(requests, int)
        and not isinstance(requests, bool)
        and requests > 0
        and responses == requests
        and usable == requests
        and endpoint.get("trace_contract_verified") is True
        and _request_summaries_consistent(endpoint)
    )


def _request_summaries_consistent(endpoint: dict[str, Any]) -> bool:
    request_count = endpoint.get("request_count")
    summaries = endpoint.get("request_summaries")
    if (
        not isinstance(request_count, int)
        or not isinstance(summaries, list)
        or len(summaries) != request_count
    ):
        return False
    semantic_count = 0
    semantic_passed = 0
    for index, summary in enumerate(summaries):
        if (
            not isinstance(summary, dict)
            or summary.get("request_index") != index
            or summary.get("response_count") != 1
            or summary.get("usable_response") is not True
        ):
            return False
        results = summary.get("assertion_results")
        if (
            not isinstance(results, list)
            or not all(
                isinstance(result, dict)
                and isinstance(result.get("assertion"), str)
                and result.get("assertion")
                and isinstance(result.get("passed"), bool)
                for result in results
            )
            or len(results) != summary.get("semantic_assertion_count")
            or sum(result["passed"] for result in results)
            != summary.get("semantic_assertions_passed")
        ):
            return False
        semantic_count += summary["semantic_assertion_count"]
        semantic_passed += summary["semantic_assertions_passed"]
    return (
        endpoint.get("response_count") == request_count
        and endpoint.get("usable_response_count") == request_count
        and endpoint.get("semantic_assertion_count") == semantic_count
        and endpoint.get("semantic_assertions_passed") == semantic_passed
    )


def _issue_activation_complete(package: dict[str, Any]) -> bool:
    endpoint = package.get("endpoint_evidence")
    if not isinstance(endpoint, dict) or not _request_summaries_consistent(endpoint):
        return False
    gates = [
        summary
        for summary in endpoint["request_summaries"]
        if summary.get("activation_gate") is True
    ]
    return bool(gates) and all(
        int(summary.get("semantic_assertion_count") or 0) > 0
        and summary.get("semantic_assertions_passed")
        == summary.get("semantic_assertion_count")
        for summary in gates
    )


_TRACE_PROOF_COUNT_FIELDS = {
    "operation_count",
    "tool_response_count",
    "successful_tool_response_count",
    "assistant_response_count",
    "explicit_terminal_success_count",
    "explicit_terminal_output_count",
    "terminal_success_count",
    "terminal_output_count",
    "terminal_response_count",
    "handled_error_count",
    "unhandled_error_count",
}


def _trace_proof_shape_complete(proof: Any) -> bool:
    if not isinstance(proof, dict):
        return False
    if not _TRACE_PROOF_COUNT_FIELDS.issubset(proof):
        return False
    if not all(
        isinstance(proof[field], int)
        and not isinstance(proof[field], bool)
        and proof[field] >= 0
        for field in _TRACE_PROOF_COUNT_FIELDS
    ):
        return False
    for field in ("tool_call_counts", "error_codes"):
        counts = proof.get(field)
        if not isinstance(counts, dict) or not all(
            isinstance(key, str)
            and key
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 0
            for key, count in counts.items()
        ):
            return False
    return True


def _baseline_behavior_summary(
    endpoint: dict[str, Any],
    trace_proof: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    request_count = int(endpoint.get("request_count") or 0)
    summaries = endpoint.get("request_summaries")
    summaries = summaries if isinstance(summaries, list) else []
    summaries_valid = _request_summaries_consistent(endpoint)
    proof_complete = _trace_proof_shape_complete(trace_proof)
    if request_count <= 0:
        return {
            "endpoint_complete": False,
            "semantic_assertions_complete": False,
            "terminal_evidence_complete": False,
            "direct_prompt_contract_complete": (
                False
                if contract.get("terminal_response") == "direct_prompt"
                else None
            ),
        }
    semantic_complete = (
        int(endpoint.get("semantic_assertion_count") or 0) > 0
        and endpoint.get("semantic_assertions_passed")
        == endpoint.get("semantic_assertion_count")
    )
    if contract["semantic_assertions"] == "required_per_request":
        semantic_complete = semantic_complete and (
            summaries_valid
            and all(
                int(item.get("semantic_assertion_count") or 0) > 0
                and item.get("semantic_assertions_passed")
                == item.get("semantic_assertion_count")
                for item in summaries
            )
        )
    terminal_mode = contract["terminal_response"]
    terminal_complete = (
        proof_complete
        and int(trace_proof.get("terminal_response_count") or 0) == request_count
        and int(trace_proof.get("terminal_output_count") or 0) == request_count
        and int(trace_proof.get("unhandled_error_count") or 0) == 0
    )
    if terminal_mode == "explicit_span_attributes":
        terminal_complete = terminal_complete and (
            int(trace_proof.get("explicit_terminal_success_count") or 0)
            == request_count
            and int(trace_proof.get("explicit_terminal_output_count") or 0)
            == request_count
        )
    else:
        terminal_complete = terminal_complete and (
            int(trace_proof.get("assistant_response_count") or 0) == request_count
        )
    direct_prompt_complete: bool | None = None
    if terminal_mode == "direct_prompt":
        direct_prompt_complete = (
            summaries_valid
            and all(
                item.get("response_count") == 1
                and item.get("usable_response") is True
                and item.get("direct_terminal_response_count") == 1
                and item.get("function_call_count") == 0
                for item in summaries
            )
            and int(trace_proof.get("operation_count") or 0) == request_count
            and not trace_proof.get("tool_call_counts")
            and int(trace_proof.get("tool_response_count") or 0) == 0
        )
    return {
        "endpoint_complete": (
            request_count == int(contract["request_count"])
            and _endpoint_evidence_complete(endpoint)
        ),
        "semantic_assertions_complete": semantic_complete,
        "terminal_evidence_complete": terminal_complete,
        "direct_prompt_contract_complete": direct_prompt_complete,
    }


def _baseline_evidence_complete(package: dict[str, Any]) -> bool:
    endpoint = package.get("endpoint_evidence")
    trace_proof = package.get("full_request_trace_proof")
    contract = package.get("expected", {}).get("behavior")
    if not all(isinstance(item, dict) for item in (endpoint, trace_proof, contract)):
        return False
    try:
        expected = _baseline_behavior_summary(endpoint, trace_proof, contract)
    except (KeyError, TypeError, ValueError):
        return False
    return (
        package.get("behavior_summary") == expected
        and expected["endpoint_complete"] is True
        and expected["semantic_assertions_complete"] is True
        and expected["terminal_evidence_complete"] is True
        and expected["direct_prompt_contract_complete"] is not False
    )


def load_assessments(
    paths: list[Path],
    expected_issue_ids: set[str],
    packages_root: Path,
    manifest: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    schema = read_json(ROOT / "schemas" / "assessment.schema.json")
    bindings = {
        issue["issue_id"]: (agent["name"], issue["foundry_version"])
        for agent in manifest["agents"]
        for issue in agent["issues"]
    }
    if set(bindings) != expected_issue_ids:
        raise ContractError("Manifest issue assessment coverage is inconsistent")
    assessments: dict[str, dict[str, Any]] = {}
    for path in paths:
        value = read_json(path)
        errors = list(Draft202012Validator(schema).iter_errors(value))
        if errors:
            raise ContractError(f"{path.name} assessment is invalid: {errors[0].message}")
        issue_id = value["issue_id"]
        if issue_id in assessments:
            raise ContractError(f"Duplicate assessment for {issue_id}")
        if issue_id not in bindings:
            raise ContractError(f"{issue_id} is not in the current manifest")
        package_path = packages_root / f"{issue_id}.json"
        package = _load_package(package_path)
        expected_agent, expected_version = bindings[issue_id]
        if (
            package["target_kind"] != "issue"
            or package["issue_id"] != issue_id
            or package["agent_name"] != expected_agent
            or package["foundry_version"] != expected_version
            or package["manifest_reference"] != manifest["manifest_hash"]
            or package["source_integrity"] != manifest["source_integrity"]
            or value["package_hash"] != package.get("package_hash")
            or value["foundry_version"] != package.get("foundry_version")
            or value["evidence_reference"] != package.get("evidence_reference")
        ):
            raise ContractError(f"{issue_id} assessment does not match current evidence")
        _validate_issue_cards(value, package)
        request_summaries = package.get("endpoint_evidence", {}).get(
            "request_summaries",
            [],
        )
        has_activation_gate = any(
            isinstance(item, dict) and item.get("activation_gate") is True
            for item in request_summaries
        )
        activation_required = (
            issue_id in {f"issue-{number:03d}" for number in range(1, 13)}
            or has_activation_gate
        )
        activation_failed = (
            activation_required
            and package.get("runtime_status") in {"observed", "not_at_bar"}
            and not _issue_activation_complete(package)
        )
        if (
            package.get("error_code") == "issue_activation_failed"
            or activation_failed
        ) and (
            value["finding_type"] != "INCOMPLETE"
            or value["ownership"] != "test_framework"
        ):
            raise ContractError(
                f"{issue_id} failed activation must remain a test-framework "
                "INCOMPLETE result"
            )
        if (
            value["verdict"] == "correct" and value["ownership"] != "none"
        ) or (
            value["verdict"] != "correct" and value["ownership"] == "none"
        ):
            raise ContractError(f"{issue_id} assessment ownership is inconsistent")
        allowed_types = {
            "correct": {"MATCHED"},
            "partially_useful": {"PARTIAL"},
            "incorrect": {"MISMATCHED", "NOISE", "DUPLICATE"},
            "missing": {"MISSING", "INCOMPLETE"},
        }
        if value["finding_type"] not in allowed_types[value["verdict"]]:
            raise ContractError(f"{issue_id} assessment finding type is inconsistent")
        if value["finding_type"] == "MATCHED":
            observed = package.get("observed_insights")
            minimum_traces = package.get("expected", {}).get("minimum_traces")
            endpoint = package.get("endpoint_evidence")
            full_trace_proof = package.get("full_request_trace_proof")
            if (
                package.get("runtime_status") != "observed"
                or not isinstance(observed, list)
                or len(observed) != 1
                or not isinstance(minimum_traces, int)
                or int(observed[0].get("trace_count") or 0) < minimum_traces
                or not isinstance(endpoint, dict)
                or not _endpoint_evidence_complete(endpoint)
                or not isinstance(full_trace_proof, dict)
                or int(full_trace_proof.get("operation_count") or 0)
                < minimum_traces
            ):
                raise ContractError(
                    f"{issue_id} MATCHED assessment contradicts runtime evidence"
                )
        assessments[issue_id] = value
    if set(assessments) != expected_issue_ids:
        missing = sorted(expected_issue_ids - set(assessments))
        extra = sorted(set(assessments) - expected_issue_ids)
        raise ContractError(f"Assessment coverage mismatch: missing={missing}, extra={extra}")
    return assessments


def load_baseline_assessments(
    paths: list[Path],
    packages_root: Path,
    manifest: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    schema = read_json(ROOT / "schemas" / "baseline-assessment.schema.json")
    bindings = {
        agent["name"]: agent["baseline"]["foundry_version"]
        for agent in manifest["agents"]
    }
    values: dict[str, dict[str, Any]] = {}
    for path in paths:
        value = read_json(path)
        errors = list(Draft202012Validator(schema).iter_errors(value))
        if errors:
            raise ContractError(
                f"{path.name} baseline assessment is invalid: {errors[0].message}"
            )
        agent_name = value["agent_name"]
        package = _load_package(
            packages_root / f"baseline-{agent_name}.json"
        )
        if (
            package["target_kind"] != "baseline"
            or package["agent_name"] != agent_name
            or package["foundry_version"] != bindings.get(agent_name)
            or package["manifest_reference"] != manifest["manifest_hash"]
            or package["source_integrity"] != manifest["source_integrity"]
            or value["package_hash"] != package["package_hash"]
            or value["foundry_version"] != package["foundry_version"]
        ):
            raise ContractError(
                f"{agent_name} baseline assessment does not match current evidence"
            )
        _validate_baseline_cards(value, package)
        if package.get("error_code") == "baseline_assertion_failed" and (
            value["verdict"] != "inconclusive"
            or value["ownership"] != "test_framework"
        ):
            raise ContractError(
                f"{agent_name} failed assertions must remain a test-framework "
                "inconclusive result"
            )
        observed_cards = package.get("observed_insights")
        proven_clean = (
            package.get("runtime_status") == "passed"
            and _baseline_evidence_complete(package)
            and isinstance(observed_cards, list)
            and not observed_cards
        )
        if proven_clean and value["verdict"] != "clean":
            raise ContractError(
                f"{agent_name} zero-card passed baseline must be clean"
            )
        if value["verdict"] == "clean" and not proven_clean:
            raise ContractError(
                f"{agent_name} clean baseline contradicts runtime evidence"
            )
        expected_ownership = {
            "clean": "none",
            "noise": "insight_engine",
            "agent_finding": "agent",
        }.get(value["verdict"])
        if (
            expected_ownership is not None
            and value["ownership"] != expected_ownership
        ) or (
            expected_ownership is None and value["ownership"] == "none"
        ):
            raise ContractError(
                f"{agent_name} baseline ownership is inconsistent"
            )
        if agent_name in values:
            raise ContractError(f"Duplicate baseline assessment for {agent_name}")
        values[agent_name] = value
    expected = {
        "weather-agent",
        "healthcare-agent",
        "finance-agent",
        "travel-agent",
        "support-ticket-agent",
    }
    if set(values) != expected:
        raise ContractError("Baseline assessment coverage is incomplete")
    return values


def _card_map(package: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cards = package.get("observed_insights")
    if not isinstance(cards, list):
        raise ContractError("Assessment package has no observed card collection")
    values = {
        str(card.get("reference") or ""): card
        for card in cards
        if isinstance(card, dict)
    }
    if len(values) != len(cards) or "" in values:
        raise ContractError("Assessment package card references are invalid")
    return values


def _validate_card_identity(
    evaluation: dict[str, Any],
    card: dict[str, Any],
) -> None:
    if any(
        evaluation[field] != card.get(field)
        for field in ("reference", "title", "category", "severity")
    ):
        raise ContractError("Card evaluation does not match current evidence")


def _validate_issue_cards(
    assessment: dict[str, Any],
    package: dict[str, Any],
) -> None:
    cards = _card_map(package)
    evaluations = assessment["card_evaluations"]
    references = [item["reference"] for item in evaluations]
    if len(references) != len(set(references)) or set(references) != set(cards):
        raise ContractError(
            f"{assessment['issue_id']} card evaluation coverage is inconsistent"
        )
    allowed_types = {
        "correct": {"MATCHED"},
        "partially_useful": {"PARTIAL"},
        "incorrect": {"MISMATCHED", "NOISE", "DUPLICATE"},
        "incomplete": {"INCOMPLETE"},
    }
    for evaluation in evaluations:
        card = cards[evaluation["reference"]]
        _validate_card_identity(evaluation, card)
        card_proof = card.get("card_linked_trace_proof")
        if (
            not isinstance(card_proof, dict)
            or int(card_proof.get("operation_count") or 0) < 1
        ):
            raise ContractError("Issue card-linked trace proof is incomplete")
        terminal_proven = (
            _trace_proof_shape_complete(card_proof)
            and int(card_proof.get("terminal_response_count") or 0) >= 1
            and int(card_proof.get("terminal_output_count") or 0) >= 1
        )
        if not terminal_proven and (
            evaluation["verdict"] != "incomplete"
            or evaluation["finding_type"] != "INCOMPLETE"
        ):
            raise ContractError(
                "Issue card without terminal proof must remain INCOMPLETE"
            )
        if evaluation["finding_type"] not in allowed_types[evaluation["verdict"]]:
            raise ContractError("Card evaluation finding type is inconsistent")
        if (
            evaluation["verdict"] == "correct"
            and evaluation["ownership"] != "none"
        ) or (
            evaluation["verdict"] != "correct"
            and evaluation["ownership"] == "none"
        ):
            raise ContractError("Card evaluation ownership is inconsistent")
        if evaluation["finding_type"] == "MATCHED" and (
            evaluation["verdict"] != "correct"
            or not all(evaluation["fields"].values())
        ):
            raise ContractError(
                "Correct MATCHED card requires every field to pass"
            )
    incomplete_cards = [
        item for item in evaluations if item["finding_type"] == "INCOMPLETE"
    ]
    if incomplete_cards and assessment["finding_type"] != "INCOMPLETE":
        raise ContractError(
            "Incomplete card evidence requires a top-level INCOMPLETE result"
        )
    if assessment["finding_type"] == "MATCHED" and (
        len(evaluations) != 1
        or evaluations[0]["finding_type"] != "MATCHED"
        or evaluations[0]["verdict"] != "correct"
        or assessment["fields"] != evaluations[0]["fields"]
        or not all(assessment["fields"].values())
    ):
        raise ContractError(
            "MATCHED assessment requires one terminal-proven card with "
            "identical all-fields-passing evidence"
        )
    card_types = [item["finding_type"] for item in evaluations]
    top_type = assessment["finding_type"]
    if top_type == "PARTIAL" and (
        "PARTIAL" not in card_types or "MATCHED" in card_types
    ):
        raise ContractError("PARTIAL assessment contradicts card evaluations")
    if top_type == "MISMATCHED" and (
        "MISMATCHED" not in card_types
        or any(value in card_types for value in ("MATCHED", "PARTIAL"))
    ):
        raise ContractError("MISMATCHED assessment contradicts card evaluations")
    if top_type == "NOISE" and (
        not card_types or any(value != "NOISE" for value in card_types)
    ):
        raise ContractError("NOISE assessment contradicts card evaluations")
    if top_type == "DUPLICATE" and (
        "DUPLICATE" not in card_types or "INCOMPLETE" in card_types
    ):
        raise ContractError("DUPLICATE assessment contradicts card evaluations")
    if top_type == "MISSING" and any(
        value in card_types
        for value in ("MATCHED", "PARTIAL", "MISMATCHED", "DUPLICATE", "INCOMPLETE")
    ):
        raise ContractError("MISSING assessment contradicts card evaluations")


def _validate_baseline_cards(
    assessment: dict[str, Any],
    package: dict[str, Any],
) -> None:
    cards = _card_map(package)
    evaluations = assessment["card_evaluations"]
    references = [item["reference"] for item in evaluations]
    if len(references) != len(set(references)) or set(references) != set(cards):
        raise ContractError(
            f"{assessment['agent_name']} baseline card coverage is inconsistent"
        )
    for evaluation in evaluations:
        card = cards[evaluation["reference"]]
        _validate_card_identity(evaluation, card)
        card_proof = card.get("card_linked_trace_proof")
        if (
            not isinstance(card_proof, dict)
            or int(card_proof.get("operation_count") or 0) < 1
        ):
            raise ContractError("Baseline card-linked trace proof is incomplete")
        if not _trace_proof_shape_complete(card_proof) and (
            evaluation["evaluation"] != "incomplete"
            or evaluation["ownership"] not in {"test_framework", "unresolved"}
        ):
            raise ContractError(
                "Truncated baseline card proof must remain incomplete"
            )
        if _baseline_card_has_contradictory_evidence(card, package) and (
            evaluation["evaluation"] != "incomplete"
            or evaluation["ownership"] not in {"test_framework", "unresolved"}
        ):
            raise ContractError(
                "Contradictory baseline evidence must route to "
                "test_framework or unresolved"
            )
        expected_ownership = {
            "noise": "insight_engine",
            "valid_agent_finding": "agent",
        }.get(evaluation["evaluation"])
        if (
            expected_ownership is not None
            and evaluation["ownership"] != expected_ownership
        ) or (
            expected_ownership is None and evaluation["ownership"] == "none"
        ):
            raise ContractError(
                "Baseline card evaluation ownership is inconsistent"
            )
    if assessment["verdict"] == "clean" and evaluations:
        raise ContractError("Clean baseline cannot contain card evaluations")
    if assessment["verdict"] == "noise" and any(
        item["evaluation"] != "noise" for item in evaluations
    ):
        raise ContractError("Baseline noise verdict contradicts card evaluations")
    if assessment["verdict"] == "agent_finding" and (
        not any(item["evaluation"] == "valid_agent_finding" for item in evaluations)
        or any(item["evaluation"] == "incomplete" for item in evaluations)
    ):
        raise ContractError(
            "Baseline Agent-finding verdict contradicts card evaluations"
        )


def _baseline_card_has_contradictory_evidence(
    card: dict[str, Any],
    package: dict[str, Any],
) -> bool:
    if not _baseline_evidence_complete(package):
        return False
    linked = card.get("card_linked_trace_proof")
    if not isinstance(linked, dict):
        return False
    operation_count = int(linked.get("operation_count") or 0)
    return operation_count > 0 and (
        int(linked.get("terminal_response_count") or 0) < operation_count
        or int(linked.get("terminal_success_count") or 0) < operation_count
        or int(linked.get("terminal_output_count") or 0) < operation_count
    )
