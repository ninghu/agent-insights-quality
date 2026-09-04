from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_insights_quality.models import (
    InvocationEvidence,
    RequestCompletionEvidence,
    SemanticAssertionEvidence,
    TraceAssertionEvidence,
    request_completion_payload,
)
from agent_insights_quality.runner import (
    _baseline_validation_decision,
    _issue_activation_decision,
)
from agent_insights_quality.validation_trace_gap_policy import (
    ROLE_PASS_POLICY,
    build_trace_maturity_proof,
    daily_target_decision,
    role_pass_summary,
    target_evidence_decided,
    validate_trace_maturity_proof,
)


def _attempt(
    index: int,
    *,
    complete: bool,
    observation: bool,
    missing: str | None = None,
) -> dict:
    return {
        "index": index,
        "complete": complete,
        "observation": observation,
        "error_code": None if complete else "missing_evidence",
        "endpoint_complete": missing != "endpoint",
        "identity_complete": missing != "identity",
        "semantic_evidence_complete": missing != "semantic",
        "trace_evidence_complete": missing != "trace",
        "assertions_contradicted": complete and not observation,
    }


def _six_pass_four_arbitrary_misses() -> list[dict]:
    return [
        *[
            _attempt(index, complete=True, observation=True)
            for index in range(1, 7)
        ],
        _attempt(7, complete=True, observation=False),
        _attempt(8, complete=False, observation=False, missing="endpoint"),
        _attempt(9, complete=False, observation=True, missing="semantic"),
        _attempt(10, complete=False, observation=False, missing="trace"),
    ]


def test_role_pass_summary_accepts_six_strict_passes_and_classifies_misses() -> None:
    summary = role_pass_summary(
        target_role="issue",
        n=10,
        k=6,
        attempts=_six_pass_four_arbitrary_misses(),
    )

    assert summary == {
        "policy": ROLE_PASS_POLICY,
        "target_role": "issue",
        "required_pass_count": 6,
        "pass_count": 6,
        "pass_attempt_indices": [1, 2, 3, 4, 5, 6],
        "miss_count": 4,
        "miss_attempt_indices": [7, 8, 9, 10],
        "miss_counts": {
            "complete_non_pass": 1,
            "endpoint_incomplete": 1,
            "identity_incomplete": 0,
            "semantic_incomplete": 1,
            "trace_incomplete": 1,
            "other_incomplete": 0,
        },
    }
    assert target_evidence_decided(n=10, k=6, role_pass_count=6)


def test_baseline_and_issue_share_six_role_pass_threshold() -> None:
    attempts = _six_pass_four_arbitrary_misses()

    for role in ("baseline", "issue"):
        summary = role_pass_summary(
            target_role=role,
            n=10,
            k=6,
            attempts=attempts,
        )
        assert summary is not None
        assert summary["pass_count"] == 6
        assert target_evidence_decided(
            n=10,
            k=6,
            role_pass_count=int(summary["pass_count"]),
        )


def test_paired_v0_requires_six_complete_zero_defect_controls() -> None:
    six_controls = [
        *[
            _attempt(index, complete=True, observation=False)
            for index in range(1, 7)
        ],
        _attempt(7, complete=True, observation=True),
        _attempt(8, complete=True, observation=True),
        _attempt(9, complete=False, observation=False, missing="identity"),
        _attempt(10, complete=False, observation=True, missing="trace"),
    ]
    accepted = role_pass_summary(
        target_role="paired_v0",
        n=10,
        k=6,
        attempts=six_controls,
    )
    rejected = role_pass_summary(
        target_role="paired_v0",
        n=10,
        k=6,
        attempts=[
            *six_controls[:5],
            _attempt(6, complete=True, observation=True),
            *six_controls[6:],
        ],
    )

    assert accepted is not None
    assert accepted["pass_count"] == 6
    assert accepted["miss_counts"]["complete_non_pass"] == 2
    assert target_evidence_decided(n=10, k=6, role_pass_count=6)
    assert rejected is not None
    assert rejected["pass_count"] == 5
    assert not target_evidence_decided(n=10, k=6, role_pass_count=5)


def _invocation(*, observed: int, semantic_non_pass: int = 0) -> InvocationEvidence:
    summaries = []
    for index in range(1, 11):
        role_pass = index <= observed
        semantic_pass = role_pass or index > observed + semantic_non_pass
        trace_pass = role_pass
        summaries.append(
            RequestCompletionEvidence(
                request_index=index - 1,
                response_count=1,
                usable_response=True,
                semantic_assertion_count=1,
                semantic_assertions_passed=int(semantic_pass),
                assertion_results=(
                    SemanticAssertionEvidence(
                        "semantic",
                        semantic_pass,
                        evidence_sufficient=True,
                    ),
                ),
                activation_gate=True,
                direct_terminal_response_count=1,
                function_call_count=0,
                trace_assertion_count=1,
                trace_assertions_passed=int(trace_pass),
                trace_assertion_results=(
                    TraceAssertionEvidence(
                        "trace",
                        trace_pass,
                        evidence_sufficient=True,
                    ),
                ),
                error_code=None if role_pass else "assertion_failed",
            )
        )
    return InvocationEvidence(
        operation_ids=tuple(f"{index:032x}" for index in range(1, 11)),
        response_references=tuple(
            f"response-{index}" for index in range(1, 11)
        ),
        started_at="2026-09-03T00:00:00+00:00",
        completed_at="2026-09-03T00:01:00+00:00",
        request_count=10,
        allow_window_correlation=False,
        response_count=10,
        usable_response_count=10,
        request_summaries=tuple(summaries),
    )


def test_daily_issue_accepts_healthcare_nine_plus_one_semantic_non_pass() -> None:
    invocation = _invocation(observed=9, semantic_non_pass=1)

    decided, summary = _issue_activation_decision(
        {
            "validation_mode": "deterministic",
            "n": 10,
            "k": 6,
            "required_surfaces": ["semantic", "trace"],
        },
        invocation,
    )

    assert decided is True
    assert summary is not None
    assert summary["pass_count"] == 9
    assert summary["miss_counts"]["complete_non_pass"] == 1


def test_daily_issue_accepts_travel_six_and_eight_of_ten() -> None:
    for observed in (6, 8):
        decided, summary = _issue_activation_decision(
            {
                "validation_mode": "model_mediated",
                "n": 10,
                "k": 6,
                "required_surfaces": ["semantic", "trace"],
            },
            _invocation(observed=observed),
        )

        assert decided is True
        assert summary is not None
        assert summary["pass_count"] == observed
        assert summary["miss_count"] == 10 - observed


def test_daily_baseline_accepts_six_strict_healthy_attempts() -> None:
    decided, summary = _baseline_validation_decision(_invocation(observed=6))

    assert decided is True
    assert summary is not None
    assert summary["target_role"] == "baseline"
    assert summary["pass_count"] == 6


def test_daily_target_keeps_incomplete_observation_as_miss() -> None:
    invocation = _invocation(observed=6)
    summaries = [
        request_completion_payload(item)
        for item in invocation.request_summaries
    ]
    summaries[0]["trace_assertion_results"][0]["evidence_sufficient"] = False

    decided, summary = daily_target_decision(
        target_role="issue",
        validation_mode="deterministic",
        n=10,
        k=6,
        required_surfaces=["semantic", "trace"],
        summaries=summaries,
        identity_verified=True,
    )

    assert decided is False
    assert summary is not None
    assert summary["pass_count"] == 5
    assert summary["miss_counts"]["trace_incomplete"] == 1


def test_trace_maturity_proof_remains_integrity_bound() -> None:
    end = datetime(2026, 9, 3, 0, 1, tzinfo=UTC)
    proof = build_trace_maturity_proof(
        evidence_window_start="2026-09-03T00:00:00+00:00",
        evidence_window_end=end.isoformat(),
        snapshot_observed_at=end + timedelta(seconds=1080),
        maximum_hydration_seconds=900,
        stabilization_seconds=180,
    )

    assert proof is not None
    assert validate_trace_maturity_proof(proof) == proof[
        "maturity_proof_digest"
    ]
