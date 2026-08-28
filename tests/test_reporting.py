from __future__ import annotations

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


def _manifest() -> dict:
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
                "activation_gate": activation,
                "direct_terminal_response_count": int(prompt),
                "function_call_count": 0,
            }
            for index in range(5)
        ]

    values = []
    for agent in agents["agents"]:
        selected = agent["issue_ids"][:5]
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
        "profile": "daily",
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
    for assessment in list(changed.values())[:5]:
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
                "issues_expected": 25,
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
                "issues_expected": 25,
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
    for assessment in list(assessments.values())[:5]:
        assessment["verdict"] = "partially_useful"
        assessment["finding_type"] = "PARTIAL"
        assessment["fields"]["severity"] = False
    baseline = _baseline_assessments(manifest)
    threshold = build_report(manifest, issues, assessments, baseline)
    assert threshold["summary"]["issues_correct"] == 20
    assert threshold["summary"]["issues_partial"] == 5
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
    assert penalized["summary"]["clean_card_precision"] == 96.2
    assert penalized["summary"]["quality_score"] == 97.7
    assert penalized["status"] == "PASS"


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
