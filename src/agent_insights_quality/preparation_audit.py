"""Read-only staging preparation observations, not native-contract acceptance."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json

from .errors import QualityError
from .state import RuntimeStore, StateError
from .telemetry import Snapshot


def _policy(records) -> int:
    value = records.read_completed("staging-session-lookahead", missing_ok=True)
    if value is None:
        return 0
    if set(value) != {"travel_sessions_ahead"} or (
        type(value["travel_sessions_ahead"]) is not int or value["travel_sessions_ahead"] not in (0, 1)
    ):
        raise StateError("session_lookahead_policy_invalid")
    return value["travel_sessions_ahead"]


def _segments(records) -> list[dict]:
    roots = {path.name for collection in ("artifacts", "progress")
             for path in (records.directory / collection / "performance").glob("segment-*") if path.is_dir()}
    result = []
    for segment in sorted(roots):
        key = "performance/" + segment
        final = records.read_artifact(key + "/performance", missing_ok=True)
        summary = final or records.read(key, missing_ok=True) or {}
        observations = {}
        batches = (records.directory / "artifacts" / key / "batches").glob("batch-*.json")
        for batch in sorted(batches):
            value = records.read_artifact(key + "/batches/" + batch.stem)
            for row in value["observations"]:
                observations[row["sequence"]] = row
        for row in summary.get("observations", ()):
            if row["sequence"] in observations and observations[row["sequence"]] != row:
                raise StateError("preparation_metrics_conflict")
            observations[row["sequence"]] = row
        result.append({
            "observations": list(observations.values()),
            "complete": bool(final) and not summary.get("dropped_records") and not summary.get("warnings"),
            "configuration": summary.get("configuration", {}),
            "global_attempt_peak": summary.get("peak_active", {}).get("execution:staging_attempt"),
        })
    return result


def _intervals(rows, kind, name):
    return [row for row in rows if row["kind"] == kind and row["name"] == name
            and row.get("elapsed_seconds") is not None
            and "start_offset_seconds" in row and "end_offset_seconds" in row]


def _overlap(left, right) -> float:
    return max(0, min(left["end_offset_seconds"], right["end_offset_seconds"])
               - max(left["start_offset_seconds"], right["start_offset_seconds"]))


def _peak(rows) -> int:
    points = [(row["start_offset_seconds"], 1) for row in rows
              if row["end_offset_seconds"] > row["start_offset_seconds"]]
    points += [(row["end_offset_seconds"], -1) for row in rows
               if row["end_offset_seconds"] > row["start_offset_seconds"]]
    active = peak = 0
    for _, delta in sorted(points):
        active += delta
        peak = max(peak, active)
    return peak


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def audit_run(runtime: RuntimeStore, run_id: str) -> dict:
    if runtime.environment != "staging":
        raise QualityError("preparation_audit_requires_staging")
    run = runtime.run(run_id)
    metadata = run.read_completed("run")
    if metadata["kind"] != "staging":
        raise StateError("preparation_audit_binding_invalid")
    policy = _policy(run)
    segments, rows, native_sessions = _segments(run), [], []
    versions = {}
    for unit in metadata["targets"]:
        if not unit.startswith("travel-agent/"):
            continue
        source = run.read("targets/" + unit + "/source", missing_ok=True)
        if source is None:
            rows.append({"unit": unit, "status": "binding_unavailable"})
            continue
        key = source["work_key"]
        if not key.startswith("targets/" + unit + "/work-"):
            raise StateError("preparation_audit_binding_invalid")
        records = runtime.run(source["traffic_run_id"])
        plan = records.read_completed(key + "/plan", missing_ok=True)
        deployment = records.read(key + "/deployment", missing_ok=True)
        if plan is None or deployment is None:
            rows.append({"unit": unit, "status": "plan_or_deployment_unavailable"})
            continue
        artifact = source.get("evidence_key")
        snapshot = Snapshot.from_private_dict(records.read_artifact(artifact)) if artifact else None
        attributable = snapshot.attributable_responses if snapshot else set()
        owned = source["traffic_run_id"] == run_id
        eligible = bool(owned and policy and _policy(records) and deployment["agent_type"] == "hosted_code")
        expected_affinity = {field: deployment[field] for field in (
            "target_key", "agent_name", "provider_version",
        )}
        expected_affinity["deployment_source_revision"] = deployment["source_revision"]
        sessions, receipts, attempts, affinities, roots = [], [], [], 0, 0
        for attempt in plan["attempts"]:
            base = key + f"/traffic/attempt-{attempt['index']:02d}"
            session = records.read(base + "/session", missing_ok=True)
            affinity = records.read_completed(base + "/session-affinity", missing_ok=True)
            session_id = session.get("session_id") if session and session["status"] == "ready" else None
            if session_id:
                sessions.append(session_id)
                native_sessions.append(session_id)
            affinities += affinity == expected_affinity and deployment["target_key"] == unit
            turns = [records.read(base + "/" + step["step_id"], missing_ok=True) for step in attempt["steps"]]
            bound = bool(session_id) and all(r and r["session_id"] == session_id for r in turns)
            attempts.append(bound)
            receipts.extend(turns)
            roots += any(step["phase"] == "probe" and receipt
                         and receipt.get("response_id") in attributable
                         for step, receipt in zip(attempt["steps"], turns, strict=True))
        completed = [r for r in receipts if r and r["status"] == "completed"]
        windows = [(_timestamp(r.get("started_at")), _timestamp(r.get("completed_at"))) for r in completed]
        ordered = len(completed) == len(receipts) and all(
            start is not None and end is not None and start <= end for start, end in windows
        )
        if ordered:
            ordered = all(left[1] <= right[0] for left, right in zip(windows, windows[1:]))
        counts, overlap, business_peak, owned_peak = Counter(), 0.0, 0, 0
        observed_turns = set()
        for segment in segments if owned else ():
            observations = [r for r in segment["observations"] if r.get("unit") == unit]
            prep = _intervals(observations, "port_call", "create_session")
            business = _intervals(observations, "port_call", "invoke")
            observed_turns.update((b.get("attempt"), b.get("turn")) for b in business)
            counts.update(create_session_calls=len(prep), invocation_calls=len(business))
            pairs = [(p, b) for p in prep for b in business
                     if p.get("attempt") == b.get("attempt", -2) + 1 and _overlap(p, b) > 0]
            counts["next_session_overlap_pairs"] += len(pairs)
            overlap += sum(_overlap(p, b) for p, b in pairs)
            business_peak = max(business_peak, _peak(business))
            owned_peak = max(owned_peak, _peak(_intervals(observations, "execution", "staging_attempt")))
        version = (deployment["agent_name"], deployment["provider_version"])
        version_alias = versions.setdefault(version, f"version-{len(versions) + 1:03d}")
        rows.append({
            "unit": unit, "status": "observed", "traffic_owned_by_this_run": owned,
            "trial_policy_applies": eligible, "provider_version_alias": version_alias,
            "planned_attempts": len(plan["attempts"]), "planned_turns": len(receipts),
            "completed_turns": len(completed), "ready_sessions": len(sessions),
            "sessions_distinct_per_attempt": len(sessions) == len(set(sessions)) == len(attempts),
            "turns_bound_to_attempt_session": all(attempts),
            "matching_version_affinities": affinities,
            "ordered_completed_receipts": ordered,
            "attributable_probe_attempts": roots if snapshot else None,
            "snapshot_query_complete": snapshot.query_complete if snapshot else None,
            "qualification_status": source.get("status"),
            "qualification_minimum_required": source.get("minimum_required"),
            "measured_calls": dict(counts),
            "observed_preparation_invoke_overlap_seconds": overlap if owned and segments else None,
            "observed_business_peak": business_peak if owned and segments else None,
            "observed_whole_attempt_peak": owned_peak if owned and segments else None,
            "planned_turns_with_call_observation": len(observed_turns & {
                (attempt["index"], step["step_id"])
                for attempt in plan["attempts"] for step in attempt["steps"]
            }),
        })
    return {
        "schema_version": "1.0", "run_id": run_id, "profile": "staging",
        "frozen_sessions_ahead": policy, "units": rows,
        "current_run_trial_units": sum(bool(row.get("trial_policy_applies")) for row in rows),
        "ready_sessions_globally_distinct": len(native_sessions) == len(set(native_sessions)) if native_sessions else None,
        "performance_segments": len(segments),
        "performance_segments_complete": bool(segments) and all(s["complete"] for s in segments),
        "global_attempt_peaks": [s["global_attempt_peak"] for s in segments],
        "recorded_attempt_budgets": [s["configuration"].get("daily_attempt_budget") for s in segments],
        "acceptance": "requires_qualification_and_native_continuation_review",
        "limits": [
            "Preparation can provision a sandbox and consume quota; it is not an Agent business request.",
            "Overlap is observed adapter wall time, not guaranteed savings or a sum-of-stages speedup.",
            "Missing, truncated or unfinished measurements cannot prove zero overlap or complete execution.",
            "Review retained raw qualification/continuation evidence for interference; source is not serving-binary proof.",
            "This read-only comparison does not enable Daily, alter qualification, or create an approval record.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        value = audit_run(RuntimeStore("staging"), args.run_id)
    except QualityError as error:
        print(json.dumps({"status": "unavailable", "code": error.code}))
        return 2
    print(json.dumps(value, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
