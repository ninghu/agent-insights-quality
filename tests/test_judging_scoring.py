from __future__ import annotations

from copy import deepcopy

import pytest

from agent_insights_quality.contracts import ContractError
from agent_insights_quality.judging import (
    export_judge_package,
    import_judgment,
    project_evidence,
)
from agent_insights_quality.runtime import content_hash
from agent_insights_quality.runtime import read_json_object
from agent_insights_quality.scoring import score_run
from agent_insights_quality.scoring import deterministic_violations


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
TRACE = "1" * 32


def plan() -> dict:
    return {
        "schema_version": "1.0.0",
        "plan_id": "aiq-20260821",
        "report_date": "2026-08-21",
        "created_at": "2026-08-21T07:00:00Z",
        "catalog_version": "1.0.0",
        "catalog_hash": SHA_A,
        "planner_version": "1.0.0",
        "seed": 42,
        "engine": {
            "endpoint_reference": SHA_B,
            "build": "build-1",
            "generator_model": "gpt-5.6-terra",
        },
        "project": {
            "name": "aiq-20260821",
            "resource_reference": SHA_C,
            "expires_on": "2026-11-19",
        },
        "assignments": [
            {
                "scenario_id": "aiq-scn-010-fault",
                "scenario_version": "1.0.0",
                "agent_id": "aiq-001-weather",
                "agent_name": "aiq-001-weather-v1",
                "agent_version_digest": SHA_A,
                "wave": 1,
                "traffic_seed": 7,
                "window": {
                    "start": "2026-08-21T07:00:00Z",
                    "end": "2026-08-21T07:10:00Z",
                },
                "expected": {
                    "category": "tool_call_failures",
                    "severity": "high",
                    "finding_count": 1,
                },
            },
            {
                "scenario_id": "aiq-scn-011-healthy",
                "scenario_version": "1.0.0",
                "agent_id": "aiq-001-weather",
                "agent_name": "aiq-001-weather-v1",
                "agent_version_digest": SHA_A,
                "wave": 0,
                "traffic_seed": 8,
                "window": {
                    "start": "2026-08-21T07:10:00Z",
                    "end": "2026-08-21T07:20:00Z",
                },
                "expected": {
                    "category": "none",
                    "severity": "none",
                    "finding_count": 0,
                },
            },
        ],
    }


def raw_bundle(scenario_id: str, *, healthy: bool = False) -> dict:
    window = (
        ("2026-08-21T07:10:00Z", "2026-08-21T07:20:00Z")
        if healthy
        else ("2026-08-21T07:00:00Z", "2026-08-21T07:10:00Z")
    )
    insights = [] if healthy else [
        {
            "id": "insight-1",
            "title": "Wrong tool selected",
            "description": "The incompatible tool cannot satisfy the request.",
            "category": "tool_call_failures",
            "severity": "high",
            "trace_count": 1,
            "trace_ids": [TRACE],
            "proposed_fix": "Constrain tool selection to forecast.",
            "fix_kind": "prompt_patch",
            "tool_references": ["forecast"],
            "signature": SHA_B,
            "evidence_fingerprint": SHA_C,
        }
    ]
    return {
        "schema_version": "1.0.0",
        "bundle_id": (
            "00000000-0000-4000-8000-000000000011"
            if healthy
            else "00000000-0000-4000-8000-000000000010"
        ),
        "plan_id": "aiq-20260821",
        "scenario": {"id": scenario_id, "version": "1.0.0"},
        "agent": {
            "id": "aiq-001-weather",
            "name": "aiq-001-weather-v1",
            "type": "prompt",
            "version_digest": SHA_A,
            "available_tools": ["forecast"],
        },
        "run": {
            "run_id": f"run-{scenario_id}",
            "window_start": window[0],
            "window_end": window[1],
            "engine_build": "build-1",
            "generator_model": "gpt-5.6-terra",
        },
        "ground_truth": {
            "root_cause": "none" if healthy else "The agent selected an incompatible tool.",
            "category": "none" if healthy else "tool_call_failures",
            "severity": "none" if healthy else "high",
            "fix_boundary": "No change expected." if healthy else "Prompt selection boundary.",
        },
        "mutation": {
            "healthy_digest": SHA_A,
            "faulted_digest": SHA_B,
            "sanitized_delta": "No mutation." if healthy else "Bias to incompatible tool.",
        },
        "trace_evidence": [
            {
                "trace_id": TRACE,
                "span_ids": ["2" * 16],
                "summary": "Synthetic tool selection trace.",
                "artifact_reference": SHA_A,
                "project_reference": SHA_C,
                "agent_id": "aiq-001-weather",
                "version_digest": SHA_A,
                "observed_at": window[0],
            }
        ],
        "insights": insights,
        "previous_insight": None,
    }


def judgment(bundle: dict, *, valid: bool = True) -> dict:
    value = {
        "schema_version": "1.0.0",
        "bundle_id": bundle["bundle_id"],
        "judge_role": "primary",
        "model": "gpt-5.6-sol",
        "prompt_version": "primary-v1",
        "prompt_hash": export_judge_package(bundle, "primary")["prompt_hash"],
        "evidence_schema_version": "1.0.0",
        "mapping": {
            "scenario_id": bundle["scenario"]["id"],
            "insight_id": "insight-1",
        },
        "verdict": "correct",
        "attributes": {
            name: {"passes": True, "reason": "Supported by bounded evidence."}
            for name in (
                "root_cause",
                "title",
                "description",
                "proposed_fix",
                "category",
                "severity",
                "linked_traces",
                "meaningfulness",
                "evidence_localization",
                "actionability",
            )
        },
        "relationships": {
            "duplicate_of": [],
            "fragmented_with": [],
            "umbrella_for": [],
        },
        "defect_fingerprint": None,
        "confidence": 0.99,
        "reasoning": "The insight exactly identifies the injected root cause.",
    }
    value["output_hash"] = content_hash(value)
    if not valid:
        value["confidence"] = 0.5
    return value


def test_blinded_package_contains_no_primary_result() -> None:
    bundle = project_evidence(raw_bundle("aiq-scn-010-fault"))
    package = export_judge_package(bundle, "blinded_verifier")
    assert package["judge_role"] == "blinded_verifier"
    assert "primary_judgment" not in package
    assert "primary_confidence" not in package
    assert "primary_reasoning" not in package


def test_judgment_import_rejects_changed_output_after_hash() -> None:
    bundle = project_evidence(raw_bundle("aiq-scn-010-fault"))
    package = export_judge_package(bundle, "primary")
    result = judgment(bundle, valid=False)
    with pytest.raises(ContractError, match="output_hash"):
        import_judgment(package, result)


def test_scoring_recomputes_full_at_bar_metrics(monkeypatch) -> None:
    fault = project_evidence(raw_bundle("aiq-scn-010-fault"))
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))
    monkeypatch.setattr(
        "agent_insights_quality.scoring.validate_daily_plan_semantics",
        lambda *_args: None,
    )
    score = score_run(plan(), [fault, healthy], [judgment(fault)])
    assert score["verdict"] == "AT BAR"
    assert score["rates"]["high_severity_recall"] == 1
    assert score["rates"]["healthy_noise_rate"] == 0
    assert score["rates"]["distinctness_rate"] == 1


def test_missing_judgment_is_inconclusive(monkeypatch) -> None:
    fault = project_evidence(raw_bundle("aiq-scn-010-fault"))
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))
    monkeypatch.setattr(
        "agent_insights_quality.scoring.validate_daily_plan_semantics",
        lambda *_args: None,
    )
    score = score_run(plan(), [fault, healthy], [])
    assert score["verdict"] == "INCONCLUSIVE"
    assert "unresolved_judgment" in score["violations"]


@pytest.mark.parametrize(
    ("mutate", "violation"),
    [
        (
            lambda value: value["attributes"]["actionability"].update({"passes": False}),
            "attribute_correctness",
        ),
        (
            lambda value: value["relationships"]["duplicate_of"].append("prior-insight"),
            "duplication",
        ),
        (
            lambda value: value["relationships"]["fragmented_with"].append("fragment-2"),
            "fragmentation",
        ),
        (
            lambda value: value["relationships"]["umbrella_for"].append("cause-2"),
            "umbrella",
        ),
    ],
)
def test_scoring_enforces_semantic_and_collection_gates(
    monkeypatch,
    mutate,
    violation,
) -> None:
    fault = project_evidence(raw_bundle("aiq-scn-010-fault"))
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))
    result = judgment(fault)
    mutate(result)
    result["output_hash"] = content_hash(
        {key: value for key, value in result.items() if key != "output_hash"}
    )
    monkeypatch.setattr(
        "agent_insights_quality.scoring.validate_daily_plan_semantics",
        lambda *_args: None,
    )
    score = score_run(plan(), [fault, healthy], [result])
    assert score["verdict"] == "NOT AT BAR"
    assert violation in score["violations"]


def test_evidence_projection_enforces_five_insight_limit() -> None:
    value = raw_bundle("aiq-scn-010-fault")
    value["insights"] = [
        {**deepcopy(value["insights"][0]), "id": f"insight-{index}"}
        for index in range(6)
    ]
    with pytest.raises(ContractError, match="too long"):
        project_evidence(value)


def test_judge_export_rejects_pii_shaped_evidence() -> None:
    value = raw_bundle("aiq-scn-010-fault")
    value["trace_evidence"][0]["summary"] = "Synthetic user 123-45-6789"
    bundle = project_evidence(value)
    with pytest.raises(ContractError, match="secret or PII"):
        export_judge_package(bundle, "primary")


def test_trace_count_and_stale_version_are_rejected() -> None:
    value = project_evidence(raw_bundle("aiq-scn-010-fault"))
    value["insights"][0]["trace_count"] = 2
    value["trace_evidence"][0]["version_digest"] = SHA_B
    value["bundle_hash"] = content_hash(
        {key: item for key, item in value.items() if key != "bundle_hash"}
    )
    violations, failures = deterministic_violations(plan(), [value])
    assert failures == 1
    assert {"structural_failure", "cross_version_stale", "provenance_failure"} <= violations


def test_strict_json_rejects_duplicate_keys_and_non_finite_numbers(tmp_path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"value": 1, "value": 2}', encoding="ascii")
    with pytest.raises(ContractError, match="duplicate object key"):
        read_json_object(duplicate)
    non_finite = tmp_path / "nan.json"
    non_finite.write_text('{"value": NaN}', encoding="ascii")
    with pytest.raises(ContractError, match="non-finite"):
        read_json_object(non_finite)
