from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from agent_insights_quality.models import (
    RequestCompletionEvidence,
    SemanticAssertionEvidence,
    TraceAssertionEvidence,
)
from agent_insights_quality.runner import (
    _baseline_validation_decision,
    _issue_activation_decision,
)
from agent_insights_quality.validation_copilot import (
    _trace_unknown_acceptance,
)
from agent_insights_quality.validation_trace_gap_policy import (
    TRACE_UNKNOWN_ACCEPTANCE_POLICY,
    build_trace_maturity_proof,
    target_evidence_decided,
    trace_unknown_acceptance,
)
from agent_insights_quality.models import InvocationEvidence

HASH = "sha256:" + ("a" * 64)


def _daily_maturity_proof() -> dict:
    end = datetime(2026, 9, 3, 0, 1, tzinfo=UTC)
    proof = build_trace_maturity_proof(
        evidence_window_start="2026-09-03T00:00:00+00:00",
        evidence_window_end=end.isoformat(),
        snapshot_observed_at=end + timedelta(seconds=1080),
        maximum_hydration_seconds=900,
        stabilization_seconds=180,
    )
    assert proof is not None
    return proof


def _attempts(
    *,
    observations: int,
    complete_misses: int,
    unknowns: int,
) -> list[dict]:
    attempts = []
    for index in range(1, observations + complete_misses + unknowns + 1):
        observed = index <= observations
        complete = index <= observations + complete_misses
        attempts.append(
            {
                "index": index,
                "complete": complete,
                "observation": observed,
                "error_code": None if complete else "missing_evidence",
                "endpoint_complete": True,
                "identity_complete": True,
                "semantic_evidence_complete": True,
                "trace_evidence_complete": complete,
                "assertions_contradicted": False,
            }
        )
    return attempts


@pytest.mark.parametrize(
    ("observations", "complete_misses", "unknowns"),
    [(6, 0, 4), (6, 2, 2), (8, 0, 2)],
)
def test_issue_trace_unknown_accepts_reviewed_bounds(
    observations: int,
    complete_misses: int,
    unknowns: int,
) -> None:
    acceptance = trace_unknown_acceptance(
        target_role="issue",
        validation_mode="deterministic",
        n=10,
        k=6,
        required_surfaces=["semantic", "trace"],
        attempts=_attempts(
            observations=observations,
            complete_misses=complete_misses,
            unknowns=unknowns,
        ),
        maturity_proof_digest=HASH,
    )

    assert acceptance == {
        "policy": TRACE_UNKNOWN_ACCEPTANCE_POLICY,
        "target_role": "issue",
        "observation_count": observations,
        "unknown_attempt_indices": list(
            range(observations + complete_misses + 1, 11)
        ),
        "maturity_proof_digest": HASH,
    }


def test_complete_issue_misses_count_only_against_six_of_ten() -> None:
    assert (
        trace_unknown_acceptance(
            target_role="issue",
            validation_mode="model_mediated",
            n=10,
            k=6,
            required_surfaces=["semantic", "trace"],
            attempts=_attempts(
                observations=6,
                complete_misses=4,
                unknowns=0,
            ),
            maturity_proof_digest=None,
        )
        is None
    )
    assert target_evidence_decided(
        target_role="issue",
        n=10,
        k=6,
        complete_count=10,
        observation_count=6,
        trace_unknown_acceptance=None,
    )


def test_baseline_and_control_accept_four_trace_unknowns() -> None:
    baseline = trace_unknown_acceptance(
        target_role="baseline",
        validation_mode="baseline",
        n=10,
        k=6,
        required_surfaces=["semantic", "trace"],
        attempts=_attempts(
            observations=6,
            complete_misses=0,
            unknowns=4,
        ),
        maturity_proof_digest=HASH,
    )
    controls = _attempts(observations=0, complete_misses=6, unknowns=4)
    paired = trace_unknown_acceptance(
        target_role="paired_v0",
        validation_mode="deterministic",
        n=10,
        k=6,
        required_surfaces=["trace"],
        attempts=controls,
        maturity_proof_digest=HASH,
    )

    assert baseline is not None
    assert baseline["target_role"] == "baseline"
    assert baseline["unknown_attempt_indices"] == [7, 8, 9, 10]
    assert paired is not None
    assert paired["target_role"] == "paired_v0"
    assert paired["unknown_attempt_indices"] == [7, 8, 9, 10]


@pytest.mark.parametrize(
    "mutation",
    [
        "too_many_unknown",
        "semantic_insufficient",
        "endpoint",
        "identity",
        "contradiction",
        "not_mature",
    ],
)
def test_trace_unknown_rejects_unreviewed_evidence(mutation: str) -> None:
    attempts = _attempts(observations=6, complete_misses=0, unknowns=4)
    maturity_digest = HASH
    if mutation == "too_many_unknown":
        attempts = _attempts(observations=5, complete_misses=0, unknowns=5)
    elif mutation == "semantic_insufficient":
        attempts[-1]["semantic_evidence_complete"] = False
    elif mutation == "endpoint":
        attempts[-1]["endpoint_complete"] = False
    elif mutation == "identity":
        attempts[-1]["identity_complete"] = False
    elif mutation == "contradiction":
        attempts[-1]["assertions_contradicted"] = True
    else:
        maturity_digest = None

    assert (
        trace_unknown_acceptance(
            target_role="issue",
            validation_mode="model_mediated",
            n=10,
            k=6,
            required_surfaces=["semantic", "trace"],
            attempts=attempts,
            maturity_proof_digest=maturity_digest,
        )
        is None
    )


def test_control_rejects_observation_and_baseline_rejects_failure() -> None:
    controls = _attempts(observations=0, complete_misses=9, unknowns=1)
    controls[0]["observation"] = True
    assert (
        trace_unknown_acceptance(
            target_role="paired_v0",
            validation_mode="model_mediated",
            n=10,
            k=6,
            required_surfaces=["trace"],
            attempts=controls,
            maturity_proof_digest=HASH,
        )
        is None
    )
    baseline = _attempts(observations=6, complete_misses=0, unknowns=4)
    baseline[0]["observation"] = False
    assert (
        trace_unknown_acceptance(
            target_role="baseline",
            validation_mode="baseline",
            n=10,
            k=6,
            required_surfaces=["semantic", "trace"],
            attempts=baseline,
            maturity_proof_digest=HASH,
        )
        is None
    )


def _staging_attempts() -> list[dict]:
    values = []
    for attempt in _attempts(observations=6, complete_misses=0, unknowns=4):
        complete = attempt["complete"]
        step = {
            "endpoint_pass": True,
            "identity_pass": True,
            "semantic_pass": True,
            "trace_pass": complete,
            "semantic_evidence_complete": True,
            "trace_evidence_complete": complete,
        }
        values.append(
            {
                "index": attempt["index"],
                "complete": complete,
                "observation": attempt["observation"],
                "error_code": attempt["error_code"],
                "setup_steps": [deepcopy(step)],
                "probe_steps": [deepcopy(step)],
            }
        )
    return values


def _daily_invocation() -> InvocationEvidence:
    summaries = []
    for attempt in _attempts(observations=6, complete_misses=0, unknowns=4):
        observed = attempt["observation"]
        summaries.append(
            RequestCompletionEvidence(
                request_index=attempt["index"] - 1,
                response_count=1,
                usable_response=True,
                semantic_assertion_count=1,
                semantic_assertions_passed=1,
                assertion_results=(
                    SemanticAssertionEvidence("semantic", True, True),
                ),
                activation_gate=True,
                direct_terminal_response_count=1,
                function_call_count=0,
                trace_assertion_count=1,
                trace_assertions_passed=int(observed),
                trace_assertion_results=(
                    TraceAssertionEvidence(
                        "trace",
                        observed,
                        evidence_sufficient=observed,
                    ),
                ),
                error_code=None if observed else "missing_evidence",
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


def test_staging_and_daily_share_issue_trace_unknown_acceptance() -> None:
    rule = {
        "validation_mode": "model_mediated",
        "n": 10,
        "k": 6,
        "defect_predicate": {
            "kind": "all_observation_steps_pass",
            "required_surfaces": ["semantic", "trace"],
        },
    }
    invocation = _daily_invocation()
    maturity_proof = _daily_maturity_proof()
    maturity_digest = maturity_proof["maturity_proof_digest"]
    staging = _trace_unknown_acceptance(
        authority=type("Authority", (), {"authority_kind": "issue"})(),
        rule=rule,
        target={
            "evidence_snapshot": {
                "mature": True,
                "maturity_proof_digest": maturity_digest,
                "required_trace_hydration": "incomplete",
            }
        },
        attempts=_staging_attempts(),
        target_role="issue",
    )
    daily_complete, daily = _issue_activation_decision(
        {
            "validation_mode": "model_mediated",
            "n": 10,
            "k": 6,
            "required_surfaces": ["semantic", "trace"],
        },
        invocation,
        maturity_proof,
    )

    assert daily_complete is True
    assert staging == daily


def test_daily_baseline_uses_shared_six_plus_four_policy() -> None:
    invocation = _daily_invocation()

    complete, acceptance = _baseline_validation_decision(
        invocation,
        _daily_maturity_proof(),
    )

    assert complete is True
    assert acceptance is not None
    assert acceptance["target_role"] == "baseline"
    assert acceptance["unknown_attempt_indices"] == [7, 8, 9, 10]
