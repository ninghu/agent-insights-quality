from __future__ import annotations

from copy import deepcopy

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.reporting import (
    build_report,
    render_agent_markdown,
    validate_published_report,
)
from agent_insights_quality.util import ContractError
import pytest


def _manifest() -> dict:
    agents, _ = load_catalogs()
    values = []
    for agent in agents["agents"]:
        selected = agent["issue_ids"][:5]
        values.append(
            {
                "name": agent["name"],
                "baseline": {
                    "foundry_version": "1",
                    "status": "passed",
                    "insight_references": [],
                },
                "issues": [
                    {
                        "issue_id": issue_id,
                        "foundry_version": issue_id,
                        "status": "observed",
                        "insight_references": ["sha256:" + "a" * 64],
                        "evidence_reference": "sha256:" + "b" * 64,
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

    manifest["agents"][0]["baseline"] = {
        "foundry_version": "1",
        "status": "not_at_bar",
        "insight_references": ["sha256:" + "1" * 64],
    }
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


def test_inconclusive_assessment_prevents_a_numeric_score() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    baseline = _baseline_assessments(manifest)
    baseline["weather-agent"] = {
        "verdict": "inconclusive",
        "ownership": "unresolved",
        "ownership_reason": "Independent endpoint evidence is unavailable.",
        "confidence": 0.8,
        "card_evaluations": [],
    }
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        baseline,
    )
    assert report["status"] == "INCOMPLETE"
    assert report["summary"]["incomplete"] is True
    assert report["summary"]["quality_score"] is None


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


def test_agent_report_is_a_human_validation_handoff() -> None:
    _, issues = load_catalogs()
    manifest = _manifest()
    report = build_report(
        manifest,
        issues,
        _assessments(manifest),
        _baseline_assessments(manifest),
    )
    markdown = render_agent_markdown(report, "weather-agent")
    assert "## Review summary" in markdown
    assert "## Evaluation guide" in markdown
    assert "## Insight-level evaluation" in markdown
    assert "## Human validation checklist" in markdown
    assert "| Issue | Foundry version | Generated Insight | Evaluation |" in markdown
    assert "| Ownership |" not in markdown
    assert "| Fields |" not in markdown
