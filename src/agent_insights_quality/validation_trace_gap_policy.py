from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_insights_quality.util import ContractError, content_hash


TRACE_UNKNOWN_ACCEPTANCE_POLICY = "mature_trace_unknown_v1"
TRACE_MATURITY_PROOF_KIND = "daily-trace-maturity-proof"


def build_trace_maturity_proof(
    *,
    evidence_window_start: str,
    evidence_window_end: str,
    snapshot_observed_at: datetime,
    maximum_hydration_seconds: int,
    stabilization_seconds: int,
) -> dict[str, Any] | None:
    try:
        start = datetime.fromisoformat(
            evidence_window_start.replace("Z", "+00:00")
        ).astimezone(UTC)
        end = datetime.fromisoformat(
            evidence_window_end.replace("Z", "+00:00")
        ).astimezone(UTC)
    except ValueError as error:
        raise ContractError("Daily trace maturity window is invalid") from error
    observed = snapshot_observed_at.astimezone(UTC)
    if (
        snapshot_observed_at.tzinfo is None
        or end < start
        or maximum_hydration_seconds < 1
        or stabilization_seconds < 1
    ):
        raise ContractError("Daily trace maturity inputs are invalid")
    boundary = end + timedelta(
        seconds=maximum_hydration_seconds + stabilization_seconds
    )
    if observed < boundary:
        return None
    value = {
        "schema_version": "1.0.0",
        "kind": TRACE_MATURITY_PROOF_KIND,
        "evidence_window_start": start.isoformat(),
        "evidence_window_end": end.isoformat(),
        "maturity_boundary": boundary.isoformat(),
        "snapshot_observed_at": observed.isoformat(),
        "maximum_hydration_seconds": maximum_hydration_seconds,
        "stabilization_seconds": stabilization_seconds,
        "maturity_proof_digest": "",
    }
    value["maturity_proof_digest"] = content_hash(
        {
            key: item
            for key, item in value.items()
            if key != "maturity_proof_digest"
        }
    )
    return value


def validate_trace_maturity_proof(
    value: Mapping[str, Any] | None,
) -> str | None:
    if value is None:
        return None
    required = {
        "schema_version",
        "kind",
        "evidence_window_start",
        "evidence_window_end",
        "maturity_boundary",
        "snapshot_observed_at",
        "maximum_hydration_seconds",
        "stabilization_seconds",
        "maturity_proof_digest",
    }
    if (
        set(value) != required
        or value.get("schema_version") != "1.0.0"
        or value.get("kind") != TRACE_MATURITY_PROOF_KIND
        or value.get("maturity_proof_digest")
        != content_hash(
            {
                key: item
                for key, item in value.items()
                if key != "maturity_proof_digest"
            }
        )
    ):
        raise ContractError("Daily trace maturity proof integrity is invalid")
    rebuilt = build_trace_maturity_proof(
        evidence_window_start=str(value["evidence_window_start"]),
        evidence_window_end=str(value["evidence_window_end"]),
        snapshot_observed_at=datetime.fromisoformat(
            str(value["snapshot_observed_at"]).replace("Z", "+00:00")
        ),
        maximum_hydration_seconds=int(value["maximum_hydration_seconds"]),
        stabilization_seconds=int(value["stabilization_seconds"]),
    )
    if rebuilt != dict(value):
        raise ContractError("Daily trace maturity proof binding is invalid")
    return str(value["maturity_proof_digest"])


def trace_unknown_acceptance(
    *,
    target_role: str,
    validation_mode: str,
    n: int,
    k: int,
    required_surfaces: Sequence[str],
    attempts: Sequence[Mapping[str, Any]],
    maturity_proof_digest: str | None,
) -> dict[str, Any] | None:
    if (
        target_role not in {"baseline", "issue", "paired_v0"}
        or validation_mode
        not in {"baseline", "deterministic", "model_mediated"}
        or n != 10
        or (
            target_role == "baseline"
            and (validation_mode != "baseline" or k != 6)
        )
        or (
            target_role != "baseline"
            and (validation_mode == "baseline" or k != 6)
        )
        or "trace" not in set(required_surfaces)
        or len(attempts) != n
        or [attempt.get("index") for attempt in attempts]
        != list(range(1, n + 1))
        or not _valid_digest(maturity_proof_digest)
    ):
        return None
    complete = [
        attempt for attempt in attempts if attempt.get("complete") is True
    ]
    unknown = [
        attempt for attempt in attempts if attempt.get("complete") is not True
    ]
    if not unknown or len(unknown) > 4:
        return None
    if target_role == "baseline":
        if (
            len(complete) < k
            or any(attempt.get("observation") is not True for attempt in complete)
        ):
            return None
    elif target_role == "issue":
        if sum(attempt.get("observation") is True for attempt in complete) < k:
            return None
    elif (
        len(complete) < n - 4
        or any(attempt.get("observation") is True for attempt in complete)
    ):
        return None
    for attempt in unknown:
        if (
            attempt.get("observation") is True
            or attempt.get("error_code") != "missing_evidence"
            or attempt.get("endpoint_complete") is not True
            or attempt.get("identity_complete") is not True
            or attempt.get("semantic_evidence_complete") is not True
            or attempt.get("trace_evidence_complete") is not False
            or attempt.get("assertions_contradicted") is True
        ):
            return None
    return {
        "policy": TRACE_UNKNOWN_ACCEPTANCE_POLICY,
        "target_role": target_role,
        "observation_count": sum(
            attempt.get("observation") is True for attempt in complete
        ),
        "unknown_attempt_indices": [
            int(attempt["index"]) for attempt in unknown
        ],
        "maturity_proof_digest": maturity_proof_digest,
    }


def target_evidence_decided(
    *,
    target_role: str,
    n: int,
    k: int,
    complete_count: int,
    observation_count: int,
    trace_unknown_acceptance: Mapping[str, Any] | None,
) -> bool:
    if target_role == "issue":
        return observation_count >= k and (
            complete_count == n or trace_unknown_acceptance is not None
        )
    if target_role == "baseline":
        return observation_count == complete_count and observation_count >= k and (
            complete_count == n or trace_unknown_acceptance is not None
        )
    return (
        observation_count == 0
        and (
            complete_count == n
            or trace_unknown_acceptance is not None
        )
    )


def daily_target_decision(
    *,
    target_role: str,
    validation_mode: str,
    n: int,
    k: int,
    required_surfaces: Sequence[str],
    summaries: Sequence[Mapping[str, Any]],
    identity_verified: bool,
    maturity_proof_digest: str | None,
) -> tuple[bool, dict[str, Any] | None]:
    surfaces = set(required_surfaces)
    if len(summaries) != n:
        return False, None
    attempts = []
    for index, summary in enumerate(summaries, start=1):
        semantic_count = int(summary.get("semantic_assertion_count") or 0)
        semantic_passed = int(summary.get("semantic_assertions_passed") or 0)
        semantic_results = summary.get("assertion_results")
        if not isinstance(semantic_results, Sequence):
            return False, None
        semantic_evidence_complete = (
            len(semantic_results) == semantic_count
            and ("semantic" not in surfaces or semantic_count > 0)
            and all(
                isinstance(result, Mapping)
                and result.get("evidence_sufficient") is True
                for result in semantic_results
            )
        )
        semantic_pass = semantic_passed == semantic_count
        trace_results = summary.get("trace_assertion_results")
        if not isinstance(trace_results, Sequence):
            return False, None
        trace_evidence_complete = all(
            isinstance(result, Mapping)
            and result.get("evidence_sufficient") is True
            for result in trace_results
        ) and (
            bool(trace_results)
            or target_role != "baseline"
            or summary.get("error_code") != "missing_evidence"
        )
        trace_pass = (
            "trace" not in surfaces
            or (
                (
                    int(summary.get("trace_assertion_count") or 0) > 0
                    and summary.get("trace_assertions_passed")
                    == summary.get("trace_assertion_count")
                )
                or (
                    target_role == "baseline"
                    and not trace_results
                    and summary.get("error_code") != "missing_evidence"
                )
            )
        )
        endpoint_complete = (
            summary.get("response_count") == 1
            and summary.get("usable_response") is True
        )
        complete = (
            endpoint_complete
            and identity_verified
            and semantic_evidence_complete
            and ("trace" not in surfaces or trace_evidence_complete)
        )
        observation = complete and semantic_pass and trace_pass
        attempts.append(
            {
                "index": index,
                "complete": complete,
                "observation": observation,
                "error_code": summary.get("error_code"),
                "endpoint_complete": endpoint_complete,
                "identity_complete": identity_verified,
                "semantic_evidence_complete": semantic_evidence_complete,
                "trace_evidence_complete": trace_evidence_complete,
                "assertions_contradicted": (
                    (
                        semantic_evidence_complete
                        and "semantic" in surfaces
                        and not semantic_pass
                    )
                    or (
                        trace_evidence_complete
                        and "trace" in surfaces
                        and not trace_pass
                    )
                ),
            }
        )
    acceptance = trace_unknown_acceptance(
        target_role=target_role,
        validation_mode=validation_mode,
        n=n,
        k=k,
        required_surfaces=required_surfaces,
        attempts=attempts,
        maturity_proof_digest=maturity_proof_digest,
    )
    complete_count = sum(attempt["complete"] is True for attempt in attempts)
    observation_count = sum(
        attempt["observation"] is True for attempt in attempts
    )
    if any(attempt["assertions_contradicted"] is True for attempt in attempts):
        return False, None
    return target_evidence_decided(
        target_role=target_role,
        n=n,
        k=k,
        complete_count=complete_count,
        observation_count=observation_count,
        trace_unknown_acceptance=acceptance,
    ), acceptance


def _valid_digest(value: str | None) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    suffix = value.removeprefix("sha256:")
    return len(suffix) == 64 and all(
        character in "0123456789abcdef" for character in suffix
    )
