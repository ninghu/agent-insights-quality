from __future__ import annotations

from copy import deepcopy

import pytest

from agent_insights_quality.contracts import ContractError
from agent_insights_quality.judging import (
    export_judge_package,
    import_judgment,
    project_evidence,
)
from agent_insights_quality.artifact_io import content_hash
from agent_insights_quality.artifact_io import read_json_object
from agent_insights_quality.scoring import case_to_insight_mappings, score_run
from agent_insights_quality.scoring import deterministic_violations


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
TRACE = "1" * 32


def plan() -> dict:
    value = {
        "schema_version": "1.0.0",
        "plan_id": "aiq-20260821",
        "plan_digest": SHA_A,
        "artifact_directory": "reports/daily/2026/08/21",
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
        "coverage": {
            "scenario_count": 2,
            "healthy_control_count": 1,
            "families": ["synthetic"],
            "categories": ["none", "tool_call_failures"],
            "severities": ["high", "none"],
            "agent_types": ["prompt"],
        },
        "assignments": [
            {
                "scenario_id": "aiq-scn-010-fault",
                "scenario_version": "1.0.0",
                "family": "synthetic",
                "conflict_tags": ["synthetic"],
                "run_id": "run-01-aiq-001-weather",
                "agent_id": "aiq-001-weather",
                "agent_name": "aiq-001-weather-v1",
                "agent_type": "prompt",
                "agent_version_digest": SHA_A,
                "version_sequence": [
                    {
                        "phase": "faulted",
                        "version_key": "faulted",
                        "digest": SHA_A,
                        "window": {
                            "start": "window://run-01-aiq-001-weather/faulted/start-inclusive",
                            "end": "window://run-01-aiq-001-weather/faulted/end-exclusive",
                        },
                    }
                ],
                "wave": 1,
                "traffic_seed": 7,
                "traffic_seed_namespace": "synthetic-fault-v1",
                "traffic_recipe_id": "traffic-synthetic-fault-v1",
                "traffic_requests": 1,
                "lifecycle": "injected_immutable_version",
                "window": {
                    "start": "window://run-01-aiq-001-weather/faulted/start-inclusive",
                    "end": "window://run-01-aiq-001-weather/faulted/end-exclusive",
                },
                "expected": {
                    "category": "tool_call_failures",
                    "severity": "high",
                    "finding_count": 1,
                    "validation_targets": ["root_cause"],
                },
            },
            {
                "scenario_id": "aiq-scn-011-healthy",
                "scenario_version": "1.0.0",
                "family": "synthetic",
                "conflict_tags": ["healthy-control"],
                "run_id": "run-00-aiq-001-weather",
                "agent_id": "aiq-001-weather",
                "agent_name": "aiq-001-weather-v1",
                "agent_type": "prompt",
                "agent_version_digest": SHA_A,
                "version_sequence": [
                    {
                        "phase": "healthy",
                        "version_key": "healthy",
                        "digest": SHA_A,
                        "window": {
                            "start": "window://run-00-aiq-001-weather/healthy/start-inclusive",
                            "end": "window://run-00-aiq-001-weather/healthy/end-exclusive",
                        },
                    }
                ],
                "wave": 0,
                "traffic_seed": 8,
                "traffic_seed_namespace": "synthetic-healthy-v1",
                "traffic_recipe_id": "traffic-synthetic-healthy-v1",
                "traffic_requests": 1,
                "lifecycle": "current_immutable_version",
                "window": {
                    "start": "window://run-00-aiq-001-weather/healthy/start-inclusive",
                    "end": "window://run-00-aiq-001-weather/healthy/end-exclusive",
                },
                "expected": {
                    "category": "none",
                    "severity": "none",
                    "finding_count": 0,
                    "validation_targets": ["healthy_noise"],
                },
            },
        ],
    }
    return value


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
            "run_id": (
                "run-00-aiq-001-weather"
                if healthy
                else "run-01-aiq-001-weather"
            ),
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


def judgment(
    bundle: dict,
    *,
    valid: bool = True,
    insight_id: str | None = "insight-1",
) -> dict:
    package = export_judge_package(bundle, "primary")
    value = {
        "schema_version": "1.0.0",
        "bundle_id": bundle["bundle_id"],
        "bundle_hash": bundle["bundle_hash"],
        "package_hash": package["package_hash"],
        "judge_role": "primary",
        "model": "gpt-5.6-sol",
        "prompt_version": "primary-v1",
        "prompt_hash": package["prompt_hash"],
        "evidence_schema_version": "1.0.0",
        "mapping": {
            "scenario_id": bundle["scenario"]["id"],
            "insight_id": insight_id,
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


def synthetic_catalog() -> dict:
    return {
        "scenarios": [
            {
                "id": "aiq-scn-010-fault",
                "expected": {
                    "root_cause": "The agent selected an incompatible tool.",
                    "fix": {"boundary": "Prompt selection boundary."},
                    "category": "tool_call_failures",
                    "severity": "high",
                },
            },
            {
                "id": "aiq-scn-011-healthy",
                "expected": {
                    "root_cause": "none",
                    "fix": {"boundary": "No change expected."},
                    "category": "none",
                    "severity": "none",
                },
            },
        ]
    }


@pytest.fixture
def synthetic_contracts(monkeypatch):
    monkeypatch.setattr(
        "agent_insights_quality.scoring.validate_daily_plan_semantics",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "agent_insights_quality.scoring.load_scenario_catalog",
        lambda *_args: synthetic_catalog(),
    )


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


def test_scoring_recomputes_full_at_bar_metrics(synthetic_contracts) -> None:
    fault = project_evidence(raw_bundle("aiq-scn-010-fault"))
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))
    score = score_run(plan(), [fault, healthy], [judgment(fault)])
    assert score["verdict"] == "AT BAR"
    assert score["rates"]["high_severity_recall"] == 1
    assert score["rates"]["healthy_noise_rate"] == 0
    assert score["rates"]["distinctness_rate"] == 1


def test_missing_judgment_is_inconclusive(synthetic_contracts) -> None:
    fault = project_evidence(raw_bundle("aiq-scn-010-fault"))
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))
    score = score_run(plan(), [fault, healthy], [])
    assert score["verdict"] == "INCONCLUSIVE"
    assert "unresolved_judgment" in score["violations"]


def test_zero_insight_judgment_accepts_null_mapping(synthetic_contracts) -> None:
    fault = project_evidence(raw_bundle("aiq-scn-010-fault"))
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))

    score = score_run(
        plan(),
        [fault, healthy],
        [judgment(fault), judgment(healthy, insight_id=None)],
    )

    assert score["verdict"] == "AT BAR"
    assert "judge_schema_failure" not in score["violations"]


def test_null_mapping_cannot_replace_produced_insight_coverage(
    synthetic_contracts,
) -> None:
    fault = project_evidence(raw_bundle("aiq-scn-010-fault"))
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))

    score = score_run(plan(), [fault, healthy], [judgment(fault, insight_id=None)])

    assert score["verdict"] == "INCONCLUSIVE"
    assert "judge_schema_failure" in score["violations"]
    assert "unresolved_judgment" in score["violations"]


def test_extra_findings_are_noise_and_explicit_not_at_bar(synthetic_contracts) -> None:
    raw = raw_bundle("aiq-scn-010-fault")
    template = raw["insights"][0]
    raw["insights"] = []
    for index in range(6):
        insight = deepcopy(template)
        insight["id"] = f"insight-{index}"
        insight["signature"] = content_hash({"signature": index})
        insight["evidence_fingerprint"] = content_hash({"evidence": index})
        raw["insights"].append(insight)
    fault = project_evidence(raw)
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))
    judgments = [
        judgment(fault, insight_id=f"insight-{index}")
        for index in range(6)
    ]

    score = score_run(plan(), [fault, healthy], judgments)

    assert score["verdict"] == "NOT AT BAR"
    assert score["complete"] is True
    assert "finding_count_mismatch" in score["violations"]
    assert score["counts"]["false_positives"] == 5
    assert "duplication" not in score["violations"]


def test_missing_expected_count_is_a_miss_not_inconclusive(synthetic_contracts) -> None:
    expected_plan = plan()
    expected_plan["assignments"][0]["expected"]["finding_count"] = 2
    fault = project_evidence(raw_bundle("aiq-scn-010-fault"))
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))

    score = score_run(expected_plan, [fault, healthy], [judgment(fault)])

    assert score["verdict"] == "NOT AT BAR"
    assert score["complete"] is True
    assert score["counts"]["false_negatives"] == 1
    assert "finding_count_mismatch" in score["violations"]
    outcome = case_to_insight_mappings(
        expected_plan, [fault, healthy], [judgment(fault)]
    )[0]
    assert outcome["expected_count"] == 2
    assert outcome["observed_count"] == 1
    assert outcome["verdict"] == "mixed"


def test_one_expected_plus_extra_noise_is_explicit_mixed_outcome(
    synthetic_contracts,
) -> None:
    raw = raw_bundle("aiq-scn-010-fault")
    extra = deepcopy(raw["insights"][0])
    extra["id"] = "insight-noise"
    extra["signature"] = content_hash({"signature": "noise"})
    extra["evidence_fingerprint"] = content_hash({"evidence": "noise"})
    raw["insights"].append(extra)
    fault = project_evidence(raw)
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))
    correct = judgment(fault)
    noise = judgment(fault, insight_id="insight-noise")
    noise["verdict"] = "incorrect_noise"
    noise["output_hash"] = content_hash(
        {key: value for key, value in noise.items() if key != "output_hash"}
    )

    score = score_run(plan(), [fault, healthy], [correct, noise])
    outcome = case_to_insight_mappings(
        plan(), [fault, healthy], [correct, noise]
    )[0]

    assert score["verdict"] == "NOT AT BAR"
    assert score["counts"]["false_positives"] == 1
    assert outcome["expected_count"] == 1
    assert outcome["observed_count"] == 2
    assert outcome["verdict"] == "mixed"


@pytest.mark.parametrize("mutation", ["missing_traces", "null_insights"])
def test_invalid_or_evidence_incomplete_bundle_is_inconclusive(
    synthetic_contracts,
    mutation,
) -> None:
    fault = project_evidence(raw_bundle("aiq-scn-010-fault"))
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))
    if mutation == "missing_traces":
        fault["trace_evidence"] = []
    else:
        fault["insights"] = None
    fault["bundle_hash"] = content_hash(
        {key: value for key, value in fault.items() if key != "bundle_hash"}
    )

    score = score_run(plan(), [fault, healthy], [])

    assert score["verdict"] == "INCONCLUSIVE"
    assert score["complete"] is False
    assert "structural_failure" in score["violations"]
    assert score["counts"]["completed_scenarios"] == 1


def test_evidence_projection_rejects_empty_traces() -> None:
    raw = raw_bundle("aiq-scn-010-fault")
    raw["trace_evidence"] = []
    with pytest.raises(ContractError, match="non-empty"):
        project_evidence(raw)


def test_low_confidence_primary_judgment_is_inconclusive(
    synthetic_contracts,
) -> None:
    fault = project_evidence(raw_bundle("aiq-scn-010-fault"))
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))
    result = judgment(fault)
    result["confidence"] = 0.79
    result["output_hash"] = content_hash(
        {key: value for key, value in result.items() if key != "output_hash"}
    )
    score = score_run(plan(), [fault, healthy], [result])
    assert score["verdict"] == "INCONCLUSIVE"
    assert "unresolved_judgment" in score["violations"]


def test_judgment_package_hash_must_match_exact_bundle(
    synthetic_contracts,
) -> None:
    fault = project_evidence(raw_bundle("aiq-scn-010-fault"))
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))
    result = judgment(fault)
    result["package_hash"] = SHA_B
    result["output_hash"] = content_hash(
        {key: value for key, value in result.items() if key != "output_hash"}
    )
    score = score_run(plan(), [fault, healthy], [result])
    assert score["verdict"] == "INCONCLUSIVE"
    assert "judge_schema_failure" in score["violations"]


def test_catalog_ground_truth_mismatch_fails_provenance(
    synthetic_contracts,
) -> None:
    raw = raw_bundle("aiq-scn-010-fault")
    raw["ground_truth"]["fix_boundary"] = "An unreviewed fix boundary."
    fault = project_evidence(raw)
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))
    score = score_run(plan(), [fault, healthy], [judgment(fault)])
    assert score["verdict"] == "INCONCLUSIVE"
    assert "provenance_failure" in score["violations"]


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
    synthetic_contracts,
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
    score = score_run(plan(), [fault, healthy], [result])
    assert score["verdict"] == "NOT AT BAR"
    assert violation in score["violations"]


def test_evidence_projection_preserves_insights_up_to_structural_bound() -> None:
    value = raw_bundle("aiq-scn-010-fault")
    value["insights"] = [
        {
            **deepcopy(value["insights"][0]),
            "id": f"insight-{index}",
            "signature": content_hash({"signature": index}),
            "evidence_fingerprint": content_hash({"evidence": index}),
        }
        for index in range(6)
    ]
    assert len(project_evidence(value)["insights"]) == 6


def test_evidence_projection_rejects_more_than_structural_bound() -> None:
    value = raw_bundle("aiq-scn-010-fault")
    value["insights"] = [
        {
            **deepcopy(value["insights"][0]),
            "id": f"insight-{index}",
            "signature": content_hash({"signature": index}),
            "evidence_fingerprint": content_hash({"evidence": index}),
        }
        for index in range(101)
    ]
    with pytest.raises(ContractError, match="too long"):
        project_evidence(value)


def test_judge_export_rejects_pii_shaped_evidence() -> None:
    value = raw_bundle("aiq-scn-010-fault")
    value["trace_evidence"][0]["summary"] = "Synthetic user 123-45-6789"
    bundle = project_evidence(value)
    with pytest.raises(ContractError, match="secret or PII"):
        export_judge_package(bundle, "primary")


@pytest.mark.parametrize(
    "sensitive",
    [
        "Payment card 4111 1111 1111 1111",
        "account number: 123456789",
        "api_key=abcdefgh12345678",
        "secret=abcdefgh12345678",
        "token=abcdefgh12345678",
        "https://storage.example/item?sig=abcdefghijklmnop",
    ],
)
def test_all_judge_exports_use_comprehensive_privacy_scan(sensitive) -> None:
    value = raw_bundle("aiq-scn-010-fault")
    value["trace_evidence"][0]["summary"] = sensitive
    bundle = project_evidence(value)
    with pytest.raises(ContractError, match="secret or PII"):
        export_judge_package(bundle, "blinded_verifier")


def test_judge_export_scans_unconstrained_run_identifier() -> None:
    value = raw_bundle("aiq-scn-010-fault")
    value["run"]["run_id"] = "person@example.test"
    bundle = project_evidence(value)
    with pytest.raises(ContractError, match="secret or PII"):
        export_judge_package(bundle, "primary")


def test_overlapping_relationships_do_not_make_distinctness_negative(
    synthetic_contracts,
) -> None:
    fault = project_evidence(raw_bundle("aiq-scn-010-fault"))
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))
    result = judgment(fault)
    result["relationships"]["duplicate_of"] = ["prior"]
    result["relationships"]["fragmented_with"] = ["other"]
    result["output_hash"] = content_hash(
        {key: value for key, value in result.items() if key != "output_hash"}
    )
    score = score_run(plan(), [fault, healthy], [result])
    assert score["rates"]["distinctness_rate"] == 0


def test_trace_count_and_stale_version_are_rejected() -> None:
    value = project_evidence(raw_bundle("aiq-scn-010-fault"))
    value["insights"][0]["trace_count"] = 2
    value["trace_evidence"][0]["version_digest"] = SHA_B
    value["bundle_hash"] = content_hash(
        {key: item for key, item in value.items() if key != "bundle_hash"}
    )
    violations, failures = deterministic_violations(
        plan(), [value], synthetic_catalog()
    )
    assert failures == 1
    assert {"structural_failure", "cross_version_stale", "provenance_failure"} <= violations


def test_engine_and_agent_capabilities_are_provenance_bound() -> None:
    value = project_evidence(raw_bundle("aiq-scn-010-fault"))
    value["run"]["engine_build"] = "forged-build"
    value["agent"]["available_tools"].append("unregistered-tool")
    value["bundle_hash"] = content_hash(
        {key: item for key, item in value.items() if key != "bundle_hash"}
    )
    violations, _ = deterministic_violations(
        plan(), [value], synthetic_catalog()
    )
    assert "provenance_failure" in violations


def test_strict_json_rejects_duplicate_keys_and_non_finite_numbers(tmp_path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"value": 1, "value": 2}', encoding="ascii")
    with pytest.raises(ContractError, match="duplicate object key"):
        read_json_object(duplicate)
    non_finite = tmp_path / "nan.json"
    non_finite.write_text('{"value": NaN}', encoding="ascii")
    with pytest.raises(ContractError, match="non-finite"):
        read_json_object(non_finite)
