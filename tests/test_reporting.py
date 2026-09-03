from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.reporting import (
    _runtime_evidence_complete,
    apply_score_comparison,
    apply_staging_score_comparison,
    build_report,
    calculate_quality_score,
    render_agent_markdown,
    render_markdown,
    resolve_test_region,
    score_comparison,
    updated_trend,
    validate_report,
    validate_published_report,
)
from agent_insights_quality.live import (
    _normalize_fixture,
    _semantic_assertion_names,
    _trace_assertion_names,
)
from agent_insights_quality.util import ROOT, ContractError
from agent_insights_quality.validation_rules import (
    execution_context,
    execution_requests,
    issue_observation_context,
)
import pytest


def _manifest(*, full: bool = False) -> dict:
    agents, issues = load_catalogs()
    issue_by_id = {item["id"]: item for item in issues["issues"]}

    def evidence(
        traffic_path: Path,
        *,
        prompt: bool,
        baseline: bool,
    ) -> dict:
        summaries = []
        for index, raw in enumerate(execution_requests(traffic_path)):
            fixture = _normalize_fixture(raw)
            semantic_names = _semantic_assertion_names(
                fixture["semantic_assertions"]
            )
            trace_names = _trace_assertion_names(fixture["trace_assertions"])
            summaries.append({
                "request_index": index,
                "response_count": 1,
                "usable_response": True,
                "semantic_assertion_count": len(semantic_names),
                "semantic_assertions_passed": len(semantic_names),
                "assertion_results": [
                    {"assertion": name, "passed": True}
                    for name in semantic_names
                ],
                "trace_assertion_count": len(trace_names),
                "trace_assertions_passed": len(trace_names),
                "trace_assertion_results": [
                    {"assertion": name, "passed": True}
                    for name in trace_names
                ],
                "activation_gate": fixture["activation_gate"],
                "direct_terminal_response_count": int(prompt),
                "function_call_count": 0,
            })
        request_count = len(summaries)
        return {
            **(
                execution_context(traffic_path)
                if baseline
                else issue_observation_context(traffic_path)
            ),
            "endpoint_request_count": request_count,
            "endpoint_response_count": request_count,
            "endpoint_usable_response_count": request_count,
            "semantic_assertion_count": sum(
                item["semantic_assertion_count"] for item in summaries
            ),
            "semantic_assertions_passed": sum(
                item["semantic_assertions_passed"] for item in summaries
            ),
            "trace_assertion_count": sum(
                item["trace_assertion_count"] for item in summaries
            ),
            "trace_assertions_passed": sum(
                item["trace_assertions_passed"] for item in summaries
            ),
            "trace_contract_verified": True,
            "trace_behavior_summary": (
                {
                    "operation_count": request_count,
                    "tool_call_counts": {},
                    "tool_response_count": 0,
                    "assistant_response_count": request_count,
                    "explicit_terminal_success_count": request_count,
                    "explicit_terminal_output_count": request_count,
                    "terminal_response_count": request_count,
                    "terminal_success_count": request_count,
                    "terminal_output_count": request_count,
                    "handled_error_count": 0,
                    "unhandled_error_count": 0,
                }
                if baseline
                else {}
            ),
            "endpoint_request_summaries": summaries,
        }

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
                    **evidence(
                        ROOT / agent["baseline_path"] / "traffic.json",
                        prompt=prompt,
                        baseline=True,
                    ),
                },
                "issues": [
                    {
                        "issue_id": issue_id,
                        "foundry_version": issue_id,
                        "status": "observed",
                        "insight_references": ["sha256:" + "a" * 64],
                        "evidence_reference": "sha256:" + "b" * 64,
                        **evidence(
                            ROOT
                            / issue_by_id[issue_id]["implementation"]
                            / "traffic.json",
                            prompt=prompt,
                            baseline=False,
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
        "test_region": "WestUS2",
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
    return _with_card_evaluations({
        item["issue_id"]: {
            "issue_id": item["issue_id"],
            "verdict": "correct",
            "confidence": 0.99,
            "ownership": "none",
            "finding_type": "MATCHED",
            "ownership_reason": "The expected Insight is fully correct.",
            "reasoning": "The expected Insight is fully correct.",
            "fields": {
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
    })


def _with_card_evaluations(
    assessments: dict[str, dict],
) -> dict[str, dict]:
    for index, assessment in enumerate(assessments.values(), start=1):
        assessment["card_evaluations"] = [
            {
                "reference": f"sha256:{index:064x}",
                "title": f"Synthetic finding {index}",
                "category": "synthetic",
                "severity": "medium",
                "verdict": assessment["verdict"],
                "finding_type": assessment["finding_type"],
                "ownership": assessment["ownership"],
                "ownership_reason": assessment["ownership_reason"],
                "fields": deepcopy(assessment["fields"]),
                "confidence": assessment["confidence"],
                "reasoning": assessment["reasoning"],
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


def test_report_uses_correct_issue_percentage_without_status() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    assessments = _assessments(manifest)
    report = build_report(
        manifest,
        issues,
        assessments,
        _baseline_assessments(manifest),
    )
    assert report["summary"]["quality_score"] == 100
    assert "status" not in report
    changed = deepcopy(assessments)
    for assessment in list(changed.values())[:4]:
        assessment["verdict"] = "incorrect"
        assessment["finding_type"] = "MISMATCHED"
        assessment["fields"] = {
            field: False for field in assessment["fields"]
        }
        assessment["card_evaluations"][0]["verdict"] = "incorrect"
        assessment["card_evaluations"][0]["finding_type"] = "MISMATCHED"
        assessment["card_evaluations"][0]["fields"] = {
            field: False
            for field in assessment["card_evaluations"][0]["fields"]
        }
    failed = build_report(
        manifest,
        issues,
        changed,
        _baseline_assessments(manifest),
    )
    assert failed["summary"]["issues_incorrect"] == 4
    assert failed["summary"]["quality_score"] == 80
    assert "status" not in failed


def test_reporting_uses_reviewed_model_mediated_threshold() -> None:
    manifest = _manifest()
    issue = next(
        value
        for agent in manifest["agents"]
        for value in agent["issues"]
        if value["issue_id"] == "issue-004"
    )
    observations = [
        summary
        for summary in issue["endpoint_request_summaries"]
        if summary["activation_gate"]
    ]
    for summary in observations[-2:]:
        summary["semantic_assertions_passed"] = 0
        for result in summary["assertion_results"]:
            result["passed"] = False
    issue["semantic_assertions_passed"] -= sum(
        summary["semantic_assertion_count"] for summary in observations[-2:]
    )
    traffic_path = (
        ROOT
        / "agents"
        / "weather-agent"
        / "issues"
        / "issue-004"
        / "traffic.json"
    )

    assert _runtime_evidence_complete(issue, traffic_path=traffic_path) is True
    failing = observations[-3]
    failing["semantic_assertions_passed"] = 0
    for result in failing["assertion_results"]:
        result["passed"] = False
    issue["semantic_assertions_passed"] -= failing["semantic_assertion_count"]
    assert _runtime_evidence_complete(issue, traffic_path=traffic_path) is False


def test_quality_score_formula_is_directly_explainable() -> None:
    assert calculate_quality_score(
        correct_issues=17,
        expected_issues=20,
        noise_cards=1,
        duplicate_cards=1,
    ) == 77.3


def test_render_markdown_uses_exact_agent_insights_quality_title() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        _baseline_assessments(manifest),
    )
    markdown = render_markdown(report)
    assert markdown.startswith("# Agent Insights Quality - 2026-08-24")


def test_build_report_uses_arm_resolved_canonical_test_region() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        _baseline_assessments(manifest),
    )
    assert report["test_region"] == "WestUS2"


def test_build_report_fails_closed_on_missing_test_region() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    del manifest["test_region"]
    with pytest.raises(ContractError, match="test_region"):
        build_report(
            manifest,
            issues,
            _assessments(manifest),
            _baseline_assessments(manifest),
        )


def test_build_report_fails_closed_on_unresolved_test_region_metadata() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    manifest["test_region"] = "notarealazureregion"
    with pytest.raises(ContractError, match="test_region"):
        build_report(
            manifest,
            issues,
            _assessments(manifest),
            _baseline_assessments(manifest),
        )


def test_build_report_accepts_other_arm_resolved_canonical_regions() -> None:
    _, issues = load_catalogs()
    for canonical in ("EastUS", "UKSouth", "CentralIndia", "SoutheastAsia"):
        manifest = _manifest()
        manifest["test_region"] = canonical
        report = build_report(
            manifest,
            issues,
            _assessments(manifest),
            _baseline_assessments(manifest),
        )
        assert report["test_region"] == canonical


def test_build_report_registry_test_region_cannot_supply_or_fall_back() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    del manifest["test_region"]
    manifest["test_region_registry"] = "westus2"
    with pytest.raises(ContractError, match="test_region"):
        build_report(
            manifest,
            issues,
            _assessments(manifest),
            _baseline_assessments(manifest),
        )


def test_build_report_fails_closed_on_registry_test_region_mismatch() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    manifest["test_region"] = "WestUS2"
    manifest["test_region_registry"] = "EastUS"
    with pytest.raises(ContractError, match="cross-check"):
        build_report(
            manifest,
            issues,
            _assessments(manifest),
            _baseline_assessments(manifest),
        )


def test_build_report_registry_test_region_cross_check_matches() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    manifest["test_region"] = "WestUS2"
    manifest["test_region_registry"] = "west-us-2"
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        _baseline_assessments(manifest),
    )
    assert report["test_region"] == "WestUS2"


def test_resolve_test_region_uses_only_live_location_as_source() -> None:
    assert resolve_test_region("WestUS2") == "WestUS2"
    with pytest.raises(ContractError, match="live"):
        resolve_test_region(None, "westus2")
    with pytest.raises(ContractError, match="live"):
        resolve_test_region("", "westus2")


def test_resolve_test_region_generic_with_injected_metadata() -> None:
    metadata = {"contosonorth": "Contoso North"}
    assert (
        resolve_test_region("contosonorth", location_metadata=metadata)
        == "ContosoNorth"
    )
    with pytest.raises(ContractError, match="Azure location metadata"):
        resolve_test_region("westus2", location_metadata=metadata)


def test_report_schema_requires_test_region() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        _baseline_assessments(manifest),
    )
    validate_report(report)
    missing = deepcopy(report)
    del missing["test_region"]
    with pytest.raises(Exception):
        validate_report(missing)
    invalid = deepcopy(report)
    invalid["test_region"] = "eastus2"
    with pytest.raises(Exception):
        validate_report(invalid)


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
    assert result["outcome"] == "missing"
    assert report["summary"]["issues_missing"] == 1
    assert report["summary"]["quality_score"] == 95


def test_staging_uses_the_single_quality_score() -> None:
    _, issues = load_catalogs()
    manifest = _manifest(full=True)
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        _baseline_assessments(manifest),
    )
    assert report["summary"]["quality_score_formula"] == (
        "correct_over_expected_plus_noise_v1"
    )
    assert report["summary"]["quality_score"] == 100
    assert "shadow_quality_score" not in report["summary"]
    assert "shadow_v2_primary" not in report["issues"][0]
    validate_report(report)


def test_incomplete_staging_produces_no_report() -> None:
    _, issues = load_catalogs()
    manifest = _manifest(full=True)
    manifest["agents"][0]["issues"][0]["endpoint_usable_response_count"] = 4
    with pytest.raises(ContractError, match="no quality report"):
        build_report(
            manifest,
            issues,
            _assessments(manifest),
            _baseline_assessments(manifest),
        )


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
                field: field != "title" for field in assessment["fields"]
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
    assert result["outcome"] == "incorrect"
    assert report["summary"]["issues_incorrect"] == 1
    assert report["summary"]["quality_score"] == 95


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
    assert result["outcome"] == "incorrect"
    assert report["summary"]["quality_score"] == 95


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
  "schema_version": "2.0.0",
  "quality_score_formula": "correct_over_expected_plus_noise_v1",
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
    assert "| Quality score | **100 / 100 (+5.9 vs 2026-08-25)** |" in (
        render_markdown(report)
    )


def test_score_comparison_rejects_legacy_formula(tmp_path: Path) -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
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

    with pytest.raises(ContractError, match="different quality-score formula"):
        apply_score_comparison(report, trend)


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
    (receipts / "aiq-20260826.json").write_text(
        '{"profile":"staging","qualified":true,"human_reviewed":true,'
        '"quality_score":99.9}',
        encoding="utf-8",
    )
    (receipts / "aiq-20260827-r29.json").write_text(
        """{
  "profile": "staging",
  "qualified": true,
  "human_reviewed": true,
  "quality_score_formula": "correct_over_expected_plus_noise_v1",
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
        '"quality_score_formula":"correct_over_expected_plus_noise_v1",'
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
        "schema_version": "2.0.0",
        "quality_score_formula": "correct_over_expected_plus_noise_v1",
        "days": [
            {
                "report_date": "2026-08-26",
                "baseline_passed": 1,
                "issues_correct": 4,
                "issues_incorrect": 15,
                "issues_missing": 1,
                "issues_expected": 20,
                "noise_cards": 0,
                "duplicate_cards": 0,
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
        "schema_version": "2.0.0",
        "quality_score_formula": "correct_over_expected_plus_noise_v1",
        "days": [
            updated_trend(
                report,
                {
                    "schema_version": "2.0.0",
                    "quality_score_formula": "correct_over_expected_plus_noise_v1",
                    "days": [],
                },
            )["days"][0]
        ],
    }
    base["days"][0]["quality_score"] = 44.1
    with pytest.raises(ContractError, match="immutable"):
        updated_trend(report, base)


def test_optional_fields_do_not_score_and_noise_expands_denominator() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    assessments = _assessments(manifest)
    for assessment in list(assessments.values())[:4]:
        assessment["fields"]["severity"] = False
        card = assessment["card_evaluations"][0]
        card["fields"]["severity"] = False
        card["field_reasons"] = {
            "severity": "The synthetic severity is incomplete."
        }
    baseline = _baseline_assessments(manifest)
    report = build_report(manifest, issues, assessments, baseline)
    assert report["summary"]["issues_correct"] == 20
    assert report["summary"]["issues_incorrect"] == 0
    assert report["summary"]["quality_score"] == 100

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
    assert penalized["summary"]["quality_score"] == 95.2


def test_optional_top_level_field_mismatch_does_not_change_card_score() -> None:
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
    assert failed_issue["outcome"] == "correct"
    assert report["summary"]["quality_score"] == 100


def test_incomplete_issue_is_inconclusive() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    manifest["agents"][0]["issues"][0]["status"] = "inconclusive"
    with pytest.raises(ContractError, match="no quality report"):
        build_report(
            manifest,
            issues,
            _assessments(manifest),
            _baseline_assessments(manifest),
        )


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
    with pytest.raises(ContractError, match="no quality report"):
        build_report(
            manifest,
            issues,
            assessments,
            _baseline_assessments(manifest),
        )


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
    with pytest.raises(ContractError, match="no quality report"):
        build_report(
            manifest,
            issues,
            _assessments(manifest),
            baseline,
        )


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
    with pytest.raises(ContractError, match="no quality report"):
        build_report(manifest, issues, _assessments(manifest), baseline)


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
                "reference": "sha256:" + "1" * 64,
                "title": "Synthetic baseline finding",
                "category": "synthetic",
                "severity": "medium",
                "evaluation": "valid_agent_finding",
                "ownership": "agent",
                "ownership_reason": (
                    "Independent trace proof confirms an Agent defect."
                ),
                "confidence": 0.99,
                "reasoning": (
                    "Independent trace proof confirms an Agent defect."
                ),
            }
        ],
    }
    report = build_report(manifest, issues, _assessments(manifest), baseline)
    assert report["summary"]["baseline_passed"] == 4
    assert report["summary"]["noise_cards"] == 0
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
    with pytest.raises(ContractError, match="no quality report"):
        build_report(
            manifest,
            issues,
            _assessments(manifest),
            _baseline_assessments(manifest),
        )


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
    inconsistent_outcome = deepcopy(report)
    inconsistent_outcome["issues"][0]["outcome"] = "missing"
    with pytest.raises(ContractError, match="outcomes are inconsistent"):
        validate_published_report(inconsistent_outcome)
    report["issues"][0]["assessment"] = None
    with pytest.raises(ContractError, match="incomplete"):
        validate_published_report(report)


def test_report_rejects_malformed_or_private_nested_content() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        _baseline_assessments(manifest),
    )
    validate_report(report)

    malformed = deepcopy(report)
    del malformed["issues"][0]["assessment"]["card_evaluations"][0]["category"]
    with pytest.raises(ContractError, match="invalid or incomplete"):
        validate_report(malformed)

    private = deepcopy(report)
    private["issues"][0]["assessment"]["card_evaluations"][0][
        "ownership_reason"
    ] = "See /subscriptions/private-resource."
    with pytest.raises(ContractError, match="private Azure"):
        validate_report(private)

    nested = deepcopy(report)
    nested["baseline"][0]["raw_trace"] = {"provider_id": "private"}
    with pytest.raises(ContractError, match="invalid or incomplete"):
        validate_report(nested)

    missing_reasoning = deepcopy(report)
    del missing_reasoning["issues"][0]["assessment"]["reasoning"]
    with pytest.raises(ContractError, match="invalid or incomplete"):
        validate_report(missing_reasoning)

    wrong_field_reasons = deepcopy(report)
    card = wrong_field_reasons["issues"][0]["assessment"]["card_evaluations"][0]
    card["finding_type"] = "PARTIAL"
    card["verdict"] = "partially_useful"
    card["ownership"] = "insight_engine"
    card["fields"]["severity"] = False
    card["field_reasons"] = {
        "title": "The title is correct, so this reason is not allowed."
    }
    with pytest.raises(ContractError, match="exactly each failed field"):
        validate_report(wrong_field_reasons)

    unresolved_duplicate = deepcopy(report)
    primary = unresolved_duplicate["issues"][0]["assessment"][
        "card_evaluations"
    ][0]
    duplicate = deepcopy(primary)
    duplicate.update(
        {
            "reference": "sha256:" + "f" * 64,
            "verdict": "incorrect",
            "finding_type": "DUPLICATE",
            "ownership": "insight_engine",
            "duplicate_of": "sha256:" + "e" * 64,
        }
    )
    unresolved_duplicate["issues"][0]["assessment"][
        "card_evaluations"
    ].append(duplicate)
    unresolved_duplicate["issues"][0]["observed_count"] += 1
    with pytest.raises(ContractError, match="same assessment"):
        validate_report(unresolved_duplicate)

    missing_card_evaluation = deepcopy(report)
    missing_card_evaluation["issues"][0]["assessment"]["card_evaluations"] = []
    with pytest.raises(ContractError, match="cover every observed card"):
        validate_report(missing_card_evaluation)


def test_incomplete_assessment_cannot_build_a_report() -> None:
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
    with pytest.raises(ContractError, match="no quality report"):
        build_report(
            manifest,
            issues,
            assessments,
            _baseline_assessments(manifest),
        )


def test_agent_report_is_a_human_validation_handoff() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    assessments = _assessments(manifest)
    first_issue_id = manifest["agents"][0]["issues"][0]["issue_id"]
    assessments[first_issue_id]["verdict"] = "partially_useful"
    assessments[first_issue_id]["finding_type"] = "PARTIAL"
    assessments[first_issue_id]["ownership"] = "insight_engine"
    assessments[first_issue_id]["reasoning"] = (
        "The card names the correct root but severity and fix are wrong."
    )
    assessments[first_issue_id]["fields"] = {
        "title": True,
        "description": True,
        "category": True,
        "severity": False,
        "proposed_fix": False,
        "linked_traces": True,
    }
    assessments[first_issue_id]["card_evaluations"] = [
        {
            "reference": "sha256:" + "9" * 64,
            "title": "Synthetic partial finding",
            "finding_type": "PARTIAL",
            "fields": assessments[first_issue_id]["fields"],
            "field_reasons": {
                "severity": "The card understates impact though traces show a full outage.",
                "proposed_fix": "The proposed fix does not address the identified root cause.",
            },
            "ownership": "insight_engine",
            "confidence": 0.8,
            "reasoning": "The card names the correct root but severity and fix are wrong.",
        },
        {
            "reference": "sha256:" + "8" * 64,
            "title": "Secondary attributable finding",
            "finding_type": "PARTIAL",
            "fields": {
                **assessments[first_issue_id]["fields"],
                "title": False,
            },
            "field_reasons": {
                "root_cause": "The secondary card names a downstream symptom.",
                "severity": "The secondary card understates impact.",
                "proposed_fix": "The secondary fix targets the wrong component.",
            },
            "ownership": "insight_engine",
            "confidence": 0.6,
            "reasoning": "This additional card is attributable but not primary.",
        },
    ]
    report = build_report(
        manifest,
        issues,
        assessments,
        _baseline_assessments(manifest),
    )
    markdown = render_agent_markdown(report, "weather-agent")
    assert "## Expected issue coverage" in markdown
    assert "## Extra generated Insights" in markdown
    assert "## Decision details" in markdown
    assert "## Evaluation guide" in markdown
    assert "## Human validation checklist" in markdown
    assert "## Coding-agent context" in markdown
    assert (
        "| Expected issue | Version | Primary Insight | Evaluation | Why |"
    ) in markdown
    assert "Incorrect" in markdown
    assert "Synthetic partial finding" in markdown
    assert "understates impact" in markdown
    assert "does not address the identified root cause" in markdown
    assert "Secondary attributable finding" in markdown
    assert "Additional attributable card" in markdown
    assert "The secondary fix targets the wrong component." in markdown
    assert "Quality score:" not in markdown
    assert "Runtime evidence: `Complete`" in markdown


def test_extra_insights_missing_coverage_duplicates_and_coding_agent_context() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    assessments = _assessments(manifest)
    agent_name = manifest["agents"][0]["name"]
    agent_issue_ids = [item["issue_id"] for item in manifest["agents"][0]["issues"]]
    noise_issue_id, duplicate_issue_id = agent_issue_ids[0], agent_issue_ids[1]
    for index, issue_id in enumerate(agent_issue_ids[2:], start=1):
        assessments[issue_id]["card_evaluations"] = [
            {
                "reference": "sha256:" + f"{index + 4:02x}" * 32,
                "title": f"Primary finding for {issue_id}",
                "finding_type": "MATCHED",
                "fields": deepcopy(assessments[issue_id]["fields"]),
                "ownership": "none",
                "confidence": 0.95,
                "reasoning": "The card fully matches the expected issue.",
            }
        ]

    # An expected issue with only a Noise card is still Missing, and the Noise
    # card is recorded as an Extra generated Insight with no issue assignment.
    assessments[noise_issue_id]["verdict"] = "missing"
    assessments[noise_issue_id]["finding_type"] = "NOISE"
    assessments[noise_issue_id]["ownership"] = "insight_engine"
    assessments[noise_issue_id]["reasoning"] = (
        "No card represents this issue's root cause."
    )
    assessments[noise_issue_id]["card_evaluations"] = [
        {
            "reference": "sha256:" + "a1" * 32,
            "title": "Unrelated noise finding",
            "finding_type": "NOISE",
            "fields": {
                field: False for field in assessments[noise_issue_id]["fields"]
            },
            "ownership": "insight_engine",
            "ownership_reason": (
                "The card describes a condition never present in this trace."
            ),
            "confidence": 0.7,
            "reasoning": "Independent trace review shows no such condition occurred.",
        }
    ]

    # An issue with a primary MATCHED card plus a Duplicate of that same card
    # stays Correct, and the Duplicate renders as an explicit group.
    assessments[duplicate_issue_id]["verdict"] = "incorrect"
    assessments[duplicate_issue_id]["finding_type"] = "DUPLICATE"
    assessments[duplicate_issue_id]["ownership"] = "insight_engine"
    assessments[duplicate_issue_id]["fields"]["title"] = False
    primary_reference = "sha256:" + "b2" * 32
    assessments[duplicate_issue_id]["card_evaluations"] = [
        {
            "reference": primary_reference,
            "title": "Primary root cause finding",
            "finding_type": "MATCHED",
            "fields": {
                field: True
                for field in assessments[duplicate_issue_id]["fields"]
            },
            "ownership": "none",
            "confidence": 0.95,
            "reasoning": "The card fully matches the expected issue.",
        },
        {
            "reference": "sha256:" + "c3" * 32,
            "title": "Repeated finding",
            "finding_type": "DUPLICATE",
            "duplicate_of": primary_reference,
            "fields": deepcopy(assessments[duplicate_issue_id]["fields"]),
            "ownership": "insight_engine",
            "confidence": 0.9,
            "reasoning": "This card repeats the same root as the primary card.",
        },
        {
            "reference": "sha256:" + "d4" * 32,
            "title": "Second repeated finding",
            "finding_type": "DUPLICATE",
            "duplicate_of": primary_reference,
            "fields": deepcopy(assessments[duplicate_issue_id]["fields"]),
            "ownership": "insight_engine",
            "confidence": 0.85,
            "reasoning": "This second card also adds no independent root.",
        },
    ]

    report = build_report(
        manifest,
        issues,
        assessments,
        _baseline_assessments(manifest),
    )
    markdown = render_agent_markdown(report, agent_name)

    coverage_section = markdown.split("## Expected issue coverage", 1)[1].split(
        "## Extra generated Insights", 1
    )[0]
    noise_row = next(
        line for line in coverage_section.splitlines() if noise_issue_id in line
    )
    assert "Missing" in noise_row
    assert "No card represents this issue's root cause." in noise_row

    assert "Unrelated noise finding" in markdown
    assert "Duplicate of **Primary root cause finding**." in markdown
    duplicate_coverage_row = next(
        line for line in coverage_section.splitlines() if duplicate_issue_id in line
    )
    assert "Correct" in duplicate_coverage_row

    review_summary = markdown.split("## Review summary", 1)[1].split(
        "## Expected issue coverage", 1
    )[0]
    assert "| Missing | 1 |" in review_summary
    assert "| Noise | 1 |" in review_summary
    assert "| Duplicate | 2 |" in review_summary

    assert "### Noise card - observed in" in markdown
    assert "**Corresponding issue:** None" in markdown
    assert f"### [{noise_issue_id}]" in markdown
    assert "### Duplicate group" in markdown
    assert "**Primary card:** Primary root cause finding" in markdown
    assert "1. Repeated finding" in markdown
    assert "2. Second repeated finding" in markdown
    assert "This card repeats the same root as the primary card." in markdown
    assert "This second card also adds no independent root." in markdown

    context_section = markdown.split("## Coding-agent context", 1)[1]
    # The Missing issue's row merges its own coverage label with the
    # unmatched Noise card generated in the same version - one row, no
    # separate row for the extra card.
    assert f"`{noise_issue_id}` Missing + unmatched Noise" in context_section
    assert f"`{noise_issue_id}` Missing |" not in context_section
    # The otherwise-Correct issue still gets a row because of its Duplicate
    # group, and its ownership is the Duplicate card's own ownership, not
    # the primary's "no problem" ownership.
    assert f"`{duplicate_issue_id}` Duplicate group" in context_section
    assert f"`{duplicate_issue_id}` Missing" not in context_section
    noise_row = next(
        line for line in context_section.splitlines() if noise_issue_id in line
    )
    duplicate_row = next(
        line for line in context_section.splitlines() if duplicate_issue_id in line
    )
    assert "`insight_engine`" in noise_row
    assert "Missing / Noise details above" in noise_row
    assert f"agents/{agent_name}/issues/{noise_issue_id}/source/" in noise_row
    assert f"agents/{agent_name}/issues/{noise_issue_id}/traffic.json" in noise_row
    assert "`insight_engine`" in duplicate_row
    assert "Duplicate detail above" in duplicate_row
    assert f"agents/{agent_name}/issues/{duplicate_issue_id}/source/" in duplicate_row
    # No hyperlink is used for the issue-catalog reference in this table -
    # it is a deterministic repo-relative path a coding agent can open.
    assert f"`ISSUE_CATALOG.md#{noise_issue_id}`" in noise_row
    assert f"[ISSUE_CATALOG.md#{noise_issue_id}]" not in noise_row


def test_primary_coverage_and_every_extra_owner_are_independent() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    assessments = _assessments(manifest)
    issue_id = manifest["agents"][0]["issues"][0]["issue_id"]
    assessment = assessments[issue_id]
    assessment["verdict"] = "partially_useful"
    assessment["finding_type"] = "PARTIAL"
    assessment["ownership"] = "agent"
    assessment["fields"]["severity"] = False
    primary = assessment["card_evaluations"][0]
    primary.update(
        {
            "verdict": "partially_useful",
            "finding_type": "PARTIAL",
            "ownership": "agent",
            "ownership_reason": "The Agent emitted an incomplete severity.",
            "field_reasons": {
                "severity": "The synthetic severity is incomplete."
            },
        }
    )
    primary["fields"]["severity"] = False
    noise = deepcopy(primary)
    noise.update(
        {
            "reference": "sha256:" + ("f" * 64),
            "title": "Independent extra noise",
            "verdict": "incorrect",
            "finding_type": "NOISE",
            "ownership": "insight_engine",
            "ownership_reason": "The extra card is unrelated.",
            "reasoning": "Independent evidence disproves the extra card.",
        }
    )
    noise.pop("field_reasons")
    assessment["card_evaluations"].append(noise)
    report = build_report(
        manifest,
        issues,
        assessments,
        _baseline_assessments(manifest),
    )
    assert report["summary"]["issues_incorrect"] == 1
    assert report["summary"]["noise_cards"] == 1
    markdown = render_agent_markdown(report, manifest["agents"][0]["name"])
    context = markdown.split("## Coding-agent context", 1)[1]
    rows = [line for line in context.splitlines() if issue_id in line]
    assert len(rows) == 2
    assert any("`agent`" in row and "Incorrect" in row for row in rows)
    assert any(
        "`insight_engine`" in row and "unmatched Noise" in row
        for row in rows
    )


def test_daily_top_level_report_links_insight_engine_improvement() -> None:
    _, issues = load_catalogs()
    daily_manifest = _manifest()
    daily_report = build_report(
        daily_manifest,
        issues,
        _assessments(daily_manifest),
        _baseline_assessments(daily_manifest),
    )
    daily_markdown = render_markdown(daily_report)
    assert "## Per-Agent reports" in daily_markdown
    assert (
        "[View Insight Engine Improvement Report]"
        "(../../../../insight-engine-improvement.md)"
        in daily_markdown
    )
    assert daily_markdown.index("## Per-Agent reports") < daily_markdown.index(
        "View Insight Engine Improvement Report"
    )

    staging_manifest = _manifest(full=True)
    staging_report = build_report(
        staging_manifest,
        issues,
        _assessments(staging_manifest),
        _baseline_assessments(staging_manifest),
    )
    staging_markdown = render_markdown(staging_report)
    assert "View Insight Engine Improvement Report" not in staging_markdown


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

    for report in (passing, failing):
        aggregate = render_markdown(report)
        assert "status" not in report
        assert re.search(r"\b(?:PASS|FAIL)\b", aggregate) is None
        assert "| Issue | Agent | Finding | Ownership |" in aggregate
        for baseline in report["baseline"]:
            agent = render_agent_markdown(report, baseline["agent"])
            assert re.search(r"\b(?:PASS|FAIL)\b", agent) is None
            assert "Runtime evidence:" in agent
            assert "Quality score:" not in agent


def test_incomplete_evidence_has_no_rendered_report() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    manifest["agents"][0]["baseline"]["status"] = "inconclusive"
    with pytest.raises(ContractError, match="no quality report"):
        build_report(
            manifest,
            issues,
            _assessments(manifest),
            _baseline_assessments(manifest),
        )
