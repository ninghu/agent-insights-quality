from __future__ import annotations

from copy import deepcopy

import pytest

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.models import (
    InvocationEvidence,
    RequestCompletionEvidence,
    SemanticAssertionEvidence,
    TraceAssertionEvidence,
)
from agent_insights_quality.runner import _issue_activation_decision
from agent_insights_quality.validation_trace_gap_policy import (
    DETERMINISTIC_ISSUE_TRACE_GAP_POLICY,
    deterministic_issue_trace_gap_acceptance,
)
from agent_insights_quality.validation_copilot import (
    _issue_trace_gap_acceptance,
)
from agent_insights_quality.validation_manifest import authority_specs


def _attempts(observations: int, unknowns: int) -> list[dict]:
    return [
        {
            "index": index,
            "complete": index <= observations,
            "observation": index <= observations,
            "error_code": (
                None if index <= observations else "missing_evidence"
            ),
            "endpoint_complete": True,
            "identity_complete": True,
            "semantic_evidence_complete": True,
            "trace_evidence_complete": index <= observations,
            "assertions_contradicted": False,
        }
        for index in range(1, observations + unknowns + 1)
    ]


@pytest.mark.parametrize(
    ("observations", "unknowns"),
    [(3, 2), (4, 1)],
)
def test_deterministic_issue_trace_gap_accepts_reviewed_bounds(
    observations: int,
    unknowns: int,
) -> None:
    acceptance = deterministic_issue_trace_gap_acceptance(
        authority_kind="issue",
        validation_mode="deterministic",
        n=5,
        k=5,
        required_surfaces=["semantic", "trace"],
        attempts=_attempts(observations, unknowns),
    )

    assert acceptance == {
        "policy": DETERMINISTIC_ISSUE_TRACE_GAP_POLICY,
        "observation_count": observations,
        "unknown_attempt_indices": list(
            range(observations + 1, 6)
        ),
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "too_many_unknown",
        "semantic_insufficient",
        "endpoint",
        "identity",
        "contradiction",
    ],
)
def test_deterministic_issue_trace_gap_rejects_unreviewed_evidence(
    mutation: str,
) -> None:
    attempts = _attempts(3, 2)
    if mutation == "too_many_unknown":
        attempts = _attempts(2, 3)
    elif mutation == "semantic_insufficient":
        attempts[-1]["semantic_evidence_complete"] = False
    elif mutation == "endpoint":
        attempts[-1]["endpoint_complete"] = False
    elif mutation == "identity":
        attempts[-1]["identity_complete"] = False
    else:
        attempts[-1]["assertions_contradicted"] = True

    assert (
        deterministic_issue_trace_gap_acceptance(
            authority_kind="issue",
            validation_mode="deterministic",
            n=5,
            k=5,
            required_surfaces=["semantic", "trace"],
            attempts=attempts,
        )
        is None
    )


def test_trace_gap_policy_does_not_change_baseline_or_model_mediated() -> None:
    attempts = _attempts(3, 2)
    assert (
        deterministic_issue_trace_gap_acceptance(
            authority_kind="baseline",
            validation_mode="baseline",
            n=5,
            k=5,
            required_surfaces=["trace"],
            attempts=attempts,
        )
        is None
    )
    assert (
        deterministic_issue_trace_gap_acceptance(
            authority_kind="issue",
            validation_mode="model_mediated",
            n=7,
            k=5,
            required_surfaces=["trace"],
            attempts=[*_attempts(5, 2)],
        )
        is None
    )


def test_staging_and_daily_share_deterministic_trace_gap_acceptance() -> None:
    authority = next(
        item
        for item in authority_specs(*load_catalogs())
        if item.authority_id == "issue-016"
    )
    rule = authority.validation_rules["scenarios"][0]
    staging_attempts = []
    daily_summaries = []
    for index in range(1, 6):
        observed = index <= 3
        step = {
            "endpoint_pass": True,
            "identity_pass": True,
            "semantic_pass": True,
            "trace_pass": observed,
            "semantic_evidence_complete": True,
            "trace_evidence_complete": observed,
        }
        staging_attempts.append(
            {
                "index": index,
                "complete": observed,
                "observation": observed,
                "error_code": (
                    None if observed else "missing_evidence"
                ),
                "setup_steps": [deepcopy(step)],
                "probe_steps": [deepcopy(step)],
            }
        )
        daily_summaries.append(
            RequestCompletionEvidence(
                request_index=index - 1,
                response_count=1,
                usable_response=True,
                semantic_assertion_count=1,
                semantic_assertions_passed=1,
                assertion_results=(
                    SemanticAssertionEvidence("semantic", True),
                ),
                activation_gate=True,
                direct_terminal_response_count=0,
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
                error_code=(
                    None if observed else "missing_evidence"
                ),
            )
        )
    staging = _issue_trace_gap_acceptance(
        authority=authority,
        rule=rule,
        issue_attempts=staging_attempts,
    )
    daily_complete, daily = _issue_activation_decision(
        {
            "validation_mode": "deterministic",
            "n": 5,
            "k": 5,
            "required_surfaces": ["semantic", "trace"],
        },
        InvocationEvidence(
            operation_ids=tuple(f"{index:032x}" for index in range(1, 6)),
            response_references=tuple(
                f"response-{index}" for index in range(1, 6)
            ),
            started_at="2026-09-03T00:00:00+00:00",
            completed_at="2026-09-03T00:01:00+00:00",
            request_count=5,
            allow_window_correlation=False,
            response_count=5,
            usable_response_count=5,
            request_summaries=tuple(daily_summaries),
        ),
    )

    assert daily_complete is True
    assert staging == daily
