from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality.assessment import (
    _baseline_behavior_summary,
    _baseline_evidence_complete,
    _baseline_cards,
    _cards_for_operations,
    _checkpoint_result,
    _issue_activation_complete,
    _linked_baseline_operations,
    _load_package,
    _package_execution_context,
    _validate_baseline_cards,
    _validate_issue_cards,
    _validate_package,
    load_assessments,
    rehydrate_packages,
)
from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.cli import _rehydrate_with_retries
from agent_insights_quality.util import ROOT, ContractError, content_hash


def _trace_proof(
    *,
    operation_count: int = 1,
    terminal_count: int = 1,
) -> dict:
    return {
        "operation_count": operation_count,
        "tool_call_counts": {},
        "tool_response_count": 0,
        "successful_tool_response_count": 0,
        "error_codes": {},
        "assistant_response_count": terminal_count,
        "explicit_terminal_success_count": 0,
        "explicit_terminal_output_count": 0,
        "terminal_success_count": terminal_count,
        "terminal_output_count": terminal_count,
        "terminal_response_count": terminal_count,
        "handled_error_count": 0,
        "unhandled_error_count": 0,
    }


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
            "endpoint_request_count": 0,
            "endpoint_response_count": 0,
            "endpoint_usable_response_count": 0,
            "semantic_assertion_count": 0,
            "semantic_assertions_passed": 0,
            "trace_assertion_count": 0,
            "trace_assertions_passed": 0,
            "trace_contract_verified": False,
            "trace_behavior_summary": {},
            "endpoint_request_summaries": [],
        },
    )
    assert result.status == "inconclusive"
    assert result.error_code == "invocation_failed"


def test_assessment_must_match_current_package(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    package = {
        "schema_version": "3.0.0",
        "target_kind": "issue",
        "issue_id": "issue-001",
        "agent_name": "weather-agent",
        "foundry_version": "7",
        "manifest_reference": "sha256:" + "c" * 64,
        "source_integrity": {
            "verified": True,
            "contract_digest": "sha256:" + "f" * 64,
        },
        "validation_mode": "deterministic",
        "n": 5,
        "k": 5,
        "execution_digest": "sha256:" + "1" * 64,
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
                "card_linked_trace_proof": _trace_proof(),
            }
        ],
        "endpoint_evidence": {
            "request_count": 1,
            "response_count": 1,
            "usable_response_count": 1,
            "trace_contract_verified": True,
            "semantic_assertion_count": 1,
            "semantic_assertions_passed": 1,
            "trace_assertion_count": 0,
            "trace_assertions_passed": 0,
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
                    "trace_assertion_count": 0,
                    "trace_assertions_passed": 0,
                    "trace_assertion_results": [],
                    "activation_gate": True,
                    "direct_terminal_response_count": 1,
                    "function_call_count": 0,
                }
            ],
        },
        "full_request_trace_proof": _trace_proof(),
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
    manifest = {
        "manifest_hash": package["manifest_reference"],
        "source_integrity": package["source_integrity"],
        "agents": [
            {
                "name": "weather-agent",
                "baseline": {"foundry_version": "1"},
                "issues": [
                    {
                        "issue_id": "issue-001",
                        "foundry_version": package["foundry_version"],
                    }
                ],
            }
        ],
    }
    (packages / "issue-001.json").write_text(
        json.dumps(package),
        encoding="utf-8",
    )
    assessment = {
        "schema_version": "2.0.0",
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
    assert (
        load_assessments(
            [path],
            {"issue-001"},
            packages,
            manifest,
        )["issue-001"]
        == assessment
    )
    stale_manifest = deepcopy(manifest)
    stale_manifest["manifest_hash"] = "sha256:" + "9" * 64
    with pytest.raises(ContractError, match="current evidence"):
        load_assessments(
            [path],
            {"issue-001"},
            packages,
            stale_manifest,
        )
    invalid_package = deepcopy(package)
    invalid_package["unexpected"] = True
    invalid_package["package_hash"] = content_hash(
        {
            key: value
            for key, value in invalid_package.items()
            if key != "package_hash"
        }
    )
    with pytest.raises(ContractError, match="assessment package is invalid"):
        _validate_package(tmp_path / "invalid.json", invalid_package)
    incomplete_card_package = deepcopy(package)
    card = incomplete_card_package["observed_insights"][0]
    card.update(
        {
            "agent_version": "",
            "title": "",
            "description": "",
            "category": "",
            "severity": "Medium",
            "proposed_fix": "",
        }
    )
    incomplete_card_package["package_hash"] = content_hash(
        {
            key: value
            for key, value in incomplete_card_package.items()
            if key != "package_hash"
        }
    )
    _validate_package(
        tmp_path / "incomplete-card.json",
        incomplete_card_package,
    )
    invalid_package = deepcopy(package)
    invalid_package["observed_insights"][0]["trace_count"] = "1"
    invalid_package["package_hash"] = content_hash(
        {
            key: value
            for key, value in invalid_package.items()
            if key != "package_hash"
        }
    )
    with pytest.raises(ContractError, match="assessment package is invalid"):
        _validate_package(tmp_path / "invalid.json", invalid_package)
    assessment["package_hash"] = "sha256:" + "c" * 64
    path.write_text(json.dumps(assessment), encoding="utf-8")
    with pytest.raises(ContractError, match="current evidence"):
        load_assessments([path], {"issue-001"}, packages, manifest)

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
    with pytest.raises(ContractError, match="MATCHED assessment"):
        load_assessments([path], {"issue-001"}, packages, manifest)


def test_assessment_excludes_cards_linked_to_foreign_operations() -> None:
    exact = SimpleNamespace(linked_operation_ids=("a" * 32,))
    mixed = SimpleNamespace(linked_operation_ids=("a" * 32, "b" * 32))
    foreign = SimpleNamespace(linked_operation_ids=("b" * 32,))
    unlinked = SimpleNamespace(linked_operation_ids=())

    assert _cards_for_operations(
        [exact, mixed, foreign, unlinked],
        {"a" * 32},
    ) == [exact]
    assert _linked_baseline_operations(exact, {"a" * 32}) == ("a" * 32,)
    with pytest.raises(ContractError, match="exclusively linked"):
        _linked_baseline_operations(mixed, {"a" * 32})
    with pytest.raises(ContractError, match="exclusively linked"):
        _linked_baseline_operations(foreign, {"a" * 32})


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
        "card_linked_trace_proof": _trace_proof(),
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
            "trace_assertion_count": 0,
            "trace_assertions_passed": 0,
            "trace_assertion_results": [],
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
        "trace_assertion_count": 0,
        "trace_assertions_passed": 0,
        "request_summaries": summaries,
    }
    proof = _trace_proof(operation_count=5, terminal_count=5)
    contract = {
        "request_count": 5,
        "terminal_response": "direct_prompt",
        "semantic_assertions": "required_per_request",
        "function_calling": "forbidden",
        "trace_operations": "uniform",
        "validation_mode": "baseline",
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


def test_baseline_aggregate_assertions_must_equal_request_summaries() -> None:
    package = _complete_prompt_baseline_package()
    package["endpoint_evidence"]["semantic_assertion_count"] = 6
    package["endpoint_evidence"]["semantic_assertions_passed"] = 6
    assert _baseline_evidence_complete(package) is False


def test_truncated_trace_proof_cannot_establish_clean_baseline() -> None:
    package = _complete_prompt_baseline_package()
    package["full_request_trace_proof"].pop("error_codes")
    assert _baseline_evidence_complete(package) is False


def test_baseline_aggregate_assertion_totals_must_match_requests() -> None:
    package = _complete_prompt_baseline_package()
    package["endpoint_evidence"]["semantic_assertion_count"] += 1
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
            "trace_operations": "uniform",
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
        "card_linked_trace_proof": _trace_proof(terminal_count=0),
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
    package["source_integrity"] = {
        "verified": True,
        "contract_digest": "sha256:" + "a" * 64,
    }
    summaries = package["endpoint_evidence"]["request_summaries"]
    summaries[0]["activation_gate"] = True
    summaries[0]["trace_assertion_count"] = 1
    summaries[0]["trace_assertions_passed"] = 1
    summaries[0]["trace_assertion_results"] = [
        {"assertion": "synthetic_trace_contract", "passed": True}
    ]
    package["endpoint_evidence"]["trace_assertion_count"] = 1
    package["endpoint_evidence"]["trace_assertions_passed"] = 1
    assert _issue_activation_complete(package) is True
    summaries[0]["trace_assertion_results"][0]["passed"] = False
    summaries[0]["trace_assertions_passed"] = 0
    package["endpoint_evidence"]["trace_assertions_passed"] = 0
    assert _issue_activation_complete(package) is False
    summaries[0]["trace_assertion_results"][0]["passed"] = True
    summaries[0]["trace_assertions_passed"] = 1
    package["endpoint_evidence"]["trace_assertions_passed"] = 1
    summaries[0]["assertion_results"][0]["passed"] = False
    summaries[0]["semantic_assertions_passed"] = 0
    assert _issue_activation_complete(package) is False


def test_assessment_prompt_rejects_self_reported_activation() -> None:
    prompt = (
        Path("src")
        / "agent_insights_quality"
        / "prompts"
        / "assessment.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(prompt.split())
    assert "Passed human-reviewed activation assertions" in prompt
    assert "exact `source_integrity` digest" in prompt
    assert "self-reported defect flag" in prompt
    assert "`INCOMPLETE` with `test_framework` ownership" in prompt
    assert "title, description, category, and linked traces all pass" in prompt
    assert "Severity and proposed fix" in prompt
    assert "at least one exact-run, exact-version linked trace" in prompt
    assert "do not return a `root_cause` field" in normalized


def test_issue_card_without_terminal_proof_stays_incomplete() -> None:
    card = {
        "reference": "sha256:" + "a" * 64,
        "title": "Synthetic card",
        "category": "output_quality",
        "severity": "medium",
        "card_linked_trace_proof": {"operation_count": 1},
    }
    evaluation = {
        "reference": card["reference"],
        "title": card["title"],
        "category": card["category"],
        "severity": card["severity"],
        "verdict": "correct",
        "finding_type": "MATCHED",
        "ownership": "none",
        "fields": {
            "title": True,
            "description": True,
            "category": True,
            "severity": True,
            "proposed_fix": True,
            "linked_traces": True,
        },
    }
    assessment = {
        "issue_id": "issue-001",
        "finding_type": "MATCHED",
        "card_evaluations": [evaluation],
    }
    with pytest.raises(ContractError, match="terminal proof"):
        _validate_issue_cards(assessment, {"observed_insights": [card]})
    evaluation.update(
        {
            "verdict": "incomplete",
            "finding_type": "INCOMPLETE",
            "ownership": "unresolved",
        }
    )
    assessment["finding_type"] = "INCOMPLETE"
    _validate_issue_cards(assessment, {"observed_insights": [card]})


def test_top_level_result_cannot_contradict_card_result() -> None:
    card = {
        "reference": "sha256:" + "a" * 64,
        "title": "Synthetic card",
        "category": "output_quality",
        "severity": "medium",
        "card_linked_trace_proof": _trace_proof(),
    }
    assessment = {
        "issue_id": "issue-001",
        "finding_type": "MISMATCHED",
        "card_evaluations": [
            {
                "reference": card["reference"],
                "title": card["title"],
                "category": card["category"],
                "severity": card["severity"],
                "verdict": "correct",
                "finding_type": "MATCHED",
                "ownership": "none",
                "fields": {
                    "title": True,
                    "description": True,
                    "category": True,
                    "severity": True,
                    "proposed_fix": True,
                    "linked_traces": True,
                },
            }
        ],
    }
    with pytest.raises(ContractError, match="MISMATCHED assessment"):
        _validate_issue_cards(assessment, {"observed_insights": [card]})


def test_matched_issue_allows_matching_diagnostic_field_failure() -> None:
    fields = {
        "title": True,
        "description": True,
        "category": True,
        "severity": True,
        "proposed_fix": True,
        "linked_traces": True,
    }
    card = {
        "reference": "sha256:" + "a" * 64,
        "title": "Synthetic card",
        "category": "output_quality",
        "severity": "medium",
        "card_linked_trace_proof": _trace_proof(),
    }
    assessment = {
        "issue_id": "issue-001",
        "verdict": "correct",
        "finding_type": "MATCHED",
        "fields": {**fields, "severity": False},
        "card_evaluations": [
            {
                "reference": card["reference"],
                "title": card["title"],
                "category": card["category"],
                "severity": card["severity"],
                "verdict": "correct",
                "finding_type": "MATCHED",
                "ownership": "none",
                "fields": fields,
            }
        ],
    }
    with pytest.raises(ContractError, match="identical score-correct"):
        _validate_issue_cards(assessment, {"observed_insights": [card]})
    assessment["card_evaluations"][0]["fields"]["severity"] = False
    assessment["card_evaluations"][0]["field_reasons"] = {
        "severity": "The severity is imprecise."
    }
    _validate_issue_cards(assessment, {"observed_insights": [card]})


@pytest.mark.parametrize(
    ("verdict", "finding_type"),
    [
        ("partially_useful", "PARTIAL"),
        ("incorrect", "MISMATCHED"),
    ],
)
def test_related_noncorrect_card_requires_a_failed_field(
    verdict: str,
    finding_type: str,
) -> None:
    fields = {
        "title": True,
        "description": True,
        "category": True,
        "severity": True,
        "proposed_fix": True,
        "linked_traces": True,
    }
    card = {
        "reference": "sha256:" + "a" * 64,
        "title": "Synthetic card",
        "category": "output_quality",
        "severity": "medium",
        "card_linked_trace_proof": _trace_proof(),
    }
    evaluation = {
        "reference": card["reference"],
        "title": card["title"],
        "category": card["category"],
        "severity": card["severity"],
        "verdict": verdict,
        "finding_type": finding_type,
        "ownership": "insight_engine",
        "fields": fields,
    }
    assessment = {
        "issue_id": "issue-001",
        "verdict": verdict,
        "finding_type": finding_type,
        "fields": fields,
        "card_evaluations": [evaluation],
    }
    with pytest.raises(ContractError, match="failed scoring field"):
        _validate_issue_cards(assessment, {"observed_insights": [card]})

    evaluation["fields"] = {**fields, "title": False}
    with pytest.raises(ContractError, match="field_reasons"):
        _validate_issue_cards(assessment, {"observed_insights": [card]})

    evaluation["field_reasons"] = {"title": "The title does not identify the issue."}
    with pytest.raises(ContractError, match="assessments require"):
        _validate_issue_cards(assessment, {"observed_insights": [card]})

    assessment["fields"] = {**fields, "title": False}
    _validate_issue_cards(assessment, {"observed_insights": [card]})


@pytest.mark.parametrize(
    ("verdict", "finding_type"),
    [
        ("incorrect", "NOISE"),
        ("incorrect", "DUPLICATE"),
    ],
)
def test_all_other_noncorrect_cards_require_a_failed_field(
    verdict: str,
    finding_type: str,
) -> None:
    fields = {
        "title": True,
        "description": True,
        "category": True,
        "severity": True,
        "proposed_fix": True,
        "linked_traces": True,
    }
    card = {
        "reference": "sha256:" + "a" * 64,
        "title": "Synthetic card",
        "category": "output_quality",
        "severity": "medium",
        "card_linked_trace_proof": _trace_proof(),
    }
    assessment = {
        "issue_id": "issue-001",
        "verdict": "incorrect",
        "finding_type": finding_type,
        "fields": fields,
        "card_evaluations": [
            {
                "reference": card["reference"],
                "title": card["title"],
                "category": card["category"],
                "severity": card["severity"],
                "verdict": verdict,
                "finding_type": finding_type,
                "ownership": "unresolved",
                "fields": fields,
            }
        ],
    }
    with pytest.raises(ContractError, match="failed scoring field"):
        _validate_issue_cards(assessment, {"observed_insights": [card]})


@pytest.mark.parametrize("finding_type", ["PARTIAL", "MISMATCHED"])
def test_partial_and_mismatched_cards_require_exact_field_reasons(
    finding_type: str,
) -> None:
    fields = {
        "title": False,
        "description": False,
        "category": True,
        "severity": True,
        "proposed_fix": True,
        "linked_traces": True,
    }
    card = {
        "reference": "sha256:" + "a" * 64,
        "title": "Synthetic card",
        "category": "output_quality",
        "severity": "medium",
        "card_linked_trace_proof": _trace_proof(),
    }
    verdict = "partially_useful" if finding_type == "PARTIAL" else "incorrect"
    evaluation = {
        "reference": card["reference"],
        "title": card["title"],
        "category": card["category"],
        "severity": card["severity"],
        "verdict": verdict,
        "finding_type": finding_type,
        "ownership": "insight_engine",
        "fields": fields,
    }
    assessment = {
        "issue_id": "issue-001",
        "verdict": verdict,
        "finding_type": finding_type,
        "fields": fields,
        "card_evaluations": [evaluation],
    }
    with pytest.raises(ContractError, match="field_reasons"):
        _validate_issue_cards(assessment, {"observed_insights": [card]})

    evaluation["field_reasons"] = {"title": "Reason for title."}
    with pytest.raises(ContractError, match="field_reasons"):
        _validate_issue_cards(assessment, {"observed_insights": [card]})

    evaluation["field_reasons"] = {
        "title": "Reason for title.",
        "description": "Reason for description.",
        "severity": "Extra reason not tied to a failed field.",
    }
    with pytest.raises(ContractError, match="field_reasons"):
        _validate_issue_cards(assessment, {"observed_insights": [card]})

    evaluation["field_reasons"] = {
        "title": "Reason for title.",
        "description": "Reason for description.",
    }
    _validate_issue_cards(assessment, {"observed_insights": [card]})


def test_duplicate_card_requires_a_resolvable_primary_reference() -> None:
    fields_all_true = {
        "title": True,
        "description": True,
        "category": True,
        "severity": True,
        "proposed_fix": True,
        "linked_traces": True,
    }
    fields_failed = {**fields_all_true, "title": False}
    primary_reference = "sha256:" + "a" * 64
    duplicate_reference = "sha256:" + "b" * 64
    primary_card = {
        "reference": primary_reference,
        "title": "Primary card",
        "category": "output_quality",
        "severity": "medium",
        "card_linked_trace_proof": _trace_proof(),
    }
    duplicate_card = {
        "reference": duplicate_reference,
        "title": "Duplicate card",
        "category": "output_quality",
        "severity": "medium",
        "card_linked_trace_proof": _trace_proof(),
    }
    primary_evaluation = {
        "reference": primary_reference,
        "title": primary_card["title"],
        "category": primary_card["category"],
        "severity": primary_card["severity"],
        "verdict": "partially_useful",
        "finding_type": "PARTIAL",
        "ownership": "insight_engine",
        "fields": fields_failed,
        "field_reasons": {"title": "The title misrepresents the routing failure."},
    }
    duplicate_evaluation = {
        "reference": duplicate_reference,
        "title": duplicate_card["title"],
        "category": duplicate_card["category"],
        "severity": duplicate_card["severity"],
        "verdict": "incorrect",
        "finding_type": "DUPLICATE",
        "ownership": "insight_engine",
        "fields": {field: False for field in fields_all_true},
    }
    assessment = {
        "issue_id": "issue-001",
        "verdict": "partially_useful",
        "finding_type": "PARTIAL",
        "fields": fields_failed,
        "card_evaluations": [primary_evaluation, duplicate_evaluation],
    }
    package = {"observed_insights": [primary_card, duplicate_card]}

    with pytest.raises(ContractError, match="duplicate_of"):
        _validate_issue_cards(assessment, package)

    duplicate_evaluation["duplicate_of"] = duplicate_reference
    with pytest.raises(ContractError, match="duplicate_of"):
        _validate_issue_cards(assessment, package)

    duplicate_evaluation["duplicate_of"] = "sha256:" + "9" * 64
    with pytest.raises(ContractError, match="duplicate_of"):
        _validate_issue_cards(assessment, package)

    primary_evaluation["finding_type"] = "NOISE"
    primary_evaluation["verdict"] = "incorrect"
    primary_evaluation["fields"] = {field: False for field in fields_all_true}
    duplicate_evaluation["duplicate_of"] = primary_reference
    assessment["finding_type"] = "DUPLICATE"
    assessment["verdict"] = "incorrect"
    with pytest.raises(ContractError, match="duplicate_of"):
        _validate_issue_cards(assessment, package)

    primary_evaluation["finding_type"] = "PARTIAL"
    primary_evaluation["verdict"] = "partially_useful"
    primary_evaluation["fields"] = fields_failed
    primary_evaluation["field_reasons"] = {
        "title": "The title misrepresents the routing failure."
    }
    assessment["finding_type"] = "PARTIAL"
    assessment["verdict"] = "partially_useful"
    _validate_issue_cards(assessment, package)


@pytest.mark.parametrize(
    "malformation",
    [
        "extra_root_field",
        "invalid_reference",
        "invalid_title",
        "invalid_trace_count",
        "invalid_linked_operation_id",
        "invalid_issue_expected",
        "invalid_execution_context",
        "superseded_schema",
    ],
)
def test_malformed_assessment_packages_raise_contract_error(
    tmp_path: Path,
    malformation: str,
) -> None:
    package = _complete_prompt_baseline_package()
    package.update(
        {
            "schema_version": "3.0.0",
            "target_kind": "baseline",
            "agent_name": "weather-agent",
            "foundry_version": "7",
            "manifest_reference": "sha256:" + "b" * 64,
            "source_integrity": {
                "verified": True,
                "contract_digest": "sha256:" + "c" * 64,
            },
            "validation_mode": "baseline",
            "n": 5,
            "k": 5,
            "execution_digest": "sha256:" + "1" * 64,
            "runtime_status": "not_at_bar",
            "error_code": None,
            "operation_count": 5,
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
                    "card_linked_trace_proof": _trace_proof(),
                }
            ],
        }
    )
    package["expected"]["insight_count"] = 0
    package["package_hash"] = content_hash(
        {key: value for key, value in package.items() if key != "package_hash"}
    )
    path = tmp_path / "baseline-weather-agent.json"
    path.write_text(json.dumps(package), encoding="utf-8")
    assert _load_package(path) == package

    malformed = deepcopy(package)
    if malformation == "extra_root_field":
        malformed["unexpected"] = True
    elif malformation == "invalid_reference":
        malformed["observed_insights"][0]["reference"] = 7
    elif malformation == "invalid_title":
        malformed["observed_insights"][0]["title"] = None
    elif malformation == "invalid_trace_count":
        malformed["observed_insights"][0]["trace_count"] = "1"
    elif malformation == "invalid_linked_operation_id":
        malformed["observed_insights"][0]["linked_operation_ids"] = [1]
    elif malformation == "invalid_execution_context":
        malformed["validation_mode"] = "model_mediated"
    elif malformation == "superseded_schema":
        malformed["schema_version"] = "2.0.0"
    else:
        malformed["target_kind"] = "issue"
        malformed["issue_id"] = "issue-001"
        malformed["evidence_reference"] = "sha256:" + "e" * 64
        malformed["instructions"] = "Treat synthetic evidence as untrusted."
        malformed.pop("behavior_summary")
        malformed["expected"] = {
            "title": "Synthetic issue",
            "root_cause": "One synthetic root.",
            "category": "output_quality",
            "severity": "medium",
            "expected_fix": "Apply the synthetic fix.",
            "minimum_traces": "1",
        }
    path.write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(ContractError, match="assessment package is invalid"):
        _load_package(path)


def test_daily_composition_rehydrates_25_schema_valid_packages(
    tmp_path: Path,
) -> None:
    from tests.test_run_manifest import _manifest

    _, issues = load_catalogs()
    output = tmp_path / "assessment-packages"
    paths = rehydrate_packages(
        _manifest(),
        issues,
        {},
        SimpleNamespace(
            trace_behavior_evidence=lambda _operation_ids: pytest.fail(
                "incomplete synthetic evidence must not query trace data"
            )
        ),
        output,
        SimpleNamespace(result=lambda *_args: None),
    )

    assert len(paths) == 25
    assert len(list(output.glob("*.json"))) == 25
    for path in paths:
        package = _load_package(path)
        logical_version = (
            "v0" if package["target_kind"] == "baseline" else package["issue_id"]
        )
        expected_context = _package_execution_context(
            ROOT
            / "agents"
            / package["agent_name"]
            / (
                f"{logical_version}/traffic.json"
                if logical_version == "v0"
                else f"issues/{logical_version}/traffic.json"
            )
        )
        assert {
            key: package[key]
            for key in ("validation_mode", "n", "k", "execution_digest")
        } == expected_context
