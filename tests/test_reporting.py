from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.reporting import (
    apply_score_comparison,
    apply_staging_score_comparison,
    build_report,
    calculate_quality_score,
    render_agent_markdown,
    render_markdown,
    score_comparison,
    updated_trend,
    validate_report,
    validate_published_report,
)
from agent_insights_quality.util import ContractError
import pytest


def _manifest(*, full: bool = False) -> dict:
    agents, _ = load_catalogs()

    def request_summaries(*, prompt: bool, activation: bool) -> list[dict]:
        return [
            {
                "request_index": index,
                "response_count": 1,
                "usable_response": True,
                "semantic_assertion_count": 1,
                "semantic_assertions_passed": 1,
                "assertion_results": [
                    {"assertion": "synthetic_contract", "passed": True}
                ],
                "trace_assertion_count": 0,
                "trace_assertions_passed": 0,
                "trace_assertion_results": [],
                "activation_gate": activation,
                "direct_terminal_response_count": int(prompt),
                "function_call_count": 0,
            }
            for index in range(5)
        ]

    values = []
    for agent in agents["agents"]:
        selected = agent["issue_ids"] if full else agent["issue_ids"][:4]
        prompt = agent["type"] == "prompt"
        values.append(
            {
                "name": agent["name"],
                "type": agent["type"],
                "baseline_contract": agent["baseline_contract"],
                "baseline": {
                    "foundry_version": "1",
                    "status": "passed",
                    "insight_references": [],
                    "endpoint_request_count": 5,
                    "endpoint_response_count": 5,
                    "endpoint_usable_response_count": 5,
                    "semantic_assertion_count": 5,
                    "semantic_assertions_passed": 5,
                    "trace_assertion_count": 0,
                    "trace_assertions_passed": 0,
                    "trace_contract_verified": True,
                    "trace_behavior_summary": {
                        "operation_count": 5,
                        "tool_call_counts": {},
                        "tool_response_count": 0,
                        "assistant_response_count": 5,
                        "explicit_terminal_success_count": 5,
                        "explicit_terminal_output_count": 5,
                        "terminal_response_count": 5,
                        "terminal_success_count": 5,
                        "terminal_output_count": 5,
                        "handled_error_count": 0,
                        "unhandled_error_count": 0,
                    },
                    "endpoint_request_summaries": request_summaries(
                        prompt=prompt,
                        activation=False,
                    ),
                },
                "issues": [
                    {
                        "issue_id": issue_id,
                        "foundry_version": issue_id,
                        "status": "observed",
                        "insight_references": ["sha256:" + "a" * 64],
                        "evidence_reference": "sha256:" + "b" * 64,
                        "endpoint_request_count": 5,
                        "endpoint_response_count": 5,
                        "endpoint_usable_response_count": 5,
                        "semantic_assertion_count": 5,
                        "semantic_assertions_passed": 5,
                        "trace_assertion_count": 0,
                        "trace_assertions_passed": 0,
                        "trace_contract_verified": True,
                        "trace_behavior_summary": {},
                        "endpoint_request_summaries": request_summaries(
                            prompt=prompt,
                            activation=prompt,
                        ),
                    }
                    for issue_id in selected
                ],
            }
        )
    return {
        "report_date": "2026-08-24",
        "run_id": "aiq-20260824",
        "profile": "staging" if full else "daily",
        "manifest_hash": "sha256:" + "c" * 64,
        "catalog_hashes": {
            "agents": "sha256:" + "d" * 64,
            "issues": "sha256:" + "e" * 64,
            "artifacts": "sha256:" + "f" * 64,
        },
        "source_integrity": {
            "verified": True,
            "contract_digest": "sha256:" + "1" * 64,
        },
        "agents": values,
    }


def _assessments(manifest: dict) -> dict[str, dict]:
    return {
        item["issue_id"]: {
            "issue_id": item["issue_id"],
            "verdict": "correct",
            "confidence": 0.99,
            "ownership": "none",
            "finding_type": "MATCHED",
            "ownership_reason": "The expected Insight is fully correct.",
            "fields": {
                "root_cause": True,
                "title": True,
                "description": True,
                "category": True,
                "severity": True,
                "proposed_fix": True,
                "linked_traces": True,
            },
            "card_evaluations": [],
        }
        for agent in manifest["agents"]
        for item in agent["issues"]
    }


def _with_card_evaluations(
    assessments: dict[str, dict],
) -> dict[str, dict]:
    for index, assessment in enumerate(assessments.values(), start=1):
        assessment["card_evaluations"] = [
            {
                "reference": f"sha256:{index:064x}",
                "finding_type": assessment["finding_type"],
                "fields": deepcopy(assessment["fields"]),
            }
        ]
    return assessments


def _baseline_assessments(manifest: dict) -> dict[str, dict]:
    return {
        agent["name"]: {
            "verdict": "clean",
            "ownership": "none",
            "ownership_reason": "No baseline Insight was observed.",
            "confidence": 0.99,
            "card_evaluations": [],
        }
        for agent in manifest["agents"]
    }


def test_report_status_uses_ninety_point_threshold() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    assessments = _assessments(manifest)
    report = build_report(
        manifest,
        issues,
        assessments,
        _baseline_assessments(manifest),
    )
    assert report["status"] == "PASS"
    changed = deepcopy(assessments)
    for assessment in list(changed.values())[:4]:
        assessment["verdict"] = "incorrect"
        assessment["finding_type"] = "MISMATCHED"
        assessment["fields"] = {
            field: False for field in assessment["fields"]
        }
    failed = build_report(
        manifest,
        issues,
        changed,
        _baseline_assessments(manifest),
    )
    assert failed["summary"]["quality_score"] == 83
    assert failed["status"] == "FAIL"


def test_rejected_foreign_operation_card_does_not_enter_report_scoring() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    assessments = _assessments(manifest)
    target = manifest["agents"][0]["issues"][0]
    target["status"] = "not_at_bar"
    target["error_code"] = "expected_exactly_one_insight"
    target["insight_references"] = []
    target["evidence_reference"] = None
    assessment = assessments[target["issue_id"]]
    assessment["verdict"] = "missing"
    assessment["finding_type"] = "MISSING"
    assessment["fields"] = {
        field: False for field in assessment["fields"]
    }
    assessment["card_evaluations"] = []

    report = build_report(
        manifest,
        issues,
        assessments,
        _baseline_assessments(manifest),
    )
    result = next(
        item for item in report["issues"] if item["issue_id"] == target["issue_id"]
    )

    assert result["observed_count"] == 0
    assert result["detail"] == "MISSING"
    assert report["summary"]["observed_cards"] == (
        report["summary"]["issues_expected"] - 1
    )
    assert report["summary"]["clean_card_precision"] == 100.0


def test_staging_shadow_score_does_not_change_v1_or_daily_reports() -> None:
    _, issues = load_catalogs()
    manifest = _manifest(full=True)
    assessments = _with_card_evaluations(_assessments(manifest))
    first = next(iter(assessments.values()))
    first["verdict"] = "partially_useful"
    first["finding_type"] = "PARTIAL"
    first["fields"]["root_cause"] = False
    first["card_evaluations"][0]["finding_type"] = "PARTIAL"
    first["card_evaluations"][0]["fields"]["root_cause"] = False

    staging = build_report(
        manifest,
        issues,
        assessments,
        _baseline_assessments(manifest),
    )

    assert staging["summary"]["quality_score_formula"] == "field_weighted_v1"
    assert staging["summary"]["quality_score"] == 99.4
    assert staging["status"] == "PASS"
    shadow = staging["summary"]["shadow_quality_score"]
    assert shadow["formula"] == "coverage_quality_precision_v2"
    assert shadow["automation_authority"] is False
    assert shadow["components"] == {
        "coverage": 100.0,
        "diagnosis_recall": 97.2,
        "selected_card_quality": 97.2,
        "useful_coverage": 97.2,
        "precision": 100.0,
    }
    assert shadow["score"] == 97.8
    assert shadow["gate_failures"] == []
    assert staging["issues"][0]["shadow_v2_primary"]["quality"] == 0.0
    validate_report(staging)
    markdown = render_markdown(staging)
    assert "## Staging shadow calibration" in markdown
    assert "`coverage_quality_precision_v2`" in markdown
    assert "| Total | 97.8/100 |" in markdown

    manifest = _manifest()
    assessments = _with_card_evaluations(_assessments(manifest))
    daily = build_report(
        manifest,
        issues,
        assessments,
        _baseline_assessments(manifest),
    )
    assert "shadow_quality_score" not in daily["summary"]
    assert all("shadow_v2_primary" not in item for item in daily["issues"])
    assert "coverage_quality_precision_v2" not in render_markdown(daily)
    validate_report(daily)


def test_incomplete_staging_shadow_keeps_counts_but_nulls_metrics() -> None:
    _, issues = load_catalogs()
    manifest = _manifest(full=True)
    manifest["agents"][0]["issues"][0]["endpoint_usable_response_count"] = 4
    assessments = _with_card_evaluations(_assessments(manifest))

    report = build_report(
        manifest,
        issues,
        assessments,
        _baseline_assessments(manifest),
    )

    assert report["status"] == "INCOMPLETE"
    assert report["summary"]["quality_score"] is None
    shadow = report["summary"]["shadow_quality_score"]
    assert shadow["counts"] == {
        "expected_issues": 36,
        "detected_issues": 35,
        "correct_diagnosis_primaries": 35,
        "generated_issue_cards": 36,
        "baseline_noise_cards": 0,
    }
    assert all(value is None for value in shadow["components"].values())
    assert shadow["score"] is None
    assert shadow["gate_failures"] is None
    assert all(item["shadow_v2_primary"] is None for item in report["issues"])
    assert "coverage_quality_precision_v2" not in render_markdown(report)
    validate_report(report)


def test_failed_matched_issue_cannot_score_from_perfect_card_fields() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    assessments = _assessments(manifest)
    issue_id = next(iter(assessments))
    assessment = assessments[issue_id]
    assessment["fields"]["severity"] = False
    assessment["card_evaluations"] = [
        {
            "finding_type": "MATCHED",
            "fields": {
                field: True for field in assessment["fields"]
            },
        }
    ]
    report = build_report(
        manifest,
        issues,
        assessments,
        _baseline_assessments(manifest),
    )
    result = next(item for item in report["issues"] if item["issue_id"] == issue_id)
    assert result["result"] == "FAIL"
    assert report["summary"]["field_quality_score"] < 100
    assert report["summary"]["quality_score"] < 100


def test_failed_mismatched_issue_cannot_score_from_perfect_fields() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    assessments = _assessments(manifest)
    issue_id = next(iter(assessments))
    assessment = assessments[issue_id]
    assessment["verdict"] = "incorrect"
    assessment["finding_type"] = "MISMATCHED"
    assessment["ownership"] = "insight_engine"
    assessment["card_evaluations"] = [
        {
            "finding_type": "MISMATCHED",
            "fields": {
                field: True for field in assessment["fields"]
            },
        }
    ]
    report = build_report(
        manifest,
        issues,
        assessments,
        _baseline_assessments(manifest),
    )
    result = next(item for item in report["issues"] if item["issue_id"] == issue_id)
    assert result["result"] == "FAIL"
    assert report["summary"]["field_quality_score"] < 100
    assert report["summary"]["quality_score"] < 100


def test_report_requires_bound_source_integrity() -> None:
    _, issues = load_catalogs()
    report = build_report(
        _manifest(),
        issues,
        _assessments(_manifest()),
        _baseline_assessments(_manifest()),
    )
    report.pop("source_integrity")
    with pytest.raises(ContractError, match="Report is invalid"):
        validate_report(report)


def test_report_rejects_superseded_schema() -> None:
    manifest = _manifest()
    _, issues = load_catalogs()
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        _baseline_assessments(manifest),
    )
    report["schema_version"] = "1.0.0"
    with pytest.raises(ContractError, match="Report is invalid"):
        validate_report(report)


def test_daily_report_schema_requires_exactly_20_issues() -> None:
    manifest = _manifest()
    _, issues = load_catalogs()
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        _baseline_assessments(manifest),
    )
    report["issues"].append(deepcopy(report["issues"][0]))
    with pytest.raises(ContractError, match="Report is invalid"):
        validate_report(report)


def test_complete_report_rejects_unverified_source_integrity() -> None:
    manifest = _manifest()
    _, issues = load_catalogs()
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        _baseline_assessments(manifest),
    )
    report["source_integrity"] = {
        "verified": False,
        "contract_digest": None,
    }
    with pytest.raises(ContractError, match="source integrity"):
        validate_report(report)


def test_daily_score_compares_with_latest_prior_scored_report(
    tmp_path: Path,
) -> None:
    _, issues = load_catalogs()
    report = build_report(
        _manifest(),
        issues,
        _assessments(_manifest()),
        _baseline_assessments(_manifest()),
    )
    report["report_date"] = "2026-08-27"
    trend = tmp_path / "trend.json"
    trend.write_text(
        """{
  "schema_version": "1.0.0",
  "days": [
    {"report_date": "2026-08-25", "quality_score": 94.1},
    {"report_date": "2026-08-26", "quality_score": null}
  ]
}
""",
        encoding="utf-8",
    )

    apply_score_comparison(report, trend)

    assert report["score_comparison"] == {
        "report_date": "2026-08-25",
        "quality_score": 94.1,
        "delta": 5.9,
    }
    assert "Score **100/100** (+5.9 vs 2026-08-25)" in render_markdown(report)


def test_incomplete_daily_score_has_no_comparison(tmp_path: Path) -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    manifest["agents"][0]["baseline"]["status"] = "inconclusive"
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        _baseline_assessments(manifest),
    )
    trend = tmp_path / "trend.json"
    trend.write_text(
        '{"schema_version":"1.0.0","days":'
        '[{"report_date":"2026-08-23","quality_score":94.1}]}',
        encoding="utf-8",
    )

    apply_score_comparison(report, trend)

    assert report["score_comparison"] is None


def test_staging_score_compares_with_latest_reviewed_receipt(
    tmp_path: Path,
) -> None:
    _, issues = load_catalogs()
    report = build_report(
        _manifest(),
        issues,
        _assessments(_manifest()),
        _baseline_assessments(_manifest()),
    )
    report["profile"] = "staging"
    report["run_id"] = "aiq-20260827-r34"
    report["summary"]["quality_score"] = 48.3
    receipts = tmp_path / "promotion-receipts"
    receipts.mkdir()
    (receipts / "aiq-20260827-r29.json").write_text(
        """{
  "profile": "staging",
  "qualified": true,
  "human_reviewed": true,
  "quality_score": 47.8
}
""",
        encoding="utf-8",
    )

    apply_staging_score_comparison(report, receipts)

    assert report["score_comparison"] == {
        "report_date": "2026-08-27",
        "run_id": "aiq-20260827-r29",
        "quality_score": 47.8,
        "delta": 0.5,
    }
    assert "(+0.5 vs aiq-20260827-r29)" in render_markdown(report)


def test_staging_score_comparison_accepts_three_digit_reruns(
    tmp_path: Path,
) -> None:
    _, issues = load_catalogs()
    report = build_report(
        _manifest(),
        issues,
        _assessments(_manifest()),
        _baseline_assessments(_manifest()),
    )
    report["profile"] = "staging"
    report["run_id"] = "aiq-20260827-r100"
    receipts = tmp_path / "promotion-receipts"
    receipts.mkdir()
    (receipts / "aiq-20260827-r99.json").write_text(
        '{"profile":"staging","qualified":true,"human_reviewed":true,'
        '"quality_score":47.8}',
        encoding="utf-8",
    )
    apply_staging_score_comparison(report, receipts)
    assert report["score_comparison"]["run_id"] == "aiq-20260827-r99"


def test_score_comparison_uses_immutable_base_trend() -> None:
    _, issues = load_catalogs()
    report = build_report(
        _manifest(),
        issues,
        _assessments(_manifest()),
        _baseline_assessments(_manifest()),
    )
    report["report_date"] = "2026-08-27"
    base = {
        "schema_version": "1.0.0",
        "days": [
            {
                "report_date": "2026-08-26",
                "status": "FAIL",
                "baseline_passed": 1,
                "issues_correct": 4,
                "issues_expected": 20,
                "quality_score": 44.1,
            }
        ],
    }

    assert score_comparison(report, base) == {
        "report_date": "2026-08-26",
        "quality_score": 44.1,
        "delta": 55.9,
    }
    expected = updated_trend(report, base)
    tampered = deepcopy(expected)
    tampered["days"][0]["quality_score"] = 90
    assert tampered != expected


def test_scored_trend_day_cannot_be_replaced() -> None:
    _, issues = load_catalogs()
    report = build_report(
        _manifest(),
        issues,
        _assessments(_manifest()),
        _baseline_assessments(_manifest()),
    )
    base = {
        "schema_version": "1.0.0",
        "days": [
            {
                "report_date": report["report_date"],
                "status": "FAIL",
                "baseline_passed": 1,
                "issues_correct": 4,
                "issues_expected": 20,
                "quality_score": 44.1,
            }
        ],
    }
    with pytest.raises(ContractError, match="immutable"):
        updated_trend(report, base)


def test_field_quality_and_clean_card_precision_components() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    assessments = _assessments(manifest)
    for assessment in list(assessments.values())[:4]:
        assessment["verdict"] = "partially_useful"
        assessment["finding_type"] = "PARTIAL"
        assessment["fields"]["severity"] = False
    baseline = _baseline_assessments(manifest)
    threshold = build_report(manifest, issues, assessments, baseline)
    assert threshold["summary"]["issues_correct"] == 16
    assert threshold["summary"]["issues_partial"] == 4
    assert threshold["summary"]["field_quality_score"] == 98
    assert threshold["summary"]["clean_card_precision"] == 100
    assert threshold["summary"]["quality_score"] == 98.3
    assert threshold["status"] == "PASS"

    manifest["agents"][0]["baseline"].update(
        {
            "status": "not_at_bar",
            "insight_references": ["sha256:" + "1" * 64],
        }
    )
    baseline["weather-agent"] = {
        "verdict": "noise",
        "ownership": "insight_engine",
        "ownership_reason": "One false-positive baseline card was observed.",
        "confidence": 0.99,
        "card_evaluations": [{"evaluation": "noise"}],
    }
    penalized = build_report(manifest, issues, assessments, baseline)
    assert penalized["summary"]["noise_cards"] == 1
    assert penalized["summary"]["clean_card_precision"] == 95.2
    assert penalized["summary"]["quality_score"] == 97.6
    assert penalized["status"] == "PASS"


def test_failed_matched_fields_cannot_score_one_hundred() -> None:
    manifest = _manifest()
    _, issues = load_catalogs()
    assessments = _assessments(manifest)
    first = next(iter(assessments.values()))
    first["fields"]["severity"] = False
    report = build_report(
        manifest,
        issues,
        assessments,
        _baseline_assessments(manifest),
    )
    failed_issue = next(
        item for item in report["issues"] if item["issue_id"] == first["issue_id"]
    )
    assert failed_issue["result"] == "FAIL"
    assert report["summary"]["field_quality_score"] < 100
    assert report["summary"]["quality_score"] < 100


def test_incomplete_issue_is_inconclusive() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    manifest["agents"][0]["issues"][0]["status"] = "inconclusive"
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        _baseline_assessments(manifest),
    )
    assert report["status"] == "INCOMPLETE"
    assert report["summary"]["quality_score"] is None


def test_incomplete_issue_assessment_prevents_a_numeric_score() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    assessments = _assessments(manifest)
    issue_id = manifest["agents"][0]["issues"][0]["issue_id"]
    assessments[issue_id]["verdict"] = "missing"
    assessments[issue_id]["finding_type"] = "INCOMPLETE"
    assessments[issue_id]["ownership"] = "unresolved"
    assessments[issue_id]["fields"] = {
        field: False for field in assessments[issue_id]["fields"]
    }
    report = build_report(
        manifest,
        issues,
        assessments,
        _baseline_assessments(manifest),
    )
    result = next(item for item in report["issues"] if item["issue_id"] == issue_id)
    assert result["result"] == "INCOMPLETE"
    assert report["status"] == "INCOMPLETE"
    assert report["summary"]["incomplete"] is True
    assert report["summary"]["incomplete_reasons"] == [
        "assessment_evidence_incomplete"
    ]
    assert report["summary"]["quality_score"] is None


def test_inconclusive_assessment_prevents_a_numeric_score() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    manifest["agents"][0]["baseline"].update(
        {
            "status": "not_at_bar",
            "insight_references": ["sha256:" + "1" * 64],
        }
    )
    baseline = _baseline_assessments(manifest)
    baseline["weather-agent"] = {
        "verdict": "inconclusive",
        "ownership": "unresolved",
        "ownership_reason": "Independent endpoint evidence is unavailable.",
        "confidence": 0.8,
        "card_evaluations": [{"evaluation": "incomplete"}],
    }
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        baseline,
    )
    assert report["status"] == "INCOMPLETE"
    assert report["summary"]["incomplete"] is True
    assert report["summary"]["incomplete_reasons"] == [
        "assessment_evidence_incomplete"
    ]
    assert report["summary"]["quality_score"] is None


def test_incomplete_baseline_card_prevents_a_numeric_score() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    manifest["agents"][0]["baseline"].update(
        {
            "status": "not_at_bar",
            "insight_references": ["sha256:" + "1" * 64],
        }
    )
    baseline = _baseline_assessments(manifest)
    baseline["weather-agent"] = {
        "verdict": "noise",
        "ownership": "unresolved",
        "ownership_reason": "The generated card could not be verified.",
        "confidence": 0.8,
        "card_evaluations": [{"evaluation": "incomplete"}],
    }
    report = build_report(manifest, issues, _assessments(manifest), baseline)
    assert report["status"] == "INCOMPLETE"
    assert report["summary"]["incomplete"] is True
    assert report["summary"]["quality_score"] is None


def test_valid_baseline_agent_finding_is_not_noise() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    manifest["agents"][0]["baseline"].update(
        {
            "status": "not_at_bar",
            "insight_references": ["sha256:" + "1" * 64],
        }
    )
    baseline = _baseline_assessments(manifest)
    baseline["weather-agent"] = {
        "verdict": "agent_finding",
        "ownership": "agent",
        "ownership_reason": "Independent trace proof confirms an Agent defect.",
        "confidence": 0.99,
        "card_evaluations": [
            {
                "evaluation": "valid_agent_finding",
                "ownership": "agent",
            }
        ],
    }
    report = build_report(manifest, issues, _assessments(manifest), baseline)
    assert report["status"] == "PASS"
    assert report["summary"]["baseline_passed"] == 4
    assert report["summary"]["noise_cards"] == 0
    assert report["summary"]["clean_card_precision"] == 100
    assert report["summary"]["quality_score"] == 100
    report["delivery"]["content_digest"] = "sha256:" + "a" * 64
    validate_published_report(report)
    report["baseline"][0]["assessment"]["ownership"] = "insight_engine"
    with pytest.raises(ContractError, match="baseline"):
        validate_published_report(report)


def test_missing_runtime_evidence_prevents_a_numeric_score() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    manifest["agents"][0]["issues"][0]["endpoint_usable_response_count"] = 4
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        _baseline_assessments(manifest),
    )
    assert report["issues"][0]["result"] == "INCOMPLETE"
    assert report["status"] == "INCOMPLETE"
    assert report["summary"]["quality_score"] is None
    assert "runtime_evidence_incomplete" in report["summary"]["incomplete_reasons"]


def test_published_report_requires_complete_consistent_content() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        _baseline_assessments(manifest),
    )
    report["delivery"]["content_digest"] = "sha256:" + "a" * 64
    validate_published_report(report)
    report["issues"][0]["assessment"] = None
    with pytest.raises(ContractError, match="incomplete"):
        validate_published_report(report)


def test_published_report_rejects_incomplete_assessment_evidence() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    assessments = _assessments(manifest)
    issue_id = manifest["agents"][0]["issues"][0]["issue_id"]
    assessments[issue_id]["verdict"] = "missing"
    assessments[issue_id]["finding_type"] = "INCOMPLETE"
    assessments[issue_id]["ownership"] = "unresolved"
    assessments[issue_id]["fields"] = {
        field: False for field in assessments[issue_id]["fields"]
    }
    report = build_report(
        manifest,
        issues,
        assessments,
        _baseline_assessments(manifest),
    )
    report["summary"]["incomplete"] = False
    report["summary"]["quality_score"] = calculate_quality_score(
        field_quality_score=report["summary"]["field_quality_score"],
        clean_card_precision=report["summary"]["clean_card_precision"],
        incomplete=False,
    )
    report["status"] = (
        "PASS" if report["summary"]["quality_score"] >= 90 else "FAIL"
    )
    report["delivery"]["content_digest"] = "sha256:" + "a" * 64
    with pytest.raises(ContractError, match="incomplete"):
        validate_published_report(report)


def test_agent_report_is_a_human_validation_handoff() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        _baseline_assessments(manifest),
    )
    report["issues"][0]["assessment"]["card_evaluations"] = [
        {
            "title": "Synthetic partial finding",
            "finding_type": "PARTIAL",
            "fields": {
                "root_cause": True,
                "title": True,
                "description": True,
                "category": True,
                "severity": False,
                "proposed_fix": False,
                "linked_traces": True,
            },
        }
    ]
    markdown = render_agent_markdown(report, "weather-agent")
    assert "## Review summary" in markdown
    assert "## Evaluation guide" in markdown
    assert "## Insight-level evaluation" in markdown
    assert "## Human validation checklist" in markdown
    assert (
        "| Issue | Foundry version | Generated Insight | Evaluation | "
        "Passing fields | Failing fields |"
    ) in markdown
    assert "root cause, title, description, category, linked traces" in markdown
    assert "severity, proposed fix" in markdown
    assert "| Ownership |" not in markdown


def test_complete_rendered_reports_hide_internal_verdict_labels() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    assessments = _assessments(manifest)
    passing = build_report(
        manifest,
        issues,
        assessments,
        _baseline_assessments(manifest),
    )
    failing_assessments = deepcopy(assessments)
    for assessment in list(failing_assessments.values())[:4]:
        assessment["verdict"] = "incorrect"
        assessment["finding_type"] = "MISMATCHED"
        assessment["fields"] = {
            field: False for field in assessment["fields"]
        }
    failing = build_report(
        manifest,
        issues,
        failing_assessments,
        _baseline_assessments(manifest),
    )

    for report, expected_status in ((passing, "PASS"), (failing, "FAIL")):
        aggregate = render_markdown(report)
        assert report["status"] == expected_status
        assert re.search(r"\b(?:PASS|FAIL)\b", aggregate) is None
        assert "| Issue | Agent | Finding | Ownership |" in aggregate
        for baseline in report["baseline"]:
            agent = render_agent_markdown(report, baseline["agent"])
            assert re.search(r"\b(?:PASS|FAIL)\b", agent) is None
            assert "Quality score:" in agent


def test_incomplete_rendered_reports_keep_safety_state_visible() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    manifest["agents"][0]["baseline"]["status"] = "inconclusive"
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        _baseline_assessments(manifest),
    )

    assert report["status"] == "INCOMPLETE"
    assert "**INCOMPLETE**" in render_markdown(report)
    assert "**INCOMPLETE**" in render_agent_markdown(report, "weather-agent")
