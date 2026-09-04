from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

DETERMINISTIC_ISSUE_TRACE_GAP_POLICY = (
    "deterministic_issue_trace_gap_first_mature_batch_v1"
)


def deterministic_issue_trace_gap_acceptance(
    *,
    authority_kind: str,
    validation_mode: str,
    n: int,
    k: int,
    required_surfaces: Sequence[str],
    attempts: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    surfaces = set(required_surfaces)
    if (
        authority_kind != "issue"
        or validation_mode != "deterministic"
        or (n, k) != (5, 5)
        or "trace" not in surfaces
        or len(attempts) != n
        or [attempt.get("index") for attempt in attempts]
        != list(range(1, n + 1))
    ):
        return None
    observations = [
        attempt
        for attempt in attempts
        if attempt.get("complete") is True
        and attempt.get("observation") is True
    ]
    unknown = [
        attempt for attempt in attempts if attempt.get("complete") is not True
    ]
    if (
        len(observations) < 3
        or not unknown
        or len(unknown) > 2
        or len(observations) + len(unknown) != n
        or any(
            attempt.get("endpoint_complete") is not True
            or attempt.get("identity_complete") is not True
            or attempt.get("assertions_contradicted") is True
            for attempt in observations
        )
    ):
        return None
    for attempt in unknown:
        if (
            attempt.get("observation") is True
            or attempt.get("error_code") != "missing_evidence"
            or attempt.get("endpoint_complete") is not True
            or attempt.get("identity_complete") is not True
            or (
                "semantic" in surfaces
                and attempt.get("semantic_evidence_complete") is not True
            )
            or attempt.get("trace_evidence_complete") is not False
            or attempt.get("assertions_contradicted") is True
        ):
            return None
    return {
        "policy": DETERMINISTIC_ISSUE_TRACE_GAP_POLICY,
        "observation_count": len(observations),
        "unknown_attempt_indices": [
            int(attempt["index"]) for attempt in unknown
        ],
    }


def issue_side_decided(
    *,
    observation_count: int,
    k: int,
    trace_gap_acceptance: Mapping[str, Any] | None,
) -> bool:
    return observation_count >= k or trace_gap_acceptance is not None


def daily_issue_side_decision(
    *,
    validation_mode: str,
    n: int,
    k: int,
    required_surfaces: Sequence[str],
    summaries: Sequence[Mapping[str, Any]],
    identity_verified: bool,
) -> tuple[bool, dict[str, Any] | None]:
    surfaces = set(required_surfaces)
    if len(summaries) != n:
        return False, None
    attempts = []
    observation_count = 0
    for index, summary in enumerate(summaries, start=1):
        semantic_complete = (
            "semantic" not in surfaces
            or (
                int(summary.get("semantic_assertion_count") or 0) > 0
                and summary.get("semantic_assertions_passed")
                == summary.get("semantic_assertion_count")
            )
        )
        trace_results = summary.get("trace_assertion_results")
        if not isinstance(trace_results, Sequence):
            return False, None
        trace_evidence_complete = all(
            isinstance(result, Mapping)
            and result.get("evidence_sufficient") is True
            for result in trace_results
        )
        trace_complete = (
            "trace" not in surfaces
            or (
                int(summary.get("trace_assertion_count") or 0) > 0
                and trace_evidence_complete
                and summary.get("trace_assertions_passed")
                == summary.get("trace_assertion_count")
            )
        )
        observation = semantic_complete and trace_complete
        observation_count += int(observation)
        attempts.append(
            {
                "index": index,
                "complete": observation,
                "observation": observation,
                "error_code": summary.get("error_code"),
                "endpoint_complete": (
                    summary.get("response_count") == 1
                    and summary.get("usable_response") is True
                ),
                "identity_complete": identity_verified,
                "semantic_evidence_complete": semantic_complete,
                "trace_evidence_complete": trace_evidence_complete,
                "assertions_contradicted": (
                    summary.get("semantic_assertions_passed")
                    != summary.get("semantic_assertion_count")
                    or any(
                        isinstance(result, Mapping)
                        and result.get("evidence_sufficient") is True
                        and result.get("passed") is not True
                        for result in trace_results
                    )
                ),
            }
        )
    acceptance = deterministic_issue_trace_gap_acceptance(
        authority_kind="issue",
        validation_mode=validation_mode,
        n=n,
        k=k,
        required_surfaces=required_surfaces,
        attempts=attempts,
    )
    return issue_side_decided(
        observation_count=observation_count,
        k=k,
        trace_gap_acceptance=acceptance,
    ), acceptance
