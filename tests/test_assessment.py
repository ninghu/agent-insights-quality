from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_insights_quality.assessment import load_assessments
from agent_insights_quality.util import ContractError


def test_assessment_must_match_current_package(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    package = {
        "package_hash": "sha256:" + "a" * 64,
        "foundry_version": "7",
        "evidence_reference": "sha256:" + "b" * 64,
        "runtime_status": "observed",
        "observed_insights": [
            {
                "reference": "sha256:" + "d" * 64,
                "title": "Synthetic finding",
                "category": "output_quality",
                "severity": "medium",
                "trace_count": 1,
            }
        ],
        "expected": {"minimum_traces": 1},
    }
    (packages / "issue-001.json").write_text(
        json.dumps(package),
        encoding="utf-8",
    )
    assessment = {
        "schema_version": "1.0.0",
        "issue_id": "issue-001",
        "model": "gpt-5.6-sol",
        "package_hash": package["package_hash"],
        "foundry_version": package["foundry_version"],
        "evidence_reference": package["evidence_reference"],
        "verdict": "correct",
        "finding_type": "MATCHED",
        "ownership": "none",
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
        "card_evaluations": [
            {
                "reference": "sha256:" + "d" * 64,
                "title": "Synthetic finding",
                "category": "output_quality",
                "severity": "medium",
                "verdict": "correct",
                "finding_type": "MATCHED",
                "ownership": "none",
                "ownership_reason": "The card matches the expected root.",
                "fields": {
                    "root_cause": True,
                    "title": True,
                    "description": True,
                    "category": True,
                    "severity": True,
                    "proposed_fix": True,
                    "linked_traces": True,
                },
                "confidence": 0.99,
                "reasoning": "The card is fully correct.",
            }
        ],
        "confidence": 0.99,
        "reasoning": "The bounded evidence matches.",
    }
    path = tmp_path / "assessment.json"
    path.write_text(json.dumps(assessment), encoding="utf-8")
    assert load_assessments([path], {"issue-001"}, packages)["issue-001"] == assessment
    assessment["package_hash"] = "sha256:" + "c" * 64
    path.write_text(json.dumps(assessment), encoding="utf-8")
    with pytest.raises(ContractError, match="current evidence"):
        load_assessments([path], {"issue-001"}, packages)

    assessment["package_hash"] = package["package_hash"]
    package["runtime_status"] = "not_at_bar"
    package["observed_insights"] = [
        package["observed_insights"][0],
        {
            "reference": "sha256:" + "e" * 64,
            "title": "Second synthetic finding",
            "category": "output_quality",
            "severity": "medium",
            "trace_count": 1,
        },
    ]
    assessment["card_evaluations"].append(
        {
            "reference": "sha256:" + "e" * 64,
            "title": "Second synthetic finding",
            "category": "output_quality",
            "severity": "medium",
            "verdict": "incorrect",
            "finding_type": "NOISE",
            "ownership": "insight_engine",
            "ownership_reason": "The extra card is unrelated noise.",
            "fields": {
                "root_cause": False,
                "title": False,
                "description": False,
                "category": False,
                "severity": False,
                "proposed_fix": False,
                "linked_traces": False,
            },
            "confidence": 0.99,
            "reasoning": "The second card does not match the expected root.",
        }
    )
    (packages / "issue-001.json").write_text(
        json.dumps(package),
        encoding="utf-8",
    )
    path.write_text(json.dumps(assessment), encoding="utf-8")
    with pytest.raises(ContractError, match="contradicts runtime evidence"):
        load_assessments([path], {"issue-001"}, packages)
