from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_insights_quality.util import ContractError, content_hash


ROLE_PASS_POLICY = "six_complete_role_passes_v1"
TRACE_MATURITY_PROOF_KIND = "daily-trace-maturity-proof"
_MISS_CATEGORIES = (
    "complete_non_pass",
    "endpoint_incomplete",
    "identity_incomplete",
    "semantic_incomplete",
    "trace_incomplete",
    "other_incomplete",
)


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


def role_pass_summary(
    *,
    target_role: str,
    n: int,
    k: int,
    attempts: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    if (
        target_role not in {"baseline", "issue", "paired_v0"}
        or n != 10
        or k != 6
        or len(attempts) != n
        or [attempt.get("index") for attempt in attempts]
        != list(range(1, n + 1))
    ):
        return None

    pass_attempt_indices: list[int] = []
    miss_attempt_indices: list[int] = []
    miss_counts = dict.fromkeys(_MISS_CATEGORIES, 0)
    for attempt in attempts:
        if (
            not isinstance(attempt.get("complete"), bool)
            or not isinstance(attempt.get("observation"), bool)
        ):
            return None
        complete = attempt["complete"] is True
        observation = attempt["observation"] is True
        role_pass = complete and (
            not observation if target_role == "paired_v0" else observation
        )
        index = int(attempt["index"])
        if role_pass:
            pass_attempt_indices.append(index)
            continue
        miss_attempt_indices.append(index)
        if complete:
            category = "complete_non_pass"
        elif attempt.get("endpoint_complete") is not True:
            category = "endpoint_incomplete"
        elif attempt.get("identity_complete") is not True:
            category = "identity_incomplete"
        elif attempt.get("semantic_evidence_complete") is not True:
            category = "semantic_incomplete"
        elif attempt.get("trace_evidence_complete") is not True:
            category = "trace_incomplete"
        else:
            category = "other_incomplete"
        miss_counts[category] += 1

    return {
        "policy": ROLE_PASS_POLICY,
        "target_role": target_role,
        "required_pass_count": k,
        "pass_count": len(pass_attempt_indices),
        "pass_attempt_indices": pass_attempt_indices,
        "miss_count": len(miss_attempt_indices),
        "miss_attempt_indices": miss_attempt_indices,
        "miss_counts": miss_counts,
    }


def target_evidence_decided(
    *,
    n: int,
    k: int,
    role_pass_count: int,
) -> bool:
    return (
        n == 10
        and k == 6
        and isinstance(role_pass_count, int)
        and not isinstance(role_pass_count, bool)
        and k <= role_pass_count <= n
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
    direct_prompt_contract: bool = False,
) -> tuple[bool, dict[str, Any] | None]:
    surfaces = set(required_surfaces)
    if (
        target_role not in {"baseline", "issue", "paired_v0"}
        or validation_mode
        not in {"baseline", "deterministic", "model_mediated"}
        or (
            target_role == "baseline"
            and validation_mode != "baseline"
        )
        or (
            target_role != "baseline"
            and validation_mode == "baseline"
        )
        or (not surfaces and target_role != "issue")
        or not surfaces <= {"semantic", "trace"}
        or not isinstance(identity_verified, bool)
        or len(summaries) != n
    ):
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
            or (
                target_role == "baseline"
                and summary.get("error_code") != "missing_evidence"
            )
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
            and (
                not direct_prompt_contract
                or (
                    summary.get("direct_terminal_response_count") == 1
                    and summary.get("function_call_count") == 0
                )
            )
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
    summary = role_pass_summary(
        target_role=target_role,
        n=n,
        k=k,
        attempts=attempts,
    )
    if summary is None:
        return False, None
    return target_evidence_decided(
        n=n,
        k=k,
        role_pass_count=int(summary["pass_count"]),
    ), summary
