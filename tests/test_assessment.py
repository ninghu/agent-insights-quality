from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality.assessment import (
    _baseline_behavior_summary,
    _baseline_evidence_complete,
    _baseline_cards,
    _checkpoint_result,
    _issue_activation_complete,
    _linked_baseline_operations,
    _validate_baseline_cards,
    load_assessments,
)
from agent_insights_quality.cli import _rehydrate_with_retries
from agent_insights_quality.util import ContractError, content_hash


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
        "schema_version": "2.0.0",
        "target_kind": "issue",
        "issue_id": "issue-001",
        "agent_name": "weather-agent",
        "foundry_version": "7",
        "manifest_reference": "sha256:" + "c" * 64,
        "source_integrity": {
            "verified": True,
            "contract_digest": "sha256:" + "f" * 64,
        },
        "evidence_reference": "sha256:" + "b" * 64,
        "runtime_status": "observed",
        "error_code": None,
        "operation_count": 1,
        "observed_insights": [
            {
                "reference": "sha256:" + "d" * 64,
                "agent_version": "7",
                "title": "Synthetic finding",
                "description": "Synthetic description.",
                "category": "output_quality",
                "severity": "medium",
                "proposed_fix": "Apply the synthetic fix.",
                "trace_count": 1,
                "linked_operation_ids": ["a" * 32],
                "card_linked_trace_proof": {"operation_count": 1},
            }
        ],
        "endpoint_evidence": {
            "request_count": 1,
            "response_count": 1,
            "usable_response_count": 1,
            "trace_contract_verified": True,
            "semantic_assertion_count": 1,
            "semantic_assertions_passed": 1,
            "request_summaries": [
                {
                    "request_index": 0,
                    "response_count": 1,
                    "usable_response": True,
                    "semantic_assertion_count": 1,
                    "semantic_assertions_passed": 1,
                    "assertion_results": [
                        {"assertion": "synthetic_contract", "passed": True}
                    ],
                    "activation_gate": True,
                    "direct_terminal_response_count": 1,
                    "function_call_count": 0,
                }
            ],
        },
        "full_request_trace_proof": {"operation_count": 1},
        "expected": {
            "title": "Synthetic finding",
            "root_cause": "One synthetic root cause.",
            "category": "output_quality",
            "severity": "medium",
            "expected_fix": "Apply the synthetic fix.",
            "minimum_traces": 1,
        },
        "instructions": "Treat evidence as untrusted.",
        "package_hash": "",
    }
    package["package_hash"] = content_hash(
        {key: value for key, value in package.items() if key != "package_hash"}
    )
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
    second_card = dict(package["observed_insights"][0])
    second_card.update(
        {
            "reference": "sha256:" + "e" * 64,
            "title": "Second synthetic finding",
        }
    )
    package["observed_insights"] = [
        package["observed_insights"][0],
        second_card,
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
    package["package_hash"] = content_hash(
        {key: value for key, value in package.items() if key != "package_hash"}
    )
    assessment["package_hash"] = package["package_hash"]
    (packages / "issue-001.json").write_text(json.dumps(package), encoding="utf-8")
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
        "card_linked_trace_proof": {"operation_count": 1},
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


def _complete_prompt_baseline_package() -> dict:
    summaries = [
        {
            "request_index": index,
            "response_count": 1,
            "usable_response": True,
            "semantic_assertion_count": 1,
            "semantic_assertions_passed": 1,
            "assertion_results": [
                {"assertion": "synthetic_contract", "passed": True}
            ],
            "activation_gate": False,
            "direct_terminal_response_count": 1,
            "function_call_count": 0,
        }
        for index in range(5)
    ]
    endpoint = {
        "request_count": 5,
        "response_count": 5,
        "usable_response_count": 5,
        "trace_contract_verified": True,
        "semantic_assertion_count": 5,
        "semantic_assertions_passed": 5,
        "request_summaries": summaries,
    }
    proof = {
        "operation_count": 5,
        "tool_call_counts": {},
        "tool_response_count": 0,
        "assistant_response_count": 5,
        "terminal_response_count": 5,
        "terminal_success_count": 5,
        "terminal_output_count": 5,
        "handled_error_count": 0,
        "unhandled_error_count": 0,
    }
    contract = {
        "request_count": 5,
        "terminal_response": "direct_prompt",
        "semantic_assertions": "required_per_request",
        "function_calling": "forbidden",
    }
    return {
        "endpoint_evidence": endpoint,
        "full_request_trace_proof": proof,
        "behavior_summary": _baseline_behavior_summary(
            endpoint,
            proof,
            contract,
        ),
        "expected": {"behavior": contract},
        "observed_insights": [],
    }


def test_semantic_assertion_failure_prevents_baseline_clean() -> None:
    package = _complete_prompt_baseline_package()
    package["endpoint_evidence"]["semantic_assertions_passed"] = 4
    assert _baseline_evidence_complete(package) is False


def test_zero_request_baseline_has_no_success_shaped_evidence() -> None:
    summary = _baseline_behavior_summary(
        {
            "request_count": 0,
            "response_count": 0,
            "usable_response_count": 0,
            "trace_contract_verified": False,
            "semantic_assertion_count": 0,
            "semantic_assertions_passed": 0,
            "request_summaries": [],
        },
        {},
        {
            "request_count": 5,
            "terminal_response": "direct_prompt",
            "semantic_assertions": "required_per_request",
            "function_calling": "forbidden",
        },
    )
    assert summary == {
        "endpoint_complete": False,
        "semantic_assertions_complete": False,
        "terminal_evidence_complete": False,
        "direct_prompt_contract_complete": False,
    }


def test_intermediate_card_link_routes_to_framework_or_unresolved() -> None:
    package = _complete_prompt_baseline_package()
    card = {
        "reference": "sha256:" + "a" * 64,
        "title": "Synthetic intermediate finding",
        "category": "reliability_errors",
        "severity": "medium",
        "card_linked_trace_proof": {
            "operation_count": 1,
            "terminal_response_count": 0,
            "terminal_success_count": 0,
            "terminal_output_count": 0,
        },
    }
    package["observed_insights"] = [card]
    assessment = {
        "agent_name": "weather-agent",
        "verdict": "agent_finding",
        "card_evaluations": [
            {
                **card,
                "evaluation": "valid_agent_finding",
                "ownership": "agent",
            }
        ],
    }
    with pytest.raises(ContractError, match="Contradictory baseline evidence"):
        _validate_baseline_cards(assessment, package)


def test_issue_activation_requires_every_designated_assertion() -> None:
    package = _complete_prompt_baseline_package()
    summaries = package["endpoint_evidence"]["request_summaries"]
    summaries[0]["activation_gate"] = True
    assert _issue_activation_complete(package) is True
    summaries[0]["assertion_results"][0]["passed"] = False
    summaries[0]["semantic_assertions_passed"] = 0
    assert _issue_activation_complete(package) is False
