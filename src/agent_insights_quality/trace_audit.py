"""Read-only, alias-only comparison of planned caller context and retained roots."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import re

from .errors import QualityError
from .invocation_context import POLICY_KEY, enabled_policy, planned_context
from .state import RuntimeStore, StateError
from .telemetry import Snapshot


def audit_run(runtime: RuntimeStore, run_id: str) -> dict:
    run = runtime.run(run_id)
    metadata = run.read_completed("run")
    aliases, requests, expected_ids, observed_ids = {}, {}, [], []
    rows, unavailable_units = [], []
    observed_owners = defaultdict(list)
    def alias(value: str) -> str:
        value = value.lower() if re.fullmatch(r"[0-9a-fA-F]{32}", value) else value
        return aliases.setdefault(value, f"trace-{len(aliases) + 1:04d}")
    for unit in metadata["targets"]:
        source = run.read("targets/" + unit + "/source", missing_ok=True)
        if source is None:
            unavailable_units.append(unit)
            continue
        key = source["work_key"]
        if not key.startswith("targets/" + unit + "/work-"):
            raise StateError("trace_audit_binding_invalid")
        records = runtime.run(source["traffic_run_id"])
        policy = enabled_policy(records.read_completed(POLICY_KEY, missing_ok=True))
        plan = records.read_completed(key + "/plan", missing_ok=True)
        if plan is None:
            unavailable_units.append(unit)
            continue
        insights = records.read_completed(key + "/insights", missing_ok=True)
        evidence = records.read(key + "/evidence", missing_ok=True)
        artifact = insights["visible_snapshot"] if insights else (
            source.get("evidence_key") or (evidence["artifact"] if evidence else None)
        )
        snapshot = Snapshot.from_private_dict(records.read_artifact(artifact)) if artifact else None
        scopes = {scope.response_id: scope for scope in snapshot.scopes} if snapshot else {}
        for attempt in plan["attempts"]:
            for step in attempt["steps"]:
                turn = key + f"/traffic/attempt-{attempt['index']:02d}/" + step["step_id"]
                receipt = records.read(turn, missing_ok=True)
                context = records.read_completed(turn + "/outbound-context", missing_ok=True)
                expected = None
                if context is not None:
                    try:
                        valid = planned_context(context["request_id"], source["traffic_source_revision"])
                    except (KeyError, QualityError) as error:
                        raise StateError("trace_context_checkpoint_invalid") from error
                    if not policy or context != valid or receipt and receipt["request_id"] != context["request_id"]:
                        raise StateError("trace_context_checkpoint_invalid")
                    expected = alias(context["traceparent"][3:35])
                    expected_ids.append(expected)
                request = None
                if receipt:
                    request = requests.setdefault(receipt["request_id"], f"request-{len(requests) + 1:04d}")
                scope = scopes.get(receipt.get("response_id")) if receipt else None
                actual = sorted({alias(value) for value in scope.operation_ids}) if scope and scope.attributable else []
                observed_ids.extend(actual)
                row = {
                    "unit": unit, "attempt": attempt["index"], "turn": step["step_id"],
                    "request_alias": request, "expected_trace_alias": expected,
                    "observed_trace_aliases": actual,
                    "receipt_status": receipt["status"] if receipt else "absent",
                    "snapshot_basis": "pre_insights" if insights else "retained_collection",
                    "snapshot_query_complete": snapshot.query_complete if snapshot else None,
                    "scope_ambiguous": bool(scope and scope.reasons),
                    "comparison": (
                        "expected_unavailable" if expected is None else
                        "not_dispatched_or_unknown" if not receipt or receipt["status"] == "blocked" else
                        "observed_unavailable" if not actual else
                        "ambiguous" if len(actual) != 1 else
                        "matches" if actual == [expected] else "different_context"
                    ),
                }
                rows.append(row)
                for value in actual:
                    observed_owners[value].append({
                        "unit": unit, "attempt": attempt["index"], "turn": step["step_id"],
                        "request_alias": request,
                    })
    counts = Counter(row["comparison"] for row in rows)
    observation = (
        "not_configured_for_retained_traffic" if not expected_ids else
        "different_context_or_unsupported" if counts["different_context"] and not counts["matches"] else
        "mixed_contexts_requires_review" if counts["different_context"] else
        "matching_roots_observed" if counts["matches"] == len(expected_ids) else
        "matching_roots_with_gaps" if counts["matches"] else "unknown"
    )
    return {
        "schema_version": "1.0", "run_id": run_id, "profile": runtime.environment,
        "policy": "comparison_only_no_readiness_or_score_changes",
        "propagation_observation": observation,
        "acceptance": "not_determined_by_this_comparison",
        "limits": [
            "Expected context is a durable pre-invoke plan, not proof of socket delivery.",
            "Different observed context can be a legitimate platform restart; it is not an automatic exclusion.",
            "Matching roots are observed propagation evidence, not a guarantee for absent roots or future traffic.",
        ],
        "counts": dict(counts),
        "planned_contexts": len(expected_ids), "distinct_expected_contexts": len(set(expected_ids)),
        "distinct_request_ids": len(requests), "distinct_observed_contexts": len(set(observed_ids)),
        "planned_contexts_distinct": len(expected_ids) == len(set(expected_ids)) if expected_ids else None,
        "unavailable_units": unavailable_units,
        "shared_observed_contexts": [
            {"trace_alias": value, "turns": owners}
            for value, owners in observed_owners.items()
            if len({row["request_alias"] for row in owners}) > 1
        ],
        "turns": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("staging", "daily"), required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        result = audit_run(RuntimeStore(args.profile), args.run_id)
    except QualityError as error:
        print(json.dumps({"status": "unavailable", "code": error.code}))
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
