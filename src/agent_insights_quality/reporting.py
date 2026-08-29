from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from agent_insights_quality.shadow_scoring import (
    SHADOW_SCORE_REPORT_PROFILES,
    calculate_shadow_quality_score,
    select_shadow_primary,
)
from agent_insights_quality.util import ROOT, ContractError, atomic_json, read_json
from agent_insights_quality.util import content_hash

REQUIRED_FIELDS = {
    "root_cause",
    "title",
    "description",
    "category",
    "severity",
    "proposed_fix",
    "linked_traces",
}
QUALITY_SCORE_FORMULA = "field_weighted_v1"
QUALITY_SCORE_THRESHOLD = 90
FIELD_QUALITY_WEIGHT = 0.85
CLEAN_CARD_PRECISION_WEIGHT = 0.15
FIELD_WEIGHTS = {
    "root_cause": 0.25,
    "title": 0.10,
    "description": 0.15,
    "category": 0.10,
    "severity": 0.10,
    "proposed_fix": 0.15,
    "linked_traces": 0.15,
}


def _request_summaries_complete(value: dict[str, Any]) -> bool:
    requests = value.get("endpoint_request_count")
    summaries = value.get("endpoint_request_summaries")
    if not isinstance(requests, int) or not isinstance(summaries, list):
        return False
    if len(summaries) != requests:
        return False
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
            or not all(isinstance(item, dict) for item in results)
            or len(results) != summary.get("semantic_assertion_count")
            or sum(item.get("passed") is True for item in results)
            != summary.get("semantic_assertions_passed")
        ):
            return False
    return True


def _runtime_evidence_complete(
    value: dict[str, Any],
    *,
    require_activation: bool = False,
) -> bool:
    requests = value.get("endpoint_request_count")
    responses = value.get("endpoint_response_count")
    usable = value.get("endpoint_usable_response_count")
    return (
        isinstance(requests, int)
        and not isinstance(requests, bool)
        and requests > 0
        and isinstance(responses, int)
        and not isinstance(responses, bool)
        and responses > 0
        and isinstance(usable, int)
        and not isinstance(usable, bool)
        and usable > 0
        and requests == responses == usable
        and value.get("trace_contract_verified") is True
        and _request_summaries_complete(value)
        and (
            (
                not require_activation
                or any(
                    item.get("activation_gate") is True
                    for item in value["endpoint_request_summaries"]
                )
            )
            and all(
                item.get("semantic_assertion_count", 0) > 0
                and item.get("semantic_assertions_passed")
                == item.get("semantic_assertion_count")
                for item in value["endpoint_request_summaries"]
                if item.get("activation_gate") is True
            )
        )
    )


def _baseline_runtime_evidence_complete(
    agent: dict[str, Any],
    value: dict[str, Any],
) -> bool:
    request_count = value.get("endpoint_request_count")
    trace = value.get("trace_behavior_summary")
    summaries = value.get("endpoint_request_summaries")
    terminal_mode = agent["baseline_contract"]["terminal_response"]
    if (
        not _runtime_evidence_complete(value)
        or request_count != agent["baseline_contract"]["request_count"]
        or not isinstance(trace, dict)
        or not isinstance(summaries, list)
        or int(value.get("semantic_assertion_count") or 0) < 1
        or value.get("semantic_assertions_passed")
        != value.get("semantic_assertion_count")
        or int(trace.get("terminal_response_count") or 0) != request_count
        or int(trace.get("terminal_output_count") or 0) != request_count
        or int(trace.get("unhandled_error_count") or 0) != 0
    ):
        return False
    if terminal_mode == "explicit_span_attributes":
        if (
            int(trace.get("explicit_terminal_success_count") or 0)
            != request_count
            or int(trace.get("explicit_terminal_output_count") or 0)
            != request_count
        ):
            return False
    elif int(trace.get("assistant_response_count") or 0) != request_count:
        return False
    if agent["baseline_contract"]["semantic_assertions"] == "required_per_request":
        if any(int(item.get("semantic_assertion_count") or 0) < 1 for item in summaries):
            return False
    if agent["type"] == "prompt":
        return (
            int(trace.get("operation_count") or 0) == request_count
            and not trace.get("tool_call_counts")
            and int(trace.get("tool_response_count") or 0) == 0
            and all(
                item.get("direct_terminal_response_count") == 1
                and item.get("function_call_count") == 0
                for item in summaries
            )
        )
    return True


def _activation_evidence(value: dict[str, Any]) -> dict[str, int]:
    gates = [
        item
        for item in value.get("endpoint_request_summaries", [])
        if isinstance(item, dict) and item.get("activation_gate") is True
    ]
    return {
        "request_count": len(gates),
        "assertion_count": sum(
            int(item.get("semantic_assertion_count") or 0) for item in gates
        ),
        "assertions_passed": sum(
            int(item.get("semantic_assertions_passed") or 0) for item in gates
        ),
    }


def calculate_quality_score(
    *,
    field_quality_score: float,
    clean_card_precision: float,
    incomplete: bool,
) -> int | float | None:
    if incomplete:
        return None
    value = round(
        max(
            0.0,
            min(
                100.0,
                FIELD_QUALITY_WEIGHT * field_quality_score
                + CLEAN_CARD_PRECISION_WEIGHT * clean_card_precision,
            ),
        ),
        1,
    )
    return int(value) if value.is_integer() else value


def _field_score(fields: dict[str, Any]) -> float:
    return 100.0 * sum(
        weight
        for field, weight in FIELD_WEIGHTS.items()
        if fields.get(field) is True
    )


def _issue_field_score(item: dict[str, Any]) -> float:
    if (
        item["assessment"].get("finding_type") == "MATCHED"
        and not all(item["assessment"].get("fields", {}).values())
    ):
        return 0.0
    cards = item["assessment"].get("card_evaluations", [])
    attributable = [
        _field_score(card["fields"])
        for card in cards
        if card.get("finding_type") != "NOISE"
    ]
    if attributable:
        score = max(attributable)
    elif item["detail"] in {"MISSING", "INCOMPLETE"}:
        score = 0.0
    else:
        score = _field_score(item["assessment"]["fields"])
    if item["result"] != "PASS" and score >= 100.0:
        return 0.0
    return score


def _summary_metrics(
    baseline: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    *,
    incomplete: bool,
) -> dict[str, Any]:
    baseline_passed = sum(
        item["status"] == "passed"
        and item["insight_count"] == 0
        and item["assessment"]["verdict"] == "clean"
        for item in baseline
    )
    issues_correct = sum(
        item["status"] == "observed"
        and item["observed_count"] == 1
        and item["assessment"]["verdict"] == "correct"
        and all(item["assessment"]["fields"].values())
        for item in issues
    )
    issues_partial = sum(item["detail"] == "PARTIAL" for item in issues)
    noise_cards = sum(
        sum(
            card.get("evaluation") == "noise"
            for card in item["assessment"].get("card_evaluations", [])
        )
        if "card_evaluations" in item["assessment"]
        else item["insight_count"]
        if item["assessment"]["verdict"] == "noise"
        else 0
        for item in baseline
    ) + sum(
        sum(
            card.get("finding_type") in {"NOISE", "DUPLICATE"}
            for card in item["assessment"].get("card_evaluations", [])
        )
        if "card_evaluations" in item["assessment"]
        else item["observed_count"]
        if item["detail"] in {"NOISE", "DUPLICATE"}
        else 0
        for item in issues
    )
    unverified_cards = sum(
        sum(
            card.get("evaluation") == "incomplete"
            for card in item["assessment"].get("card_evaluations", [])
        )
        for item in baseline
    ) + sum(
        sum(
            card.get("finding_type") == "INCOMPLETE"
            for card in item["assessment"].get("card_evaluations", [])
        )
        for item in issues
    )
    non_clean_cards = (
        sum(
            sum(
                card.get("evaluation") == "noise"
                for card in item["assessment"].get("card_evaluations", [])
            )
            for item in baseline
        )
        + sum(
            sum(
                card.get("finding_type")
                in {"NOISE", "DUPLICATE", "INCOMPLETE"}
                for card in item["assessment"].get("card_evaluations", [])
            )
            for item in issues
        )
    )
    observed_cards = sum(item["insight_count"] for item in baseline) + sum(
        item["observed_count"] for item in issues
    )
    field_quality_score = (
        sum(_issue_field_score(item) for item in issues) / len(issues)
        if issues
        else 0.0
    )
    clean_card_precision = (
        100.0 * max(observed_cards - non_clean_cards, 0) / observed_cards
        if observed_cards
        else 0.0
    )
    incomplete_reasons = sorted(
        {
            str(item["error_code"])
            for item in [*baseline, *issues]
            if item.get("error_code")
        }
        | (
            {"assessment_evidence_incomplete"}
            if any(
                item["assessment"]["verdict"] == "inconclusive"
                or any(
                    card.get("evaluation") == "incomplete"
                    for card in item["assessment"].get("card_evaluations", [])
                )
                for item in baseline
            )
            or any(
                item["assessment"]["finding_type"] == "INCOMPLETE"
                for item in issues
            )
            else set()
        )
        | (
            {"runtime_evidence_incomplete"}
            if any(
                item.get("runtime_evidence_complete") is not True
                for item in [*baseline, *issues]
            )
            else set()
        )
    )
    quality_score = calculate_quality_score(
        field_quality_score=field_quality_score,
        clean_card_precision=clean_card_precision,
        incomplete=incomplete,
    )
    return {
        "baseline_passed": baseline_passed,
        "issues_expected": len(issues),
        "issues_correct": issues_correct,
        "issues_partial": issues_partial,
        "quality_failures": (
            sum(
                item["status"] in {"passed", "not_at_bar"}
                and (
                    item["insight_count"] != 0
                    or item["assessment"]["verdict"] != "clean"
                )
                for item in baseline
            )
            + sum(item["result"] == "FAIL" for item in issues)
        ),
        "incomplete": incomplete,
        "incomplete_reasons": incomplete_reasons,
        "noise_cards": noise_cards,
        "unverified_cards": unverified_cards,
        "observed_cards": observed_cards,
        "field_quality_score": round(field_quality_score, 1),
        "clean_card_precision": round(clean_card_precision, 1),
        "quality_score": quality_score,
        "quality_threshold": QUALITY_SCORE_THRESHOLD,
        "quality_score_formula": QUALITY_SCORE_FORMULA,
    }


def _apply_staging_shadow_score(report: dict[str, Any]) -> None:
    if report["profile"] not in SHADOW_SCORE_REPORT_PROFILES:
        return
    incomplete = report["summary"]["incomplete"]
    report["summary"]["shadow_quality_score"] = calculate_shadow_quality_score(
        report["baseline"],
        report["issues"],
        incomplete=incomplete,
    )
    for item in report["issues"]:
        item["shadow_v2_primary"] = (
            None if incomplete else select_shadow_primary(item)
        )


def build_report(
    manifest: dict[str, Any],
    issues: dict[str, Any],
    assessments: dict[str, dict[str, Any]],
    baseline_assessments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    issue_by_id = {item["id"]: item for item in issues["issues"]}
    baseline = []
    results = []
    incomplete = False
    for agent in manifest["agents"]:
        baseline_value = agent["baseline"]
        baseline_runtime_complete = _baseline_runtime_evidence_complete(
            agent,
            baseline_value,
        )
        trace_summary = baseline_value.get("trace_behavior_summary") or {}
        baseline.append(
            {
                "agent": agent["name"],
                "logical_version": "v0",
                "foundry_version": baseline_value["foundry_version"],
                "status": baseline_value["status"],
                "error_code": baseline_value.get("error_code"),
                "runtime_evidence_complete": baseline_runtime_complete,
                "insight_count": len(baseline_value["insight_references"]),
                "terminal_evidence": {
                    "response_count": int(
                        trace_summary.get("terminal_response_count") or 0
                    ),
                    "success_count": int(
                        trace_summary.get("terminal_success_count") or 0
                    ),
                    "explicit_success_count": int(
                        trace_summary.get("explicit_terminal_success_count") or 0
                    ),
                    "output_count": int(
                        trace_summary.get("terminal_output_count") or 0
                    ),
                    "explicit_output_count": int(
                        trace_summary.get("explicit_terminal_output_count") or 0
                    ),
                    "handled_error_count": int(
                        trace_summary.get("handled_error_count") or 0
                    ),
                    "unhandled_error_count": int(
                        trace_summary.get("unhandled_error_count") or 0
                    ),
                },
                "assessment": {
                    "verdict": baseline_assessments[agent["name"]]["verdict"],
                    "ownership": baseline_assessments[agent["name"]]["ownership"],
                    "ownership_reason": baseline_assessments[agent["name"]][
                        "ownership_reason"
                    ],
                    "confidence": baseline_assessments[agent["name"]]["confidence"],
                    "card_evaluations": baseline_assessments[agent["name"]][
                        "card_evaluations"
                    ],
                },
            }
        )
        if (
            baseline_value["status"] not in {"passed", "not_at_bar"}
            or not baseline_runtime_complete
            or baseline_assessments[agent["name"]]["verdict"] == "inconclusive"
            or any(
                card.get("evaluation") == "incomplete"
                for card in baseline_assessments[agent["name"]][
                    "card_evaluations"
                ]
            )
        ):
            incomplete = True
        for value in agent["issues"]:
            issue_id = value["issue_id"]
            assessment = assessments[issue_id]
            runtime_complete = _runtime_evidence_complete(
                value,
                require_activation=agent["type"] == "prompt",
            )
            fields_pass = (
                set(assessment["fields"]) == REQUIRED_FIELDS
                and all(assessment["fields"].values())
            )
            complete = value["status"] not in {
                "inconclusive",
                "skipped_baseline",
            } and runtime_complete and assessment["finding_type"] != "INCOMPLETE"
            correct = (
                value["status"] == "observed"
                and len(value["insight_references"]) == 1
                and assessment["verdict"] == "correct"
                and fields_pass
            )
            if not complete:
                incomplete = True
            results.append(
                {
                    "issue_id": issue_id,
                    "agent": agent["name"],
                    "logical_version": issue_id,
                    "foundry_version": value["foundry_version"],
                    "title": issue_by_id[issue_id]["title"],
                    "status": value["status"],
                    "error_code": value.get("error_code"),
                    "runtime_evidence_complete": runtime_complete,
                    "activation_evidence": _activation_evidence(value),
                    "result": (
                        "INCOMPLETE"
                        if not complete
                        else "PASS"
                        if correct
                        else "FAIL"
                    ),
                    "detail": {
                        "correct": assessment["finding_type"],
                        "partially_useful": assessment["finding_type"],
                        "incorrect": assessment["finding_type"],
                        "missing": assessment["finding_type"],
                    }[assessment["verdict"]],
                    "observed_count": len(value["insight_references"]),
                    "assessment": {
                        "verdict": assessment["verdict"],
                        "confidence": assessment["confidence"],
                        "fields": assessment["fields"],
                        "ownership": assessment["ownership"],
                        "finding_type": assessment["finding_type"],
                        "ownership_reason": assessment["ownership_reason"],
                        "card_evaluations": assessment["card_evaluations"],
                    },
                    "evidence_reference": value.get("evidence_reference"),
                }
            )
    summary = _summary_metrics(baseline, results, incomplete=incomplete)
    status = (
        "INCOMPLETE"
        if incomplete
        else "PASS"
        if summary["quality_score"] >= QUALITY_SCORE_THRESHOLD
        else "FAIL"
    )
    report = {
        "schema_version": "2.0.0",
        "report_date": manifest["report_date"],
        "run_id": manifest["run_id"],
        "profile": manifest["profile"],
        "manifest_reference": manifest["manifest_hash"],
        "catalog_hashes": manifest["catalog_hashes"],
        "source_integrity": manifest["source_integrity"],
        "status": status,
        "baseline": baseline,
        "issues": results,
        "summary": summary,
        "delivery": {"content_digest": "sha256:" + "0" * 64},
    }
    _apply_staging_shadow_score(report)
    return report


def build_operational_failure_report(
    *,
    report_date: Any,
    run_id: str,
    profile: str,
    selected: dict[str, list[str]],
    issues: dict[str, Any],
    failure_code: str,
    catalog_hashes: dict[str, str],
) -> dict[str, Any]:
    issue_by_id = {item["id"]: item for item in issues["issues"]}
    values = [
        {
            "issue_id": issue_id,
            "agent": agent_name,
            "logical_version": issue_id,
            "foundry_version": "unavailable",
            "title": issue_by_id[issue_id]["title"],
            "status": "inconclusive",
            "result": "INCOMPLETE",
            "detail": "INCOMPLETE",
            "runtime_evidence_complete": False,
            "observed_count": 0,
            "assessment": {
                "verdict": "missing",
                "finding_type": "INCOMPLETE",
                "ownership": "infrastructure",
                "ownership_reason": (
                    "Qualification failed before trustworthy issue evidence."
                ),
                "confidence": 0.0,
                "fields": {field: False for field in sorted(REQUIRED_FIELDS)},
                "card_evaluations": [],
            },
            "evidence_reference": None,
        }
        for agent_name, issue_ids in selected.items()
        for issue_id in issue_ids
    ]
    report = {
        "schema_version": "2.0.0",
        "report_date": report_date.isoformat(),
        "run_id": run_id,
        "profile": profile,
        "manifest_reference": content_hash(
            {
                "run_id": run_id,
                "profile": profile,
                "selected": selected,
                "failure_code": failure_code,
            }
        ),
        "catalog_hashes": catalog_hashes,
        "source_integrity": {
            "verified": False,
            "contract_digest": None,
        },
        "status": "INCOMPLETE",
        "baseline": [
            {
                "agent": agent_name,
                "logical_version": "v0",
                "foundry_version": "unavailable",
                "status": "inconclusive",
                "runtime_evidence_complete": False,
                "insight_count": 0,
                "assessment": {
                    "verdict": "inconclusive",
                    "ownership": "infrastructure",
                    "ownership_reason": (
                        "Qualification failed before a trustworthy baseline assessment."
                    ),
                    "confidence": 1.0,
                    "card_evaluations": [],
                },
            }
            for agent_name in selected
        ],
        "issues": values,
        "summary": {
            "baseline_passed": 0,
            "issues_expected": len(values),
            "issues_correct": 0,
            "issues_partial": 0,
            "quality_failures": 0,
            "incomplete": True,
            "incomplete_reasons": [failure_code],
            "failure_code": failure_code,
            "noise_cards": 0,
            "unverified_cards": 0,
            "observed_cards": 0,
            "field_quality_score": None,
            "clean_card_precision": None,
            "quality_score": None,
            "quality_threshold": QUALITY_SCORE_THRESHOLD,
            "quality_score_formula": QUALITY_SCORE_FORMULA,
        },
        "delivery": {"content_digest": "sha256:" + "0" * 64},
    }
    _apply_staging_shadow_score(report)
    return report


def validate_report(report: dict[str, Any]) -> None:
    schema = read_json(ROOT / "schemas" / "report.schema.json")
    errors = list(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(report)
    )
    if errors:
        raise ContractError(f"Report is invalid: {errors[0].message}")
    source_integrity = report["source_integrity"]
    if report["status"] in {"PASS", "FAIL"} and (
        source_integrity.get("verified") is not True
        or not isinstance(source_integrity.get("contract_digest"), str)
    ):
        raise ContractError("Complete report source integrity is incomplete")
    if len(report["issues"]) not in {25, 36}:
        raise ContractError("A report must contain the daily 25 or staging 36 issues")
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("runtime_evidence_complete"), bool)
        for item in [*report["baseline"], *report["issues"]]
    ):
        raise ContractError("Report runtime evidence is incomplete")
    if report["profile"] in SHADOW_SCORE_REPORT_PROFILES:
        expected_shadow = calculate_shadow_quality_score(
            report["baseline"],
            report["issues"],
            incomplete=report["summary"]["incomplete"],
        )
        if report["summary"].get("shadow_quality_score") != expected_shadow:
            raise ContractError("Staging shadow score is inconsistent")
        incomplete = report["summary"]["incomplete"]
        if any(
            item.get("shadow_v2_primary")
            != (None if incomplete else select_shadow_primary(item))
            for item in report["issues"]
        ):
            raise ContractError("Staging shadow primary selection is inconsistent")


def _validate_complete_summary(
    report: dict[str, Any],
    *,
    expected_count: int,
    label: str,
) -> None:
    assessment_incomplete = any(
        item.get("runtime_evidence_complete") is not True
        or item["assessment"]["verdict"] == "inconclusive"
        or any(
            card.get("evaluation") == "incomplete"
            for card in item["assessment"].get("card_evaluations", [])
        )
        for item in report["baseline"]
    ) or any(
        item.get("runtime_evidence_complete") is not True
        or item["result"] == "INCOMPLETE"
        or item["assessment"]["finding_type"] == "INCOMPLETE"
        for item in report["issues"]
    )
    if assessment_incomplete:
        raise ContractError(f"{label} report is incomplete")
    expected = _summary_metrics(
        report["baseline"],
        report["issues"],
        incomplete=False,
    )
    summary = report["summary"]
    if (
        summary.get("incomplete") is not False
        or expected["issues_expected"] != expected_count
        or any(summary.get(key) != value for key, value in expected.items())
    ):
        raise ContractError(f"{label} report summary is inconsistent")
    expected_status = (
        "PASS"
        if expected["quality_score"] >= QUALITY_SCORE_THRESHOLD
        else "FAIL"
    )
    if report["status"] != expected_status:
        raise ContractError(f"{label} report status is inconsistent")


def _baseline_report_semantics_valid(item: dict[str, Any]) -> bool:
    assessment = item.get("assessment")
    if not isinstance(assessment, dict):
        return False
    cards = assessment.get("card_evaluations")
    if (
        not isinstance(cards, list)
        or len(cards) != item.get("insight_count")
        or any(
            not isinstance(card, dict)
            or card.get("evaluation")
            not in {"noise", "valid_agent_finding", "incomplete"}
            or card.get("ownership")
            not in {
                "agent",
                "insight_engine",
                "test_framework",
                "infrastructure",
                "unresolved",
            }
            or (
                card.get("evaluation") == "noise"
                and card.get("ownership") != "insight_engine"
            )
            or (
                card.get("evaluation") == "valid_agent_finding"
                and card.get("ownership") != "agent"
            )
            or (
                card.get("evaluation") == "incomplete"
                and card.get("ownership") == "none"
            )
            for card in cards
        )
    ):
        return False
    verdict = assessment.get("verdict")
    ownership = assessment.get("ownership")
    if verdict == "clean":
        return ownership == "none" and not cards
    if verdict == "noise":
        return (
            ownership == "insight_engine"
            and bool(cards)
            and all(card["evaluation"] == "noise" for card in cards)
        )
    if verdict == "agent_finding":
        return (
            ownership == "agent"
            and any(card["evaluation"] == "valid_agent_finding" for card in cards)
            and all(card["evaluation"] != "incomplete" for card in cards)
        )
    return verdict == "inconclusive" and ownership not in {None, "none"}


def validate_published_report(
    report: dict[str, Any],
    issue_catalog: dict[str, Any] | None = None,
    expected_selection: dict[str, list[str]] | None = None,
) -> None:
    validate_report(report)
    if (
        report["source_integrity"].get("verified") is not True
        or not isinstance(report["source_integrity"].get("contract_digest"), str)
    ):
        raise ContractError("Published report source integrity is incomplete")
    if report["profile"] != "daily" or report["status"] not in {"PASS", "FAIL"}:
        raise ContractError("Published report has an ineligible profile or status")
    baseline = report["baseline"]
    expected_agents = {
        "weather-agent",
        "healthcare-agent",
        "finance-agent",
        "travel-agent",
        "support-ticket-agent",
    }
    if (
        len(baseline) != 5
        or {item.get("agent") for item in baseline} != expected_agents
        or any(
            item.get("status") not in {"passed", "not_at_bar"}
            or not isinstance(item.get("insight_count"), int)
            or item["insight_count"] < 0
            or not isinstance(item.get("assessment"), dict)
            or item["assessment"].get("verdict")
            not in {"clean", "noise", "agent_finding", "inconclusive"}
            or item["assessment"].get("ownership")
            not in {
                "none",
                "agent",
                "insight_engine",
                "test_framework",
                "infrastructure",
                "unresolved",
            }
            or not _baseline_report_semantics_valid(item)
            for item in baseline
        )
    ):
        raise ContractError("Published report baseline is incomplete")
    issues = report["issues"]
    issue_by_id = (
        {item["id"]: item for item in issue_catalog["issues"]}
        if issue_catalog is not None
        else None
    )
    if (
        len(issues) != 25
        or len({item.get("issue_id") for item in issues}) != 25
        or any(
            item.get("status") in {"inconclusive", "skipped_baseline", None}
            or not isinstance(item.get("observed_count"), int)
            or item.get("result") not in {"PASS", "FAIL"}
            or item.get("detail")
            not in {
                "MATCHED",
                "PARTIAL",
                "MISMATCHED",
                "MISSING",
                "NOISE",
                "DUPLICATE",
                "INCOMPLETE",
            }
            or not isinstance(item.get("assessment"), dict)
            or item["assessment"].get("verdict")
            not in {"correct", "partially_useful", "incorrect", "missing"}
            or item["assessment"].get("finding_type") != item.get("detail")
            or not isinstance(item["assessment"].get("fields"), dict)
            or set(item["assessment"]["fields"]) != REQUIRED_FIELDS
            or not isinstance(item["assessment"].get("confidence"), (int, float))
            or not 0 <= item["assessment"]["confidence"] <= 1
            or (
                item.get("status") == "observed"
                and (
                    item.get("observed_count", 0) < 1
                    or re.fullmatch(
                        r"sha256:[0-9a-f]{64}",
                        str(item.get("evidence_reference") or ""),
                    )
                    is None
                )
            )
            for item in issues
        )
    ):
        raise ContractError("Published report issue results are incomplete")
    if issue_by_id is not None:
        if any(
            item["issue_id"] not in issue_by_id
            or item["agent"] != issue_by_id[item["issue_id"]]["agent"]
            or item["title"] != issue_by_id[item["issue_id"]]["title"]
            for item in issues
        ):
            raise ContractError("Published report issue assignments do not match the catalog")
        if {
            agent: sum(item["agent"] == agent for item in issues)
            for agent in expected_agents
        } != {agent: 5 for agent in expected_agents}:
            raise ContractError("Published report must contain five issues per Agent")
    if expected_selection is not None:
        actual = {
            agent: {
                item["issue_id"] for item in issues if item["agent"] == agent
            }
            for agent in expected_agents
        }
        expected = {
            agent: set(issue_ids)
            for agent, issue_ids in expected_selection.items()
        }
        if actual != expected:
            raise ContractError("Published report does not match deterministic daily selection")
    _validate_complete_summary(report, expected_count=25, label="Published")
    if report["delivery"]["content_digest"] == "sha256:" + "0" * 64:
        raise ContractError("Published report has no bound email content digest")


def validate_staging_report(
    report: dict[str, Any],
    issue_catalog: dict[str, Any],
) -> None:
    validate_report(report)
    if (
        report["source_integrity"].get("verified") is not True
        or not isinstance(report["source_integrity"].get("contract_digest"), str)
    ):
        raise ContractError("Staging report source integrity is incomplete")
    if report["profile"] != "staging" or report["status"] not in {"PASS", "FAIL"}:
        raise ContractError("Promotion requires a complete staging PASS or FAIL report")
    baseline = report["baseline"]
    if (
        len(baseline) != 5
        or len({item.get("agent") for item in baseline}) != 5
        or any(
            item.get("status") not in {"passed", "not_at_bar"}
            or not isinstance(item.get("insight_count"), int)
            or item["insight_count"] < 0
            or item.get("assessment", {}).get("verdict")
            not in {"clean", "noise", "agent_finding", "inconclusive"}
            or not _baseline_report_semantics_valid(item)
            for item in baseline
        )
    ):
        raise ContractError("Staging report baselines are incomplete")
    issue_by_id = {item["id"]: item for item in issue_catalog["issues"]}
    values = report["issues"]
    if (
        len(values) != 36
        or {item.get("issue_id") for item in values} != set(issue_by_id)
        or any(
            item.get("agent") != issue_by_id[item["issue_id"]]["agent"]
            or item.get("title") != issue_by_id[item["issue_id"]]["title"]
            or item.get("status") in {"inconclusive", "skipped_baseline", None}
            or not isinstance(item.get("observed_count"), int)
            or item.get("result") not in {"PASS", "FAIL"}
            or item.get("detail")
            not in {
                "MATCHED",
                "PARTIAL",
                "MISMATCHED",
                "MISSING",
                "NOISE",
                "DUPLICATE",
                "INCOMPLETE",
            }
            or item.get("assessment", {}).get("verdict")
            not in {"correct", "partially_useful", "incorrect", "missing"}
            or item.get("assessment", {}).get("finding_type") != item.get("detail")
            or set(item.get("assessment", {}).get("fields", {})) != REQUIRED_FIELDS
            or (
                item.get("status") == "observed"
                and (
                    item.get("observed_count", 0) < 1
                    or re.fullmatch(
                        r"sha256:[0-9a-f]{64}",
                        str(item.get("evidence_reference") or ""),
                    )
                    is None
                )
            )
            for item in values
        )
    ):
        raise ContractError("Staging report issue results are incomplete")
    _validate_complete_summary(report, expected_count=36, label="Staging")


def _shadow_metric(value: Any) -> str:
    return "N/A" if value is None else f"{float(value):.1f}/100"


def _shadow_markdown(report: dict[str, Any]) -> list[str]:
    shadow = report["summary"].get("shadow_quality_score")
    if (
        report["profile"] not in SHADOW_SCORE_REPORT_PROFILES
        or report["summary"]["incomplete"]
        or not isinstance(shadow, dict)
        or shadow.get("score") is None
    ):
        return []
    components = shadow["components"]
    counts = shadow["counts"]
    diagnostics = ", ".join(
        str(value).replace("_", " ") for value in shadow["gate_failures"]
    )
    return [
        "## Staging shadow calibration",
        "",
        f"`{shadow['formula']}` is report-only and has no status, promotion, "
        "or automation authority.",
        "",
        "| Shadow metric | Value |",
        "| --- | ---: |",
        f"| Total | {_shadow_metric(shadow['score'])} |",
        f"| Coverage | {_shadow_metric(components['coverage'])} |",
        "| Diagnosis recall | "
        f"{_shadow_metric(components['diagnosis_recall'])} |",
        "| Selected-card quality | "
        f"{_shadow_metric(components['selected_card_quality'])} |",
        "| Useful coverage | "
        f"{_shadow_metric(components['useful_coverage'])} |",
        f"| Precision | {_shadow_metric(components['precision'])} |",
        f"| Expected / detected issues | {counts['expected_issues']} / "
        f"{counts['detected_issues']} |",
        f"| Generated issue / baseline noise cards | "
        f"{counts['generated_issue_cards']} / "
        f"{counts['baseline_noise_cards']} |",
        f"| Below-target diagnostics | {diagnostics or 'None'} |",
        "",
    ]


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    score = (
        "N/A"
        if summary["quality_score"] is None
        else f"{summary['quality_score']:g}/100"
    )
    field_quality = (
        "N/A"
        if summary["field_quality_score"] is None
        else f"{summary['field_quality_score']:g}/100"
    )
    clean_precision = (
        "N/A"
        if summary["clean_card_precision"] is None
        else f"{summary['clean_card_precision']:g}/100"
    )
    comparison = _score_comparison_text(report)
    score_label = (
        "**INCOMPLETE**"
        if report["status"] == "INCOMPLETE"
        else "Quality score"
    )
    lines = [
        f"# Agent Insights Quality - {report['report_date']}",
        "",
        "## Summary",
        "",
        "| Metric | Findings |",
        "| --- | --- |",
        f"| {score_label} | Score **{score}**{comparison} (quality threshold "
        f"{summary['quality_threshold']}/100); "
        f"{summary['issues_correct']} matched, {summary['issues_partial']} partial, "
        f"{summary['noise_cards']} noise cards |",
        f"| Expected issue Insights | {summary['issues_expected']} |",
        "| Expected baseline Insights | 0 |",
        f"| Observed cards | {summary['observed_cards']} |",
        "",
        f"`{summary['quality_score_formula']}`: "
        f"field quality `{field_quality}` at 85%; "
        f"clean-card precision `{clean_precision}` at 15%.",
        "",
        *_shadow_markdown(report),
        "## What is working",
        "",
        "| Capability | Evidence |",
        "| --- | --- |",
        f"| Baseline health | {summary['baseline_passed']} of 5 Agents produced zero baseline Insights |",
        f"| Exact issue quality | {summary['issues_correct']} of {summary['issues_expected']} selected issues passed every field |",
        f"| Noise | {summary['noise_cards']} false-positive, unrelated, or duplicate cards |",
        "",
        "## Baseline ownership",
        "",
        "| Agent | Cards | Verdict | Ownership |",
        "| --- | ---: | --- | --- |",
        *[
            f"| `{item['agent']}` | {item['insight_count']} | "
            f"`{item['assessment']['verdict']}` | "
            f"`{item['assessment']['ownership']}` |"
            for item in report["baseline"]
        ],
        "",
        "## What needs improvement",
        "",
        "| Issue | Agent | Finding | Ownership |",
        "| --- | --- | --- | --- |",
    ]
    failures = [
        item
        for item in report["issues"]
        if item["status"] != "observed"
        or item["assessment"]["verdict"] != "correct"
        or not all(item["assessment"]["fields"].values())
    ]
    if failures:
        for item in failures:
            lines.append(
                f"| `{item['issue_id']}` - {item['title']} | `{item['agent']}` | "
                f"{_evaluation_label(item['detail'])} |"
                f" `{item['assessment']['ownership']}` |"
            )
    else:
        lines.append("| None | - | All selected issues met the strict contract | `none` |")
    lines.extend(
        [
            "",
            "## Human validation",
            "",
            "| Issue | Agent | Cards | Sol verdict | Ownership | Confidence |",
            "| --- | --- | ---: | --- | --- | ---: |",
        ]
    )
    for item in report["issues"]:
        lines.append(
            f"| `{item['issue_id']}` - {item['title']} | `{item['agent']}` | "
            f"{item['observed_count']} | {_evaluation_label(item['detail'])} | "
            f"`{item['assessment']['ownership']}` | "
            f"{item['assessment']['confidence']:.2f} |"
        )
    lines.extend(["", "## Per-Agent reports", ""])
    baseline_by_agent = {item["agent"]: item for item in report["baseline"]}
    for agent_name in sorted(baseline_by_agent, key=str.casefold):
        lines.append(f"- [{agent_name}](agents/{agent_name}.md)")
    lines.append("")
    return "\n".join(lines)


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _evaluation_label(value: str) -> str:
    return {
        "MATCHED": "Correct",
        "PARTIAL": "Partially Correct",
        "MISMATCHED": "Incorrect",
        "MISSING": "Missing",
        "NOISE": "Noise",
        "DUPLICATE": "Duplicate",
        "INCOMPLETE": "Incomplete",
        "noise": "Noise",
        "incomplete": "Incomplete",
        "valid_agent_finding": "Valid Agent Finding",
        "clean": "Clean",
        "inconclusive": "Incomplete",
    }[value]


def _field_result_cells(fields: dict[str, Any] | None) -> tuple[str, str]:
    if not fields:
        return "-", "-"
    passing = [
        field.replace("_", " ")
        for field in FIELD_WEIGHTS
        if fields.get(field) is True
    ]
    failing = [
        field.replace("_", " ")
        for field in FIELD_WEIGHTS
        if fields.get(field) is not True
    ]
    return ", ".join(passing) or "None", ", ".join(failing) or "None"


def render_agent_markdown(report: dict[str, Any], agent_name: str) -> str:
    baseline = next(
        item for item in report["baseline"] if item["agent"] == agent_name
    )
    issues = [item for item in report["issues"] if item["agent"] == agent_name]
    baseline_cards = baseline["assessment"].get("card_evaluations", [])
    issue_cards = [
        card
        for item in issues
        for card in item["assessment"].get("card_evaluations", [])
    ]
    evaluation_counts = Counter(
        _evaluation_label(card["finding_type"]) for card in issue_cards
    )
    missing_count = sum(
        not item["assessment"].get("card_evaluations") for item in issues
    )
    score = (
        "N/A"
        if report["summary"]["quality_score"] is None
        else f"{report['summary']['quality_score']:g}/100"
    )
    lines = [
        f"# {agent_name} - Insight Evaluation",
        "",
        f"- Report date: `{report['report_date']}`",
        f"- Run: `{report['run_id']}`",
        f"- Quality score: **{score}**{_score_comparison_text(report)}",
        *(
            ["- Run status: **INCOMPLETE**"]
            if report["status"] == "INCOMPLETE"
            else []
        ),
        "",
        "## Review summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Expected issue Insights | {len(issues)} |",
        f"| Generated issue cards | {len(issue_cards)} |",
        "| Expected baseline Insights | 0 |",
        f"| Generated baseline cards | {len(baseline_cards)} |",
        f"| Correct | {evaluation_counts['Correct']} |",
        f"| Partially Correct | {evaluation_counts['Partially Correct']} |",
        f"| Incorrect | {evaluation_counts['Incorrect']} |",
        f"| Noise | {evaluation_counts['Noise']} |",
        f"| Duplicate | {evaluation_counts['Duplicate']} |",
        f"| Missing expected issues | {missing_count} |",
        f"| Incomplete card evaluations | {evaluation_counts['Incomplete']} |",
        "",
        "## Evaluation guide",
        "",
        "- **Correct:** the card matches the expected issue and every required field.",
        "- **Partially Correct:** the card is useful and related, but one or more fields are wrong.",
        "- **Incorrect:** the card is related but materially misstates the issue.",
        "- **Noise:** the card is unrelated or a false positive.",
        "- **Duplicate:** an extra card represents an expected root already covered by another card.",
        "- **Missing:** no generated card represents the expected issue.",
        "- **Incomplete:** available evidence cannot support a reliable card judgment.",
        "",
        "## Insight-level evaluation",
        "",
        "| Issue | Foundry version | Generated Insight | Evaluation | Passing fields | Failing fields |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    if baseline_cards:
        for card in baseline_cards:
            passing, failing = _field_result_cells(None)
            lines.append(
                f"| `v0` | `{baseline['foundry_version']}` | "
                f"{_markdown_cell(card['title'])} | "
                f"{_evaluation_label(card['evaluation'])} | {passing} | {failing} |"
            )
    else:
        lines.append(
            f"| `v0` | `{baseline['foundry_version']}` | "
            "No generated Insight | Correct | - | - |"
        )
    for item in issues:
        issue_link = (
            "https://github.com/ninghu/agent-insights-quality/blob/main/"
            f"ISSUE_CATALOG.md#{item['issue_id']}"
        )
        cards = item["assessment"].get("card_evaluations", [])
        if cards:
            for card in cards:
                passing, failing = _field_result_cells(card["fields"])
                lines.append(
                    f"| [{item['issue_id']}]({issue_link}) | "
                    f"`{item['foundry_version']}` | "
                    f"{_markdown_cell(card['title'])} | "
                    f"{_evaluation_label(card['finding_type'])} | "
                    f"{passing} | {failing} |"
                )
        else:
            lines.append(
                f"| [{item['issue_id']}]({issue_link}) | "
                f"`{item['foundry_version']}` | No generated Insight | "
                f"{_evaluation_label(item['detail'])} | - | - |"
            )
    lines.extend(
        [
            "",
            "## Human validation checklist",
            "",
            "- [ ] Confirm each `issue-NNN` links to the intended reviewed defect.",
            "- [ ] Confirm the Foundry version matches the version under review.",
            "- [ ] Compare every generated card with the linked issue definition.",
            "- [ ] Confirm extra cards are correctly labeled Noise or Duplicate.",
            "- [ ] Confirm every expected issue without a card is labeled Missing.",
            "- [ ] Open the Agent from the email and inspect linked traces for disputed cards.",
            "- [ ] Record reviewer agree/disagree decisions outside this generated report.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(report: dict[str, Any], output: Path) -> None:
    validate_report(report)
    atomic_json(output / "report.json", report)
    (output / "report.md").write_text(
        render_markdown(report),
        encoding="utf-8",
        newline="\n",
    )
    agents_root = output / "agents"
    agents_root.mkdir(parents=True, exist_ok=True)
    for agent_name in sorted(
        (item["agent"] for item in report["baseline"]),
        key=str.casefold,
    ):
        (agents_root / f"{agent_name}.md").write_text(
            render_agent_markdown(report, agent_name),
            encoding="utf-8",
            newline="\n",
        )


def apply_score_comparison(report: dict[str, Any], trend_path: Path) -> None:
    trend = (
        read_json(trend_path)
        if trend_path.exists()
        else {"schema_version": "1.0.0", "days": []}
    )
    report["score_comparison"] = score_comparison(report, trend)


def apply_staging_score_comparison(
    report: dict[str, Any],
    receipts_root: Path,
) -> None:
    report["score_comparison"] = None
    score = report["summary"]["quality_score"]
    if report["profile"] != "staging" or score is None:
        return
    current = _staging_run_key(report["run_id"])
    candidates = []
    for path in receipts_root.glob("aiq-*.json"):
        match = re.fullmatch(r"aiq-([0-9]{8})(?:-r([0-9]{2,}))?\.json", path.name)
        if match is None:
            continue
        value = read_json(path)
        previous_score = value.get("quality_score")
        if (
            value.get("profile") != "staging"
            or value.get("qualified") is not True
            or value.get("human_reviewed") is not True
            or isinstance(previous_score, bool)
            or not isinstance(previous_score, (int, float))
            or not 0 <= previous_score <= 100
        ):
            raise ContractError("Staging score history contains an invalid receipt")
        run_id = path.stem
        key = _staging_run_key(run_id)
        if key < current:
            candidates.append((key, run_id, previous_score))
    if not candidates:
        return
    key, run_id, previous_score = max(candidates)
    delta = round(float(score) - float(previous_score), 1)
    report["score_comparison"] = {
        "report_date": key[0],
        "run_id": run_id,
        "quality_score": previous_score,
        "delta": 0 if delta == 0 else delta,
    }


def _staging_run_key(run_id: str) -> tuple[str, int]:
    match = re.fullmatch(r"aiq-([0-9]{8})(?:-r([0-9]{2,}))?", run_id)
    if match is None:
        raise ContractError("Staging run identity is invalid")
    raw_date = match.group(1)
    return (
        f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}",
        int(match.group(2) or 0),
    )


def score_comparison(
    report: dict[str, Any],
    trend: dict[str, Any],
) -> dict[str, Any] | None:
    if report["profile"] != "daily" or report["summary"]["quality_score"] is None:
        return None
    days = trend.get("days")
    if not isinstance(days, list):
        raise ContractError("Trend history has an invalid days collection")
    candidates = []
    for value in days:
        if not isinstance(value, dict):
            raise ContractError("Trend history contains an invalid day")
        report_date = value.get("report_date")
        quality_score = value.get("quality_score")
        if quality_score is None:
            continue
        if (
            not isinstance(report_date, str)
            or isinstance(quality_score, bool)
            or not isinstance(quality_score, (int, float))
            or not 0 <= quality_score <= 100
        ):
            raise ContractError("Trend history contains an invalid scored day")
        if report_date >= report["report_date"]:
            continue
        candidates.append(value)
    if not candidates:
        return None
    previous = max(candidates, key=lambda value: value["report_date"])
    delta = round(
        float(report["summary"]["quality_score"])
        - float(previous["quality_score"]),
        1,
    )
    return {
        "report_date": previous["report_date"],
        "quality_score": previous["quality_score"],
        "delta": 0 if delta == 0 else delta,
    }


def _score_comparison_text(report: dict[str, Any]) -> str:
    comparison = report.get("score_comparison")
    if not isinstance(comparison, dict):
        return " (change N/A)"
    delta = comparison["delta"]
    sign = "+" if delta > 0 else ""
    reference = comparison.get("run_id") or comparison["report_date"]
    return f" ({sign}{delta:g} vs {reference})"


def update_trend(report: dict[str, Any], path: Path) -> None:
    if path.exists():
        trend = read_json(path)
    else:
        trend = {"schema_version": "1.0.0", "days": []}
    atomic_json(path, updated_trend(report, trend))


def updated_trend(
    report: dict[str, Any],
    trend: dict[str, Any],
) -> dict[str, Any]:
    days_value = trend.get("days")
    if not isinstance(days_value, list) or any(
        not isinstance(value, dict)
        or not isinstance(value.get("report_date"), str)
        for value in days_value
    ):
        raise ContractError("Trend history contains an invalid day")
    current = {
        "report_date": report["report_date"],
        "status": report["status"],
        "baseline_passed": report["summary"]["baseline_passed"],
        "issues_correct": report["summary"]["issues_correct"],
        "issues_expected": report["summary"]["issues_expected"],
        "quality_score": report["summary"]["quality_score"],
    }
    existing = [
        value
        for value in days_value
        if value["report_date"] == report["report_date"]
    ]
    if len(existing) > 1:
        raise ContractError("Trend history contains duplicate report dates")
    if existing and existing[0] != current and existing[0].get(
        "quality_score"
    ) is not None:
        raise ContractError("A scored trend day is immutable")
    days = [
        value
        for value in days_value
        if value["report_date"] != report["report_date"]
    ]
    days.append(current)
    days.sort(key=lambda value: value["report_date"])
    return {"schema_version": "1.0.0", "days": days[-90:]}
