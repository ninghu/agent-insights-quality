from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality.assessment import (
    _baseline_cards,
    _checkpoint_result,
    _linked_baseline_operations,
    _validate_baseline_cards,
    load_assessments,
)
from agent_insights_quality.cli import _rehydrate_with_retries
from agent_insights_quality.util import ContractError


def test_assessment_package_generation_retries_transient_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attempts = 0
    progress = []
    sleeps = []

    def rehydrate(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ContractError("synthetic transient package failure")
        return [tmp_path / "package.json"]

    runtime = SimpleNamespace(report_progress=progress.append)
    monkeypatch.setattr(
        "agent_insights_quality.cli.rehydrate_packages",
        rehydrate,
    )
    monkeypatch.setattr("agent_insights_quality.cli.time.sleep", sleeps.append)
    assert _rehydrate_with_retries(
        {},
        {},
        {},
        runtime,
        tmp_path,
        SimpleNamespace(),
    ) == [tmp_path / "package.json"]
    assert attempts == 2
    assert sleeps == [1]
    assert progress == [
        "assessment package generation failed transiently; retrying (2/3)"
    ]


def test_incomplete_manifest_result_does_not_require_checkpoint() -> None:
    store = SimpleNamespace(result=lambda *_args: None)
    result = _checkpoint_result(
        store,
        "weather-agent",
        {
            "logical_version": "issue-001",
            "foundry_version": "1",
            "content_digest": "sha256:" + "a" * 64,
            "status": "inconclusive",
            "operation_ids": [],
            "insight_references": [],
            "window_start": None,
            "window_end": None,
            "error_code": "invocation_failed",
        },
    )
    assert result.status == "inconclusive"
    assert result.error_code == "invocation_failed"


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


def test_baseline_trace_proof_uses_only_baseline_operations() -> None:
    insight = SimpleNamespace(linked_operation_ids=("a" * 32, "b" * 32))
    assert _linked_baseline_operations(insight, {"a" * 32}) == ("a" * 32,)
    with pytest.raises(ContractError, match="no linked baseline"):
        _linked_baseline_operations(insight, {"c" * 32})


def test_baseline_card_attribution_requires_exact_version() -> None:
    insights = [
        SimpleNamespace(
            agent_version="38",
            linked_operation_ids=("a" * 32,),
        ),
        SimpleNamespace(
            agent_version="42",
            linked_operation_ids=("a" * 32,),
        ),
    ]
    candidates = _baseline_cards(insights, {"a" * 32}, "38")
    assert [value.agent_version for value in candidates] == ["38"]


def test_valid_baseline_finding_requires_agent_ownership() -> None:
    card = {
        "reference": "sha256:" + "a" * 64,
        "title": "Synthetic baseline finding",
        "category": "reliability_errors",
        "severity": "medium",
    }
    assessment = {
        "agent_name": "weather-agent",
        "verdict": "agent_finding",
        "card_evaluations": [
            {
                **card,
                "evaluation": "valid_agent_finding",
                "ownership": "insight_engine",
            }
        ],
    }
    with pytest.raises(ContractError, match="ownership"):
        _validate_baseline_cards(
            assessment,
            {"observed_insights": [card]},
        )
