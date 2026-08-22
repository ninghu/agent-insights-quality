from __future__ import annotations

from copy import deepcopy
from datetime import date

import pytest

from agent_insights_quality.contracts import (
    ContractError,
    load_agent_manifests,
    load_scenario_catalog,
)
from agent_insights_quality.judging import (
    export_judge_package,
    import_judgment,
    judgment_target_insight_ids,
    project_evidence,
)
from agent_insights_quality.artifact_io import content_hash
from agent_insights_quality.artifact_io import read_json_object
from agent_insights_quality.scoring import case_to_insight_mappings, score_run
from agent_insights_quality.scoring import deterministic_violations
from agent_insights_quality.planning import generate_daily_plan


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
        "policy_version": "1.0.0",
        "policy_hash": SHA_B,
        "planner_version": "1.0.0",
        "seed": 42,
        "selection_mode": "rotating_daily",
        "human_daily_contract": True,
        "selection": {
            "cycle": {
                "id": "cycle-1-aaaaaaaaaaaa",
                "number": 1,
                "business_day": 5,
                "weekday": "Friday",
                "length_business_days": 5,
                "full_coverage_horizon_business_days": 5,
            },
            "mandatory_scenario_ids": [
                "aiq-scn-010-fault",
                "aiq-scn-011-healthy",
            ],
            "rotating_scenario_ids": [],
            "selected_scenario_ids": [
                "aiq-scn-010-fault",
                "aiq-scn-011-healthy",
            ],
            "omitted_scenario_ids": [],
            "selection_reasons": {
                "aiq-scn-010-fault": "p0_fault_daily",
                "aiq-scn-011-healthy": "healthy_control_daily",
            },
        },
        "limits": {
            "expected_insight_cap_per_agent": 4,
            "expected_root_cap_per_run": 4,
            "actual_insight_count_rule": "exact_expected",
            "expected_cap_enforced": True,
        },
        "per_agent_expected_totals": {
            "aiq-001-weather": 1,
            "aiq-002-healthcare": 0,
            "aiq-003-finance": 0,
            "aiq-004-travel": 0,
            "aiq-005-support": 0,
        },
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
                "selection_reason": "p0_fault_daily",
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
                "selection_reason": "healthy_control_daily",
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
        "version_sequence": {
            "phase": "healthy" if healthy else "faulted",
            "run_id": (
                "run-00-aiq-001-weather"
                if healthy
                else "run-01-aiq-001-weather"
            ),
            "version_digest": SHA_A,
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
        "prior_trace_ids": [],
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
        "prompt_version": "primary-v2",
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


def complete_judgments(
    bundles: list[dict],
    judgments: list[dict],
) -> list[dict]:
    completed = list(judgments)
    mappings = {
        (
            item["mapping"]["scenario_id"],
            item["mapping"]["insight_id"],
        )
        for item in completed
    }
    for bundle in bundles:
        scenario_id = bundle["scenario"]["id"]
        if (
            None in judgment_target_insight_ids(bundle)
            and (scenario_id, None) not in mappings
        ):
            completed.append(judgment(bundle, insight_id=None))
    return completed


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
            {
                "id": "aiq-scn-012-umbrella",
                "expected": {
                    "root_cause": "Two independent root causes must remain distinct.",
                    "fix": {"boundary": "Keep both independent fixes."},
                    "category": "tool_call_failures",
                    "severity": "high",
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
    score = score_run(
        plan(),
        [fault, healthy],
        complete_judgments([fault, healthy], [judgment(fault)]),
    )
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

    with pytest.raises(ContractError, match="every produced insight"):
        case_to_insight_mappings(plan(), [fault, healthy], [])


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


def test_explicit_false_cannot_suppress_empty_bundle_null_target() -> None:
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))
    healthy["no_insight_target_required"] = False
    healthy["bundle_hash"] = content_hash(
        {key: value for key, value in healthy.items() if key != "bundle_hash"}
    )

    assert judgment_target_insight_ids(healthy) == (None,)


def test_noise_judgment_cannot_replace_required_no_insight_judgment(
    synthetic_contracts,
) -> None:
    fault = project_evidence(raw_bundle("aiq-scn-010-fault"))
    healthy_raw = raw_bundle("aiq-scn-011-healthy", healthy=True)
    noise = deepcopy(fault["insights"][0])
    healthy_raw["run_noise_insights"] = [deepcopy(noise)]
    healthy = project_evidence(healthy_raw)
    healthy["insights"] = [deepcopy(noise)]
    healthy["no_insight_target_required"] = True
    healthy["finding_count"] = {
        "expected": 0,
        "actual": 1,
        "verdict": "NOT_AT_BAR",
        "reason": "extra_noise",
    }
    healthy["bundle_hash"] = content_hash(
        {key: value for key, value in healthy.items() if key != "bundle_hash"}
    )

    score = score_run(
        plan(),
        [fault, healthy],
        [judgment(fault), judgment(healthy, insight_id=noise["id"])],
    )

    assert score["verdict"] == "INCONCLUSIVE"
    assert "unresolved_judgment" in score["violations"]


@pytest.mark.parametrize(
    ("linked_trace", "prior_trace_ids", "expected_violation"),
    [
        (SHA_C, [], "provenance_failure"),
        (SHA_B, [SHA_B], "cross_version_stale"),
    ],
)
def test_noise_only_insight_trace_provenance_is_validated(
    synthetic_contracts,
    linked_trace: str,
    prior_trace_ids: list[str],
    expected_violation: str,
) -> None:
    raw = raw_bundle("aiq-scn-010-fault")
    noise = deepcopy(raw["insights"][0])
    noise["trace_ids"] = [linked_trace]
    raw["insights"] = []
    raw["run_noise_insights"] = [noise]
    raw["prior_trace_ids"] = prior_trace_ids
    raw["finding_count"] = {"actual": 0}
    raw["run_finding_count"] = {"expected": 1, "actual": 1}
    bundle = project_evidence(raw)

    violations, _ = deterministic_violations(
        plan(),
        [bundle],
        synthetic_catalog(),
    )

    assert expected_violation in violations


def test_unowned_run_noise_is_unresolved_even_with_null_judgment(
    synthetic_contracts,
) -> None:
    fault = project_evidence(raw_bundle("aiq-scn-010-fault"))
    healthy_raw = raw_bundle("aiq-scn-011-healthy", healthy=True)
    noise = deepcopy(fault["insights"][0])
    healthy_raw["run_noise_insights"] = [noise]
    healthy_raw["run_finding_count"] = {"expected": 0, "actual": 1}
    healthy = project_evidence(healthy_raw)
    judgments = [
        judgment(fault),
        judgment(healthy, insight_id=None),
    ]

    score = score_run(plan(), [fault, healthy], judgments)

    assert score["verdict"] == "INCONCLUSIVE"
    assert "unresolved_judgment" in score["violations"]
    with pytest.raises(ContractError, match="one primary owner"):
        case_to_insight_mappings(plan(), [fault, healthy], judgments)


def test_noise_only_package_can_target_100_cards_plus_null() -> None:
    raw = raw_bundle("aiq-scn-011-healthy", healthy=True)
    template = deepcopy(raw_bundle("aiq-scn-010-fault")["insights"][0])
    raw["run_noise_insights"] = []
    for index in range(100):
        insight = deepcopy(template)
        insight["id"] = f"noise-{index:03d}"
        insight["signature"] = content_hash({"noise_signature": index})
        insight["evidence_fingerprint"] = content_hash(
            {"noise_evidence": index}
        )
        raw["run_noise_insights"].append(insight)
    raw["run_finding_count"] = {"expected": 0, "actual": 100}
    bundle = project_evidence(raw)
    bundle["insights"] = deepcopy(bundle["run_noise_insights"])
    bundle["no_insight_target_required"] = True
    bundle["finding_count"] = {
        "expected": 0,
        "actual": 100,
        "verdict": "NOT_AT_BAR",
        "reason": "extra_noise",
    }
    bundle["bundle_hash"] = content_hash(
        {key: value for key, value in bundle.items() if key != "bundle_hash"}
    )

    targets = judgment_target_insight_ids(bundle)

    assert len(targets) == 101
    assert targets[-1] is None


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

    judgments = complete_judgments([fault, healthy], judgments)
    score = score_run(plan(), [fault, healthy], judgments)

    assert score["verdict"] == "NOT AT BAR"
    assert score["complete"] is True
    assert "finding_count_mismatch" in score["violations"]
    assert "extra_noise" in score["violations"]
    assert score["counts"]["false_positives"] == 5
    assert "duplication" not in score["violations"]


def test_missing_expected_count_is_a_miss_not_inconclusive(synthetic_contracts) -> None:
    expected_plan = plan()
    expected_plan["assignments"][0]["expected"]["finding_count"] = 2
    fault = project_evidence(raw_bundle("aiq-scn-010-fault"))
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))

    judgments = complete_judgments([fault, healthy], [judgment(fault)])
    score = score_run(expected_plan, [fault, healthy], judgments)

    assert score["verdict"] == "NOT AT BAR"
    assert score["complete"] is True
    assert score["counts"]["false_negatives"] == 1
    assert "finding_count_mismatch" in score["violations"]
    assert "missing_findings" in score["violations"]
    outcome = case_to_insight_mappings(
        expected_plan, [fault, healthy], judgments
    )[0]
    assert outcome["expected_count"] == 2
    assert outcome["observed_count"] == 1
    assert outcome["verdict"] == "mixed"


def test_f1_is_zero_when_positive_counts_have_no_true_positives(
    synthetic_contracts,
) -> None:
    fault = project_evidence(raw_bundle("aiq-scn-010-fault"))
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))
    incorrect = judgment(fault)
    incorrect["verdict"] = "incorrect_noise"
    incorrect["output_hash"] = content_hash(
        {key: value for key, value in incorrect.items() if key != "output_hash"}
    )

    score = score_run(
        plan(),
        [fault, healthy],
        complete_judgments([fault, healthy], [incorrect]),
    )

    assert score["counts"]["false_positives"] == 1
    assert score["counts"]["false_negatives"] == 1
    assert score["rates"]["precision"] == 0
    assert score["rates"]["overall_recall"] == 0
    assert score["rates"]["f1"] == 0


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

    judgments = complete_judgments([fault, healthy], [correct, noise])
    score = score_run(plan(), [fault, healthy], judgments)
    outcome = case_to_insight_mappings(
        plan(), [fault, healthy], judgments
    )[0]

    assert score["verdict"] == "NOT AT BAR"
    assert score["counts"]["false_positives"] == 1
    assert outcome["expected_count"] == 1
    assert outcome["observed_count"] == 2
    assert outcome["verdict"] == "mixed"


def _multi_assignment_run(
    *,
    first_insight_count: int,
    umbrella_insight_count: int,
    shared_run_view: bool = False,
) -> tuple[dict, list[dict], list[dict]]:
    value = plan()
    umbrella_assignment = deepcopy(value["assignments"][0])
    umbrella_assignment["scenario_id"] = "aiq-scn-012-umbrella"
    umbrella_assignment["expected"]["finding_count"] = 2
    value["assignments"].append(umbrella_assignment)
    value["coverage"]["scenario_count"] = 3

    raw_first = raw_bundle("aiq-scn-010-fault")
    first_template = deepcopy(raw_first["insights"][0])
    raw_first["insights"] = []
    for index in range(first_insight_count):
        item = deepcopy(first_template)
        item["id"] = f"first-{index}"
        item["signature"] = content_hash({"first-signature": index})
        item["evidence_fingerprint"] = content_hash({"first-evidence": index})
        raw_first["insights"].append(item)

    raw_umbrella = raw_bundle("aiq-scn-012-umbrella")
    raw_umbrella["bundle_id"] = "00000000-0000-4000-8000-000000000012"
    raw_umbrella["ground_truth"].update(
        {
            "root_cause": "Two independent root causes must remain distinct.",
            "fix_boundary": "Keep both independent fixes.",
        }
    )
    umbrella_template = deepcopy(raw_umbrella["insights"][0])
    raw_umbrella["insights"] = []
    for index in range(umbrella_insight_count):
        item = deepcopy(umbrella_template)
        item["id"] = f"umbrella-{index}"
        item["signature"] = content_hash({"umbrella-signature": index})
        item["evidence_fingerprint"] = content_hash({"umbrella-evidence": index})
        raw_umbrella["insights"].append(item)
    run_total = first_insight_count + umbrella_insight_count
    raw_first["ground_truth"]["finding_count"] = 1
    raw_first["finding_count"] = {"actual": first_insight_count}
    raw_first["run_finding_count"] = {
        "expected": 3,
        "actual": run_total,
    }
    raw_umbrella["ground_truth"]["finding_count"] = 2
    raw_umbrella["finding_count"] = {"actual": umbrella_insight_count}
    raw_umbrella["run_finding_count"] = {
        "expected": 3,
        "actual": run_total,
    }
    if shared_run_view:
        raw_first["run_noise_insights"] = deepcopy(raw_umbrella["insights"])
        raw_umbrella["run_noise_insights"] = deepcopy(raw_first["insights"])

    first = project_evidence(raw_first)
    umbrella = project_evidence(raw_umbrella)
    run_insight_ids = [
        *(item["id"] for item in first["insights"]),
        *(item["id"] for item in umbrella["insights"]),
    ]
    run_accounting = {
        "unique_insight_count": run_total,
        "assigned_count": run_total,
        "umbrella_noise_count": 0,
        "extra_noise_count": 0,
        "sampled_count": run_total,
        "details_truncated": False,
        "insight_references": [
            content_hash({"insight_id": insight_id})
            for insight_id in run_insight_ids
        ],
    }
    for bundle in (first, umbrella):
        bundle["run_insight_accounting"] = deepcopy(run_accounting)
        bundle["bundle_hash"] = content_hash(
            {
                key: item
                for key, item in bundle.items()
                if key != "bundle_hash"
            }
        )
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))
    judgments = [
        *(judgment(first, insight_id=item["id"]) for item in first["insights"]),
        *(judgment(umbrella, insight_id=item["id"]) for item in umbrella["insights"]),
    ]
    bundles = [first, umbrella, healthy]
    return value, bundles, complete_judgments(bundles, judgments)


def test_exact_count_aggregates_multi_assignment_run_with_umbrella_count_two(
    synthetic_contracts,
) -> None:
    value, bundles, judgments = _multi_assignment_run(
        first_insight_count=1,
        umbrella_insight_count=2,
        shared_run_view=True,
    )

    score = score_run(value, bundles, judgments)
    outcomes = case_to_insight_mappings(value, bundles, judgments)

    assert score["verdict"] == "AT BAR"
    assert "finding_count_mismatch" not in score["violations"]
    assert score["counts"]["true_positives"] == 3
    assert score["counts"]["false_positives"] == 0
    assert score["rates"]["precision"] == 1
    references = [
        item["insight_reference"]
        for outcome in outcomes
        for item in outcome["insights"]
    ]
    assert len(references) == len(set(references)) == 3


def test_shared_run_collection_context_does_not_duplicate_physical_judgments(
    synthetic_contracts,
) -> None:
    value, bundles, judgments = _multi_assignment_run(
        first_insight_count=1,
        umbrella_insight_count=2,
        shared_run_view=True,
    )
    score = score_run(value, bundles, judgments)
    outcomes = case_to_insight_mappings(value, bundles, judgments)
    umbrella = next(
        item
        for item in outcomes
        if item["scenario_id"] == "aiq-scn-012-umbrella"
    )

    assert score["verdict"] == "AT BAR"
    assert len(
        [
            item
            for item in judgments
            if item["mapping"]["insight_id"] is not None
        ]
    ) == 3
    assert umbrella["observed_count"] == 2
    assert umbrella["verdict"] == "correct"


def test_duplicate_physical_primary_judgments_are_inconclusive(
    synthetic_contracts,
) -> None:
    value, bundles, _judgments = _multi_assignment_run(
        first_insight_count=1,
        umbrella_insight_count=1,
    )
    first, umbrella, healthy = bundles
    shared = deepcopy(first["insights"][0])
    umbrella["insights"] = [shared]
    umbrella["bundle_hash"] = content_hash(
        {
            key: item
            for key, item in umbrella.items()
            if key != "bundle_hash"
        }
    )
    judgments = complete_judgments(
        bundles,
        [
            judgment(first, insight_id=shared["id"]),
            judgment(umbrella, insight_id=shared["id"]),
        ],
    )

    score = score_run(value, bundles, judgments)

    assert score["verdict"] == "INCONCLUSIVE"
    assert "judge_schema_failure" in score["violations"]


def test_run_count_match_preserves_mixed_per_scenario_diagnostics(
    synthetic_contracts,
) -> None:
    value, bundles, judgments = _multi_assignment_run(
        first_insight_count=2,
        umbrella_insight_count=1,
    )
    judgments[1]["verdict"] = "incorrect_noise"
    judgments[1]["output_hash"] = content_hash(
        {key: item for key, item in judgments[1].items() if key != "output_hash"}
    )

    score = score_run(value, bundles, judgments)
    outcomes = case_to_insight_mappings(value, bundles, judgments)

    assert "finding_count_mismatch" not in score["violations"]
    assert "extra_noise" not in score["violations"]
    assert "missing_findings" not in score["violations"]
    fault_outcomes = [item for item in outcomes if item["expected_count"]]
    assert [item["verdict"] for item in fault_outcomes] == ["mixed", "mixed"]


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
    score = score_run(
        plan(),
        [fault, healthy],
        complete_judgments([fault, healthy], [result]),
    )
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
    score = score_run(
        plan(),
        [fault, healthy],
        complete_judgments([fault, healthy], [result]),
    )
    assert score["verdict"] == "INCONCLUSIVE"
    assert "judge_schema_failure" in score["violations"]


def test_catalog_ground_truth_mismatch_fails_provenance(
    synthetic_contracts,
) -> None:
    raw = raw_bundle("aiq-scn-010-fault")
    raw["ground_truth"]["fix_boundary"] = "An unreviewed fix boundary."
    fault = project_evidence(raw)
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))
    score = score_run(
        plan(),
        [fault, healthy],
        complete_judgments([fault, healthy], [judgment(fault)]),
    )
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
    score = score_run(
        plan(),
        [fault, healthy],
        complete_judgments([fault, healthy], [result]),
    )
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


def test_evidence_projection_preserves_exact_total_while_bounding_samples() -> None:
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
    bundle = project_evidence(value)
    assert len(bundle["insights"]) == 100
    assert bundle["finding_count"]["actual"] == 101
    assert bundle["run_finding_count"]["actual"] == 101
    assert bundle["run_insight_accounting"]["sampled_count"] == 100
    assert bundle["run_insight_accounting"]["details_truncated"] is True


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


def test_incompatible_fix_is_not_at_bar_not_inconclusive(
    synthetic_contracts,
) -> None:
    fault = project_evidence(raw_bundle("aiq-scn-010-fault"))
    fault["insights"][0]["tool_references"] = ["unavailable_tool"]
    fault["bundle_hash"] = content_hash(
        {key: item for key, item in fault.items() if key != "bundle_hash"}
    )
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))

    score = score_run(
        plan(),
        [fault, healthy],
        complete_judgments([fault, healthy], [judgment(fault)]),
    )

    assert score["verdict"] == "NOT AT BAR"
    assert score["complete"] is True
    assert score["counts"]["structural_failures"] == 0
    assert "capability_fix_mismatch" in score["violations"]
    assert "structural_failure" not in score["violations"]


@pytest.mark.parametrize(
    "scenario_id",
    [
        "aiq-scn-058-cross-version-stale-finding",
        "aiq-scn-060-fixed-issue-recurrence",
    ],
)
def test_sequential_evidence_binds_current_and_prior_phase_versions(
    scenario_id: str,
) -> None:
    full_plan = generate_daily_plan(date(2026, 8, 21), full_catalog=True)
    assignment = next(
        item for item in full_plan["assignments"] if item["scenario_id"] == scenario_id
    )
    plan_subset = deepcopy(full_plan)
    plan_subset["assignments"] = [assignment]
    catalog = load_scenario_catalog()
    scenario = next(item for item in catalog["scenarios"] if item["id"] == scenario_id)
    agents = load_agent_manifests()
    agent = next(item for item in agents if item["id"] == assignment["agent_id"])
    current = assignment["version_sequence"][-1]
    prior = assignment["version_sequence"][0]
    raw = raw_bundle("aiq-scn-010-fault")
    raw["bundle_id"] = (
        "00000000-0000-4000-8000-000000000058"
        if scenario_id.startswith("aiq-scn-058")
        else "00000000-0000-4000-8000-000000000060"
    )
    raw["plan_id"] = full_plan["plan_id"]
    raw["scenario"] = {"id": scenario_id, "version": assignment["scenario_version"]}
    raw["agent"] = {
        "id": assignment["agent_id"],
        "name": assignment["agent_name"],
        "type": assignment["agent_type"],
        "version_digest": current["digest"],
        "available_tools": agent["implementation"]["representative_tools"],
    }
    raw["run"]["run_id"] = assignment["run_id"]
    raw["run"]["engine_build"] = full_plan["engine"]["build"]
    raw["version_sequence"] = {
        "phase": current["phase"],
        "run_id": assignment["run_id"],
        "version_digest": current["digest"],
    }
    raw["ground_truth"] = {
        "root_cause": scenario["expected"]["root_cause"],
        "category": scenario["expected"]["category"],
        "severity": scenario["expected"]["severity"],
        "fix_boundary": scenario["expected"]["fix"]["boundary"],
    }
    raw["trace_evidence"][0].update(
        {
            "project_reference": full_plan["project"]["resource_reference"],
            "agent_id": assignment["agent_id"],
            "version_digest": current["digest"],
        }
    )
    raw["insights"][0]["fix_kind"] = "code_change"
    raw["insights"][0]["tool_references"] = []
    raw["previous_insight"] = {
        "id": "prior-insight",
        "fingerprint": SHA_B,
        "phase": prior["phase"],
        "run_id": assignment["run_id"],
        "version_digest": prior["digest"],
    }
    bundle = project_evidence(raw)

    violations, _ = deterministic_violations(
        plan_subset,
        [bundle],
        catalog,
        agents,
    )
    assert "provenance_failure" not in violations
    assert "cross_version_stale" not in violations

    stale_current = deepcopy(bundle)
    stale_current["trace_evidence"][0]["version_digest"] = prior["digest"]
    stale_current["bundle_hash"] = content_hash(
        {key: item for key, item in stale_current.items() if key != "bundle_hash"}
    )
    violations, _ = deterministic_violations(
        plan_subset,
        [stale_current],
        catalog,
        agents,
    )
    assert {"provenance_failure", "cross_version_stale"} <= violations

    stale_prior = deepcopy(bundle)
    stale_prior["previous_insight"]["version_digest"] = current["digest"]
    stale_prior["bundle_hash"] = content_hash(
        {key: item for key, item in stale_prior.items() if key != "bundle_hash"}
    )
    violations, _ = deterministic_violations(
        plan_subset,
        [stale_prior],
        catalog,
        agents,
    )
    assert {"provenance_failure", "cross_version_stale"} <= violations


def _lifecycle_plan() -> dict:
    lifecycle_plan = plan()
    assignment = lifecycle_plan["assignments"][0]
    assignment["version_sequence"] = [
        {
            "phase": "faulted",
            "version_key": "faulted",
            "digest": SHA_B,
            "window": assignment["window"],
        },
        {
            "phase": "corrected",
            "version_key": "corrected",
            "digest": "sha256:" + ("d" * 64),
            "window": assignment["window"],
        },
        {
            "phase": "recurred",
            "version_key": "recurred",
            "digest": SHA_A,
            "window": assignment["window"],
        },
    ]
    return lifecycle_plan


def test_missing_prior_card_with_proven_links_is_not_at_bar_not_inconclusive(
    synthetic_contracts,
) -> None:
    lifecycle_plan = _lifecycle_plan()
    current_raw = raw_bundle("aiq-scn-010-fault")
    current_raw["version_sequence"]["phase"] = "recurred"
    current_raw["prior_trace_ids"] = [SHA_B]
    current = project_evidence(current_raw)
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))

    current_score = score_run(
        lifecycle_plan,
        [current, healthy],
        complete_judgments([current, healthy], [judgment(current)]),
    )
    assert current_score["verdict"] == "AT BAR"

    stale = deepcopy(current)
    stale["insights"][0]["trace_ids"].append(SHA_B)
    stale["insights"][0]["trace_count"] = 2
    stale["bundle_hash"] = content_hash(
        {key: value for key, value in stale.items() if key != "bundle_hash"}
    )
    stale_score = score_run(
        lifecycle_plan,
        [stale, healthy],
        complete_judgments([stale, healthy], [judgment(stale)]),
    )
    assert stale_score["verdict"] == "NOT AT BAR"
    assert "cross_version_stale" in stale_score["violations"]
    assert "provenance_failure" not in stale_score["violations"]

    prior_only = deepcopy(current)
    prior_only["insights"][0]["trace_ids"] = [SHA_B]
    prior_only["bundle_hash"] = content_hash(
        {key: value for key, value in prior_only.items() if key != "bundle_hash"}
    )
    prior_only_score = score_run(
        lifecycle_plan,
        [prior_only, healthy],
        complete_judgments([prior_only, healthy], [judgment(prior_only)]),
    )
    assert prior_only_score["verdict"] == "NOT AT BAR"
    assert "cross_version_stale" in prior_only_score["violations"]
    assert "provenance_failure" not in prior_only_score["violations"]

    unknown = deepcopy(current)
    unknown["insights"][0]["trace_ids"].append(SHA_C)
    unknown["insights"][0]["trace_count"] = 2
    unknown["bundle_hash"] = content_hash(
        {key: value for key, value in unknown.items() if key != "bundle_hash"}
    )
    unknown_score = score_run(
        lifecycle_plan,
        [unknown, healthy],
        complete_judgments([unknown, healthy], [judgment(unknown)]),
    )
    assert unknown_score["verdict"] == "INCONCLUSIVE"
    assert "provenance_failure" in unknown_score["violations"]


def test_missing_prior_card_preserves_recurrence_miss_semantics(
    synthetic_contracts,
) -> None:
    lifecycle_plan = _lifecycle_plan()
    missed_raw = raw_bundle("aiq-scn-010-fault")
    missed_raw["version_sequence"]["phase"] = "recurred"
    missed_raw["insights"] = []
    missed = project_evidence(missed_raw)
    healthy = project_evidence(raw_bundle("aiq-scn-011-healthy", healthy=True))

    score = score_run(
        lifecycle_plan,
        [missed, healthy],
        complete_judgments([missed, healthy], []),
    )

    assert score["verdict"] == "NOT AT BAR"
    assert score["complete"] is True
    assert score["counts"]["false_negatives"] == 1
    assert "missing_findings" in score["violations"]
    assert "provenance_failure" not in score["violations"]


def test_single_version_evidence_rejects_previous_insight_before_judging() -> None:
    raw = raw_bundle("aiq-scn-010-fault")
    raw["previous_insight"] = {
        "id": "foreign-prior",
        "fingerprint": SHA_B,
        "phase": "faulted",
        "run_id": "foreign-run",
        "version_digest": SHA_B,
    }

    with pytest.raises(
        ContractError,
        match="single-version evidence cannot include previous_insight",
    ):
        project_evidence(raw)


def test_single_version_bundle_with_previous_insight_is_provenance_failure() -> None:
    bundle = project_evidence(raw_bundle("aiq-scn-010-fault"))
    bundle["previous_insight"] = {
        "id": "foreign-prior",
        "fingerprint": SHA_B,
        "phase": "faulted",
        "run_id": "foreign-run",
        "version_digest": SHA_B,
    }
    bundle["bundle_hash"] = content_hash(
        {key: item for key, item in bundle.items() if key != "bundle_hash"}
    )

    violations, _ = deterministic_violations(
        plan(),
        [bundle],
        synthetic_catalog(),
    )

    assert {"provenance_failure", "cross_version_stale"} <= violations


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
