from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from pathlib import Path
import re
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from agent_insights_quality.scoring import (
    ASSESSMENT_FIELDS,
    ATTRIBUTABLE_FINDING_TYPES,
    QUALITY_SCORE_FORMULA,
    calculate_quality_score,
    issue_outcome,
    scoring_fields_pass,
)
from agent_insights_quality.selection import (
    DAILY_ISSUE_COUNT,
    DAILY_ISSUES_PER_AGENT,
    STAGING_ISSUE_COUNT,
)
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    atomic_json,
    atomic_text,
    read_json,
)
from agent_insights_quality.azure_regions import (
    location_display_name,
    regions_match,
)

REQUIRED_FIELDS = set(ASSESSMENT_FIELDS)
_QUALITY_SCORE_DOC_URL = (
    "https://github.com/ninghu/agent-insights-quality/blob/main/"
    "docs/QUALITY_BAR.md#quality-score"
)

def resolve_test_region(
    live_location: Any,
    registry_location: Any = None,
    *,
    location_metadata: dict[str, str] | None = None,
) -> str:
    """Validate the ARM-derived canonical region carried by the run manifest."""
    if location_metadata is not None:
        canonical = location_display_name(
            str(live_location or ""),
            [
                {"name": name, "displayName": display_name}
                for name, display_name in location_metadata.items()
            ],
        )
    else:
        canonical = str(live_location or "").strip()
    if not canonical:
        raise ContractError(
            "Report test_region requires a live read-only ARM GET of the "
            "Foundry Project location; none was provided, and a registry, "
            "manifest, or config value cannot supply or fall back for it"
        )
    if re.fullmatch(r"[A-Z][A-Za-z]*[0-9]*", canonical) is None:
        raise ContractError(
            "Report test_region must be the canonical display resolved from "
            "live Azure location metadata"
        )
    if registry_location is not None and not regions_match(
        canonical,
        registry_location,
    ):
        raise ContractError(
            "Report test_region live Foundry Project location does not "
            "match the registry/manifest/config cross-check value"
        )
    return canonical


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
        trace_results = summary.get("trace_assertion_results")
        if (
            not isinstance(trace_results, list)
            or not all(isinstance(item, dict) for item in trace_results)
            or len(trace_results) != summary.get("trace_assertion_count")
            or sum(item.get("passed") is True for item in trace_results)
            != summary.get("trace_assertions_passed")
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
                item.get("semantic_assertion_count", 0)
                + item.get("trace_assertion_count", 0)
                > 0
                and item.get("semantic_assertions_passed")
                == item.get("semantic_assertion_count")
                and item.get("trace_assertions_passed")
                == item.get("trace_assertion_count")
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
            int(item.get("semantic_assertion_count") or 0)
            + int(item.get("trace_assertion_count") or 0)
            for item in gates
        ),
        "assertions_passed": sum(
            int(item.get("semantic_assertions_passed") or 0)
            + int(item.get("trace_assertions_passed") or 0)
            for item in gates
        ),
    }


def _summary_metrics(
    baseline: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    baseline_passed = sum(
        item["status"] == "passed"
        and item["insight_count"] == 0
        and item["assessment"]["verdict"] == "clean"
        for item in baseline
    )
    issues_correct = sum(item["outcome"] == "correct" for item in issues)
    issues_incorrect = sum(item["outcome"] == "incorrect" for item in issues)
    issues_missing = sum(item["outcome"] == "missing" for item in issues)
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
            card.get("finding_type") == "NOISE"
            for card in item["assessment"].get("card_evaluations", [])
        )
        if "card_evaluations" in item["assessment"]
        else item["observed_count"]
        if item["detail"] in {"NOISE", "DUPLICATE"}
        else 0
        for item in issues
    )
    duplicate_cards = sum(
        sum(
            card.get("finding_type") == "DUPLICATE"
            for card in item["assessment"].get("card_evaluations", [])
        )
        for item in issues
    )
    quality_score = calculate_quality_score(
        correct_issues=issues_correct,
        expected_issues=len(issues),
        noise_cards=noise_cards,
        duplicate_cards=duplicate_cards,
    )
    return {
        "baseline_passed": baseline_passed,
        "issues_expected": len(issues),
        "issues_correct": issues_correct,
        "issues_incorrect": issues_incorrect,
        "issues_missing": issues_missing,
        "noise_cards": noise_cards,
        "duplicate_cards": duplicate_cards,
        "quality_score": quality_score,
        "quality_score_formula": QUALITY_SCORE_FORMULA,
    }


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
            complete = value["status"] not in {
                "inconclusive",
                "skipped_baseline",
            } and runtime_complete and assessment["finding_type"] != "INCOMPLETE"
            if not complete:
                incomplete = True
            outcome = issue_outcome(assessment["card_evaluations"])
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
                    "outcome": outcome,
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
                        "reasoning": assessment["reasoning"],
                        "card_evaluations": assessment["card_evaluations"],
                    },
                    "evidence_reference": value.get("evidence_reference"),
                }
            )
    if incomplete:
        raise ContractError(
            "Qualification evidence is incomplete; no quality report was produced"
        )
    summary = _summary_metrics(baseline, results)
    report = {
        "schema_version": "3.0.0",
        "report_date": manifest["report_date"],
        "run_id": manifest["run_id"],
        "profile": manifest["profile"],
        "manifest_reference": manifest["manifest_hash"],
        "catalog_hashes": manifest["catalog_hashes"],
        "source_integrity": manifest["source_integrity"],
        "test_region": resolve_test_region(
            manifest.get("test_region"),
            manifest.get("test_region_registry"),
        ),
        "baseline": baseline,
        "issues": results,
        "summary": summary,
        "delivery": {"content_digest": "sha256:" + "0" * 64},
    }
    return report


def _validate_embedded_assessment_cards(report: Mapping[str, Any]) -> None:
    issue_schema = read_json(ROOT / "schemas" / "assessment.schema.json")[
        "properties"
    ]["card_evaluations"]["items"]
    baseline_schema = read_json(
        ROOT / "schemas" / "baseline-assessment.schema.json"
    )["properties"]["card_evaluations"]["items"]
    for item, schema in [
        *((item, baseline_schema) for item in report["baseline"]),
        *((item, issue_schema) for item in report["issues"]),
    ]:
        for card in item["assessment"].get("card_evaluations", []):
            errors = list(Draft202012Validator(schema).iter_errors(card))
            if errors:
                raise ContractError(
                    "Report assessment card is invalid: " + errors[0].message
                )
    for item in report["issues"]:
        cards = item["assessment"].get("card_evaluations", [])
        if item["observed_count"] != len(cards):
            raise ContractError(
                "Report issue card evaluations must cover every observed card"
            )
        references = [card["reference"] for card in cards]
        if len(references) != len(set(references)):
            raise ContractError(
                "Report issue card references must be unique within an assessment"
            )
        reference_types = {
            card["reference"]: card["finding_type"] for card in cards
        }
        for card in cards:
            if card["finding_type"] in ATTRIBUTABLE_FINDING_TYPES:
                failed_fields = {
                    field
                    for field, passed in card["fields"].items()
                    if passed is False
                }
                if failed_fields and set(card.get("field_reasons", {})) != failed_fields:
                    raise ContractError(
                        "Report attributable card requires a reason for "
                        "exactly each failed field"
                    )
            if card["finding_type"] == "DUPLICATE":
                primary = card.get("duplicate_of")
                if (
                    not isinstance(primary, str)
                    or primary == card["reference"]
                    or reference_types.get(primary)
                    not in {"MATCHED", "PARTIAL", "MISMATCHED"}
                ):
                    raise ContractError(
                        "Report DUPLICATE card must reference another "
                        "attributable card in the same assessment"
                    )
    for item in report["baseline"]:
        if item["insight_count"] != len(
            item["assessment"].get("card_evaluations", [])
        ):
            raise ContractError(
                "Report baseline card evaluations must cover every observed card"
            )


_FORBIDDEN_PUBLIC_REPORT_KEYS = {
    "azure_id",
    "azure_resource_id",
    "private_context",
    "prompt",
    "prompt_payload",
    "provider_id",
    "provider_ids",
    "raw_trace",
    "raw_traces",
    "response_body",
    "response_payload",
}


def _validate_public_report_content(value: Any, path: str = "report") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).casefold()
            if normalized in _FORBIDDEN_PUBLIC_REPORT_KEYS:
                raise ContractError(
                    f"Public report contains forbidden nested field at {path}.{key}"
                )
            _validate_public_report_content(child, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_public_report_content(child, f"{path}[{index}]")
        return
    if isinstance(value, str) and "/subscriptions/" in value.casefold():
        raise ContractError(
            f"Public report contains a private Azure resource identifier at {path}"
        )


def validate_report(report: dict[str, Any]) -> None:
    schema = read_json(ROOT / "schemas" / "report.schema.json")
    errors = list(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(report)
    )
    if errors:
        raise ContractError(
            f"Report is invalid or incomplete: {errors[0].message}"
        )
    _validate_embedded_assessment_cards(report)
    _validate_public_report_content(report)
    source_integrity = report["source_integrity"]
    if (
        source_integrity.get("verified") is not True
        or not isinstance(source_integrity.get("contract_digest"), str)
    ):
        raise ContractError("Report source integrity is incomplete")
    if len(report["issues"]) not in {DAILY_ISSUE_COUNT, STAGING_ISSUE_COUNT}:
        raise ContractError(
            "A report must contain the daily 20 or staging 36 issues"
        )
    if any(
        not isinstance(item, dict)
        or item.get("runtime_evidence_complete") is not True
        for item in [*report["baseline"], *report["issues"]]
    ):
        raise ContractError("Report runtime evidence is incomplete")


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
        or item["assessment"]["finding_type"] == "INCOMPLETE"
        for item in report["issues"]
    )
    if assessment_incomplete:
        raise ContractError(f"{label} report is incomplete")
    if any(
        item["outcome"]
        != issue_outcome(item["assessment"].get("card_evaluations", []))
        for item in report["issues"]
    ):
        raise ContractError(f"{label} report issue outcomes are inconsistent")
    expected = _summary_metrics(report["baseline"], report["issues"])
    summary = report["summary"]
    if (
        expected["issues_expected"] != expected_count
        or any(summary.get(key) != value for key, value in expected.items())
        or set(summary) != set(expected)
    ):
        raise ContractError(f"{label} report summary is inconsistent")


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
    if report["profile"] != "daily":
        raise ContractError("Published report has an ineligible profile")
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
        len(issues) != DAILY_ISSUE_COUNT
        or len({item.get("issue_id") for item in issues}) != DAILY_ISSUE_COUNT
        or any(
            item.get("status") in {"inconclusive", "skipped_baseline", None}
            or not isinstance(item.get("observed_count"), int)
            or item.get("outcome") not in {"correct", "incorrect", "missing"}
            or item.get("detail")
            not in {
                "MATCHED",
                "PARTIAL",
                "MISMATCHED",
                "MISSING",
                "NOISE",
                "DUPLICATE",
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
        } != {agent: DAILY_ISSUES_PER_AGENT for agent in expected_agents}:
            raise ContractError("Published report must contain four issues per Agent")
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
    _validate_complete_summary(
        report,
        expected_count=DAILY_ISSUE_COUNT,
        label="Published",
    )
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
    if report["profile"] != "staging":
        raise ContractError("Promotion requires a complete staging report")
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
            or item.get("outcome") not in {"correct", "incorrect", "missing"}
            or item.get("detail")
            not in {
                "MATCHED",
                "PARTIAL",
                "MISMATCHED",
                "MISSING",
                "NOISE",
                "DUPLICATE",
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


def render_markdown(
    report: dict[str, Any],
    *,
    include_improvement_link: bool = True,
) -> str:
    summary = report["summary"]
    score = f"{summary['quality_score']:g} / 100"
    comparison = _score_comparison_text(report)
    lines = [
        f"# Agent Insights Quality - {report['report_date']}",
        "",
        "## Summary",
        "",
        "| Summary | Result |",
        "| --- | --- |",
        f"| Quality score | **{score}{comparison}** |",
        f"| Expected issues | {summary['issues_correct']} correct / "
        f"{summary['issues_expected']} "
        f"({summary['issues_incorrect']} incorrect, "
        f"{summary['issues_missing']} missing) |",
        f"| Extra cards | {summary['noise_cards']} noise, "
        f"{summary['duplicate_cards']} duplicate |",
        f"| Scoring | [How Scoring Works]({_QUALITY_SCORE_DOC_URL}) |",
        "",
        "## What is working",
        "",
        "| Capability | Evidence |",
        "| --- | --- |",
        f"| Baseline health | {summary['baseline_passed']} of 5 Agents produced zero baseline Insights |",
        f"| Issue quality | {summary['issues_correct']} of {summary['issues_expected']} selected issues passed every scoring field |",
        f"| Extra cards | {summary['noise_cards']} noise and {summary['duplicate_cards']} duplicate cards |",
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
        if item["outcome"] != "correct"
    ]
    if failures:
        for item in failures:
            lines.append(
                f"| `{item['issue_id']}` - {item['title']} | `{item['agent']}` | "
                f"{item['outcome'].title()} |"
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
            f"{item['observed_count']} | {item['outcome'].title()} | "
            f"`{item['assessment']['ownership']}` | "
            f"{item['assessment']['confidence']:.2f} |"
        )
    lines.extend(["", "## Per-Agent reports", ""])
    baseline_by_agent = {item["agent"]: item for item in report["baseline"]}
    for agent_name in sorted(baseline_by_agent, key=str.casefold):
        lines.append(f"- [{agent_name}](agents/{agent_name}.md)")
    lines.append("")
    if report["profile"] == "daily" and include_improvement_link:
        lines.extend(
            [
                "[View Insight Engine Improvement Report]"
                "(../../../../insight-engine-improvement.md)",
                "",
            ]
        )
    return "\n".join(lines)


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _evaluation_label(value: str) -> str:
    return {
        "MATCHED": "Correct",
        "PARTIAL": "Incorrect",
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
        "agent_finding": "Agent Finding",
    }[value]


def _field_result_cells(fields: dict[str, Any] | None) -> tuple[str, str]:
    if not fields:
        return "-", "-"
    passing = [
        field.replace("_", " ")
        for field in ASSESSMENT_FIELDS
        if fields.get(field) is True
    ]
    failing = [
        field.replace("_", " ")
        for field in ASSESSMENT_FIELDS
        if fields.get(field) is not True
    ]
    return ", ".join(passing) or "None", ", ".join(failing) or "None"


COVERAGE_PRIMARY_TYPES = ATTRIBUTABLE_FINDING_TYPES


def _issue_link(issue_id: str) -> str:
    return (
        "https://github.com/ninghu/agent-insights-quality/blob/main/"
        f"ISSUE_CATALOG.md#{issue_id}"
    )


def issue_primary_card(item: Mapping[str, Any]) -> dict[str, Any] | None:
    if item.get("runtime_evidence_complete") is False:
        return None
    cards = [
        card
        for card in item["assessment"].get("card_evaluations", [])
        if card.get("finding_type") in ATTRIBUTABLE_FINDING_TYPES
    ]
    if not cards:
        return None
    return min(
        cards,
        key=lambda card: (
            not scoring_fields_pass(card.get("fields", {})),
            -sum(value is True for value in card.get("fields", {}).values()),
            str(card.get("reference") or ""),
        ),
    )


def expected_issue_coverage_label(item: Mapping[str, Any]) -> str:
    return str(item["outcome"]).title()


def _agent_runtime_evidence_complete(
    baseline: dict[str, Any],
    issues: list[dict[str, Any]],
) -> bool:
    baseline_cards = baseline["assessment"].get("card_evaluations", [])
    return (
        bool(baseline["runtime_evidence_complete"])
        and baseline["assessment"]["verdict"] != "inconclusive"
        and not any(card.get("evaluation") == "incomplete" for card in baseline_cards)
        and all(item.get("runtime_evidence_complete") is True for item in issues)
    )


def _extra_insight_rows(
    baseline: dict[str, Any],
    issues: list[dict[str, Any]],
) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for card in baseline["assessment"].get("card_evaluations", []):
        if card.get("evaluation") == "valid_agent_finding":
            continue
        rows.append(
            (
                f"Baseline `v0` / `{baseline['foundry_version']}`",
                card.get("title", "Untitled Insight"),
                _evaluation_label(card.get("evaluation", "incomplete")),
                card.get("reasoning") or card.get("ownership_reason", ""),
            )
        )
    for item in issues:
        primary = issue_primary_card(item)
        cards = item["assessment"].get("card_evaluations", [])
        cards_by_reference = {
            card["reference"]: card for card in cards if "reference" in card
        }
        observed_in = f"`{item['issue_id']}` version / `{item['foundry_version']}`"
        for card in cards:
            if primary is not None and card is primary:
                continue
            if card.get("finding_type") == "DUPLICATE":
                primary_card = cards_by_reference.get(card.get("duplicate_of"))
                primary_title = (
                    primary_card["title"] if primary_card else "the primary card"
                )
                relationship = f"Duplicate of **{primary_title}**."
                reason = card.get("reasoning") or card.get(
                    "ownership_reason", ""
                )
                if reason:
                    relationship += f" {reason}"
            elif card.get("finding_type") in COVERAGE_PRIMARY_TYPES:
                relationship = (
                    f"Additional attributable card for `{item['issue_id']}`; "
                    "not selected as the primary. "
                    + (
                        card.get("reasoning")
                        or card.get("ownership_reason", "")
                    )
                )
            else:
                relationship = card.get("reasoning") or card.get(
                    "ownership_reason", ""
                )
            rows.append(
                (
                    observed_in,
                    card.get("title", "Untitled Insight"),
                    _evaluation_label(card["finding_type"]),
                    relationship,
                )
            )
    return rows


def _decision_detail_blocks(
    baseline: dict[str, Any],
    issues: list[dict[str, Any]],
) -> list[str]:
    blocks: list[str] = []
    baseline_assessment = baseline["assessment"]
    if baseline_assessment["verdict"] != "clean":
        lines = [
            f"### Baseline `v0` - {_evaluation_label(baseline_assessment['verdict'])}",
            "",
            f"**Ownership:** `{baseline_assessment['ownership']}`  ",
            f"**Why this judgment:** {baseline_assessment['ownership_reason']}",
            "",
        ]
        for card in baseline_assessment.get("card_evaluations", []):
            lines.append(
                f"- **{card.get('title', 'Untitled Insight')}** "
                f"({_evaluation_label(card.get('evaluation', 'incomplete'))}): "
                f"{card.get('reasoning') or card.get('ownership_reason', '')}"
            )
        lines.append("")
        blocks.append("\n".join(lines))

    for item in issues:
        label = expected_issue_coverage_label(item)
        cards = item["assessment"].get("card_evaluations", [])
        issue_link = _issue_link(item["issue_id"])
        reasoning = item["assessment"].get("reasoning") or "No reasoning provided."
        primary = issue_primary_card(item)
        if label in {"Missing", "Incomplete"}:
            blocks.append(
                "\n".join(
                    [
                        f"### [{item['issue_id']}]({issue_link}) - {label}",
                        "",
                        f"**Expected issue:** {item['title']}",
                        "",
                        f"**Why this judgment:** {reasoning}",
                        "",
                    ]
                )
            )
        for card in cards:
            finding_type = card.get("finding_type")
            failed_fields = {
                field
                for field, passed in card.get("fields", {}).items()
                if passed is False
            }
            if (
                finding_type in ATTRIBUTABLE_FINDING_TYPES
                and failed_fields
            ):
                card_label = (
                    "Correct"
                    if scoring_fields_pass(card.get("fields", {}))
                    else "Incorrect"
                )
                qualifier = "" if card is primary else "Additional card - "
                passing, _unused = _field_result_cells(card.get("fields"))
                reasons = card.get("field_reasons") or {}
                lines = [
                    f"### {qualifier}[{item['issue_id']}]({issue_link}) - {card_label}",
                    "",
                    f"**Expected issue:** {item['title']}  ",
                    f"**Generated Insight:** {card.get('title', 'Untitled Insight')}",
                    "",
                    "**Why this judgment:** "
                    f"{card.get('reasoning') or reasoning}",
                    "",
                    f"**What was correct:** {passing}",
                    "",
                    "| Failing field | Specific reason |",
                    "| --- | --- |",
                ]
                for field in ASSESSMENT_FIELDS:
                    if card.get("fields", {}).get(field) is True:
                        continue
                    reason = reasons.get(field, "No reason provided.")
                    lines.append(
                        f"| {field.replace('_', ' ').title()} | "
                        f"{_markdown_cell(reason)} |"
                    )
                lines.extend(
                    [
                        "",
                        "| Review metadata | Value |",
                        "| --- | --- |",
                        f"| Ownership | `{card.get('ownership', item['assessment']['ownership'])}` |",
                        f"| Confidence | `{card.get('confidence', item['assessment']['confidence']):.2f}` |",
                        "",
                    ]
                )
                blocks.append("\n".join(lines))
            elif finding_type == "INCOMPLETE":
                blocks.append(
                    "\n".join(
                        [
                            f"### Incomplete card - observed in `{item['issue_id']}` version",
                            "",
                            f"**Generated Insight:** {card.get('title', 'Untitled Insight')}",
                            "",
                            "**Why this judgment:** "
                            f"{card.get('reasoning') or card.get('ownership_reason', '')}",
                            "",
                            f"**Ownership:** `{card.get('ownership', '')}`",
                            "",
                        ]
                    )
                )
        for card in cards:
            if card.get("finding_type") != "NOISE":
                continue
            lines = [
                f"### Noise card - observed in `{item['issue_id']}` version",
                "",
                f"**Generated Insight:** {card.get('title', 'Untitled Insight')}",
                "**Corresponding issue:** None",
                "",
                f"**Diagnosis:** {card.get('title', 'Untitled Insight')}",
                "",
                f"**Evidence:** {card.get('reasoning', '')}",
                "",
                f"**Rejected mapping:** `{item['issue_id']}` - {item['title']}",
                "",
                "**Why no reviewed issue corresponds:** "
                f"{card.get('ownership_reason') or card.get('reasoning', '')}",
                "",
                "| Review metadata | Value |",
                "| --- | --- |",
                f"| Ownership | `{card.get('ownership', '')}` |",
                f"| Confidence | `{card.get('confidence', 0):.2f}` |",
                "",
            ]
            blocks.append("\n".join(lines))
        cards_by_reference = {
            card["reference"]: card for card in cards if "reference" in card
        }
        duplicates_by_primary: dict[str, list[dict[str, Any]]] = {}
        for card in cards:
            if card.get("finding_type") == "DUPLICATE":
                duplicates_by_primary.setdefault(
                    card.get("duplicate_of") or "", []
                ).append(card)
        for primary_reference, duplicate_cards in duplicates_by_primary.items():
            primary_card = cards_by_reference.get(primary_reference)
            primary_title = (
                primary_card["title"]
                if primary_card
                else "an unresolved primary card"
            )
            lines = [
                f"### Duplicate group - `{item['issue_id']}`",
                "",
                f"**Primary card:** {primary_title}",
                "",
                "**Duplicate cards:**",
                "",
                *[
                    f"{index}. {card.get('title', 'Untitled Insight')} - "
                    f"{card.get('reasoning') or card.get('ownership_reason', '')}"
                    for index, card in enumerate(duplicate_cards, start=1)
                ],
                "",
            ]
            blocks.append("\n".join(lines))
    return blocks


def _context_paths(agent_name: str, ownership: str, issue_id: str | None) -> str:
    """Deterministic repo-relative catalog/source/traffic paths for ``ownership``.

    Excludes any issue-catalog reference or evaluation anchor phrase; callers
    that have an ``issue_id`` and an anchor phrase compose them with this via
    :func:`_context_cell`.
    """
    if ownership in {"insight_engine", "agent"}:
        if issue_id is None:
            return (
                f"`agents/{agent_name}/v0/source/`; "
                f"`agents/{agent_name}/v0/traffic.json`"
            )
        return (
            f"`agents/{agent_name}/issues/{issue_id}/source/`; "
            f"`agents/{agent_name}/issues/{issue_id}/traffic.json`"
        )
    if ownership == "test_framework":
        return (
            "`src/agent_insights_quality/`; `schemas/`; "
            "`src/agent_insights_quality/prompts/`; `tests/`"
        )
    if ownership == "infrastructure":
        return "`infra/`"
    return "Investigate first; ownership is unresolved."


_CONTEXT_PATH_OWNERSHIPS = {"insight_engine", "agent", "test_framework", "infrastructure"}


def _context_cell(
    agent_name: str,
    ownership: str,
    issue_id: str | None,
    anchor_phrase: str,
) -> str:
    """Build the full ``Context to load`` cell for one coding-agent-context row.

    An ``insight_engine``-owned finding still lists the Test Agent's catalog,
    source, and traffic paths, but only as read-only context to load, never as
    an edit target - the row's Owner column and the section's lead-in
    sentence carry that distinction, not the paths themselves. An unresolved
    (or otherwise unrecognized) ownership renders only the investigate-first
    instruction, since no deterministic path is yet known.
    """
    paths = _context_paths(agent_name, ownership, issue_id)
    if ownership not in _CONTEXT_PATH_OWNERSHIPS:
        return paths
    parts = []
    if issue_id is not None:
        parts.append(f"`ISSUE_CATALOG.md#{issue_id}`")
    parts.append(anchor_phrase)
    parts.append(paths)
    return "; ".join(parts)


def _baseline_context_entry(
    baseline: dict[str, Any],
) -> tuple[str, str, str] | None:
    """Return ``(finding, ownership, anchor_phrase)`` for the Baseline row, or
    ``None`` when the baseline has no actionable finding at all."""
    assessment = baseline["assessment"]
    verdict_non_clean = assessment["verdict"] != "clean"
    extra_cards = [
        card
        for card in assessment.get("card_evaluations", [])
        if card.get("evaluation") != "valid_agent_finding"
    ]
    if not verdict_non_clean and not extra_cards:
        return None
    parts = []
    if verdict_non_clean:
        parts.append(_evaluation_label(assessment["verdict"]))
    if extra_cards:
        parts.append("Noise")
    finding = "Baseline " + " + ".join(parts)
    block_count = (1 if verdict_non_clean else 0) + (1 if extra_cards else 0)
    anchor_phrase = f"Baseline detail{'s' if block_count > 1 else ''} above"
    ownership = (
        assessment["ownership"]
        if verdict_non_clean
        else extra_cards[0].get("ownership", assessment["ownership"])
    )
    return finding, ownership, anchor_phrase


def _issue_context_entries(
    item: dict[str, Any],
) -> list[tuple[str, str, str]]:
    """Return one entry for every distinct actionable owner on an issue."""
    label = expected_issue_coverage_label(item)
    primary = issue_primary_card(item)
    cards = item["assessment"].get("card_evaluations", [])
    by_owner: dict[str, list[tuple[str, str]]] = {}
    if label != "Correct":
        ownership = (
            primary.get("ownership", item["assessment"]["ownership"])
            if primary is not None
            else item["assessment"]["ownership"]
        )
        by_owner.setdefault(ownership, []).append(
            (label, "decision" if label == "Incorrect" else label)
        )
    for card in cards:
        if card is primary or card.get("finding_type") not in {
            "NOISE",
            "DUPLICATE",
        }:
            continue
        ownership = card.get("ownership", item["assessment"]["ownership"])
        card_label = (
            "unmatched Noise"
            if card["finding_type"] == "NOISE"
            else "Duplicate group"
        )
        entry = (
            card_label,
            "Noise" if card["finding_type"] == "NOISE" else "Duplicate",
        )
        owner_entries = by_owner.setdefault(ownership, [])
        if entry not in owner_entries:
            owner_entries.append(entry)
    entries = []
    for ownership, values in sorted(by_owner.items()):
        parts = [value[0] for value in values]
        anchors = [value[1] for value in values]
        entries.append(
            (
                f"`{item['issue_id']}` " + " + ".join(parts),
                ownership,
                (
                    f"{' / '.join(anchors)} "
                    f"detail{'s' if len(anchors) > 1 else ''} above"
                ),
            )
        )
    return entries


def _coding_agent_context_rows(
    baseline: dict[str, Any],
    issues: list[dict[str, Any]],
    agent_name: str,
) -> list[tuple[str, str, str]]:
    """One compact row per actionable non-Correct result, merging an issue's
    own evaluation with any unmatched Noise or Duplicate group generated in
    the same version so a coding agent never has to cross-reference several
    rows for one issue."""
    rows: list[tuple[str, str, str]] = []
    baseline_entry = _baseline_context_entry(baseline)
    if baseline_entry is not None:
        finding, ownership, anchor_phrase = baseline_entry
        rows.append(
            (
                finding,
                f"`{ownership}`",
                _context_cell(agent_name, ownership, None, anchor_phrase),
            )
        )
    for item in issues:
        for finding, ownership, anchor_phrase in _issue_context_entries(item):
            rows.append(
                (
                    finding,
                    f"`{ownership}`",
                    _context_cell(
                        agent_name,
                        ownership,
                        item["issue_id"],
                        anchor_phrase,
                    ),
                )
            )
    return rows


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
    outcome_counts = Counter(item["outcome"] for item in issues)
    noise_count = sum(
        card.get("finding_type") == "NOISE" for card in issue_cards
    ) + sum(card.get("evaluation") == "noise" for card in baseline_cards)
    duplicate_count = sum(
        card.get("finding_type") == "DUPLICATE" for card in issue_cards
    )
    runtime_complete = _agent_runtime_evidence_complete(baseline, issues)
    lines = [
        f"# {agent_name} - Insight Evaluation",
        "",
        f"- Report date: `{report['report_date']}`",
        f"- Run: `{report['run_id']}`",
        f"- Runtime evidence: `{'Complete' if runtime_complete else 'Incomplete'}`",
        "- Expected baseline Insights: `0`",
        "",
        "## Review summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Expected issue Insights | {len(issues)} |",
        f"| Generated issue cards | {len(issue_cards)} |",
        f"| Generated baseline cards | {len(baseline_cards)} |",
        f"| Correct | {outcome_counts['correct']} |",
        f"| Incorrect | {outcome_counts['incorrect']} |",
        f"| Missing | {outcome_counts['missing']} |",
        f"| Noise | {noise_count} |",
        f"| Duplicate | {duplicate_count} |",
        "",
        "## Expected issue coverage",
        "",
        "| Expected issue | Version | Primary Insight | Evaluation | Why |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in issues:
        label = expected_issue_coverage_label(item)
        primary = issue_primary_card(item)
        if primary is not None:
            primary_title = _markdown_cell(primary.get("title", "Untitled Insight"))
        elif label == "Incomplete":
            primary_title = "Insufficient evidence"
        else:
            primary_title = "No matching Insight"
        why = _markdown_cell(
            (
                primary.get("reasoning")
                if primary is not None
                else item["assessment"].get("reasoning")
            )
            or "No reasoning provided."
        )
        lines.append(
            f"| [{item['issue_id']}]({_issue_link(item['issue_id'])}) | "
            f"`{item['foundry_version']}` | {primary_title} | {label} | {why} |"
        )
    lines.extend(
        [
            "",
            "## Extra generated Insights",
            "",
            "`Observed in` identifies the Agent version that produced the card. It "
            "does not assign the card to that version's expected issue.",
            "",
            "| Observed in | Generated Insight | Classification | Relationship |",
            "| --- | --- | --- | --- |",
        ]
    )
    extra_rows = _extra_insight_rows(baseline, issues)
    if extra_rows:
        for observed_in, title, classification, relationship in extra_rows:
            lines.append(
                f"| {observed_in} | {_markdown_cell(title)} | {classification} | "
                f"{_markdown_cell(relationship)} |"
            )
    else:
        lines.append("| None | - | - | No extra generated Insights were observed. |")
    decision_blocks = _decision_detail_blocks(baseline, issues)
    lines.extend(["", "## Decision details", ""])
    if decision_blocks:
        for block in decision_blocks:
            lines.append(block)
    else:
        lines.append(
            "No Incorrect, Noise, Duplicate, Missing, or non-clean baseline "
            "outcomes were observed."
        )
        lines.append("")
    lines.extend(
        [
            "## Evaluation guide",
            "",
            "- **Correct:** the card matches the expected issue and passes title, description, category, and linked traces.",
            "- **Incorrect:** the card is related to the expected issue but fails at least one scoring field.",
            "- **Noise:** the card has no corresponding reviewed issue, or independent evidence disproves its diagnosis.",
            "- **Duplicate:** the card repeats a root already covered by a named primary card and adds no independent root.",
            "- **Missing:** no generated card represents the expected issue.",
            "- **Incomplete:** available evidence cannot support a reliable judgment.",
            "",
            "## Human validation checklist",
            "",
            "- [ ] Every Incorrect card has a specific reason for each failing field.",
            "- [ ] Every Noise card has no issue assignment and explains why no reviewed issue corresponds to it.",
            "- [ ] Every Duplicate group names the primary card and lists every card classified as its duplicate.",
            "- [ ] An issue with only Noise or Duplicate cards is still reported Missing unless a primary card covers it.",
            "- [ ] Baseline findings are checked against independent source, endpoint, and trace proof.",
            "- [ ] Explanations contain only public-safe summaries, never raw traces or complete payloads.",
            "",
            "## Coding-agent context",
            "",
            "When ownership is `insight_engine`, the linked Test Agent source and traffic are "
            "read-only authorities, not edit targets.",
            "",
        ]
    )
    context_rows = _coding_agent_context_rows(baseline, issues, agent_name)
    if context_rows:
        lines.extend(
            [
                "| Finding | Owner | Context to load |",
                "| --- | --- | --- |",
            ]
        )
        for finding, owner, context in context_rows:
            lines.append(f"| {finding} | {owner} | {context} |")
        lines.append("")
    else:
        lines.extend(["No coding-agent action required.", ""])
    return "\n".join(lines)


def write_report(
    report: dict[str, Any],
    output: Path,
    *,
    include_improvement_link: bool = True,
) -> None:
    validate_report(report)
    atomic_json(output / "report.json", report)
    atomic_text(
        output / "report.md",
        render_markdown(
            report,
            include_improvement_link=include_improvement_link,
        ),
    )
    agents_root = output / "agents"
    agents_root.mkdir(parents=True, exist_ok=True)
    for agent_name in sorted(
        (item["agent"] for item in report["baseline"]),
        key=str.casefold,
    ):
        atomic_text(
            agents_root / f"{agent_name}.md",
            render_agent_markdown(report, agent_name),
        )


def apply_score_comparison(report: dict[str, Any], trend_path: Path) -> None:
    trend = (
        read_json(trend_path)
        if trend_path.exists()
        else {
            "schema_version": "2.0.0",
            "quality_score_formula": QUALITY_SCORE_FORMULA,
            "days": [],
        }
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
        if value.get("quality_score_formula") != QUALITY_SCORE_FORMULA:
            continue
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
    if report["profile"] != "daily":
        return None
    if (
        trend.get("schema_version") != "2.0.0"
        or trend.get("quality_score_formula") != QUALITY_SCORE_FORMULA
    ):
        raise ContractError("Trend history uses a different quality-score formula")
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
        trend = {
            "schema_version": "2.0.0",
            "quality_score_formula": QUALITY_SCORE_FORMULA,
            "days": [],
        }
    atomic_json(path, updated_trend(report, trend))


def updated_trend(
    report: dict[str, Any],
    trend: dict[str, Any],
) -> dict[str, Any]:
    days_value = trend.get("days")
    if (
        trend.get("schema_version") != "2.0.0"
        or trend.get("quality_score_formula") != QUALITY_SCORE_FORMULA
        or not isinstance(days_value, list)
        or any(
        not isinstance(value, dict)
        or not isinstance(value.get("report_date"), str)
        for value in days_value
        )
    ):
        raise ContractError("Trend history contains an invalid day")
    current = {
        "report_date": report["report_date"],
        "baseline_passed": report["summary"]["baseline_passed"],
        "issues_correct": report["summary"]["issues_correct"],
        "issues_incorrect": report["summary"]["issues_incorrect"],
        "issues_missing": report["summary"]["issues_missing"],
        "issues_expected": report["summary"]["issues_expected"],
        "noise_cards": report["summary"]["noise_cards"],
        "duplicate_cards": report["summary"]["duplicate_cards"],
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
    return {
        "schema_version": "2.0.0",
        "quality_score_formula": QUALITY_SCORE_FORMULA,
        "days": days[-90:],
    }
