"""Explicit single-target measurements, separate from incremental selection."""

from __future__ import annotations

from datetime import date
import uuid

from .errors import QualityError
from .selection import Selection
from .state import StateError

INTENT = "staging-target-intent"
MODE = "targeted"
REASONS = ("fresh_target",)
_CONTROL_FIELDS = {
    "run_id", "source_revision", "report_date", "full", "completed", "target", "after_run",
}


def argument_target(args, catalog):
    supplied = getattr(args, "target", None)
    after = getattr(args, "after_run", None)
    if supplied is None:
        if after is not None:
            raise QualityError("staging_after_run_requires_target")
        if args.new_run and not args.full:
            raise QualityError("staging_new_run_requires_full_or_target")
        return None
    if args.full or len(supplied) != 1:
        raise QualityError("staging_target_scope_invalid")
    matches = [target for target in catalog.targets if target.key == supplied[0]]
    if len(matches) != 1:
        raise QualityError("staging_target_invalid")
    if after is not None and not args.new_run:
        raise QualityError("staging_after_run_requires_new_run")
    return matches[0]


def selection(target):
    return (Selection(target, "traffic", REASONS),)


def reference(record):
    return None if record is None else {
        "run_id": record["binding_run_id"], "work_key": record["work_key"],
    }


def read_intent(records, *, required=False):
    value = records.read_completed(INTENT, missing_ok=not required)
    if value is None:
        return None
    if set(value) != {
        "run_id", "source_revision", "report_date", "target", "after_run", "work_key", "prior",
    } or value["run_id"] != records.directory.name or any(
        not isinstance(value[field], str) or not value[field]
        for field in ("source_revision", "report_date", "target", "work_key")
    ):
        raise StateError("staging_target_intent_invalid")
    try:
        date.fromisoformat(value["report_date"])
        records._path("progress", value["work_key"])
    except (ValueError, TypeError) as error:
        raise StateError("staging_target_intent_invalid") from error
    if not value["work_key"].startswith(f"targets/{value['target']}/work-") or (
        value["after_run"] is not None and not isinstance(value["after_run"], str)
    ) or value["prior"] is not None and (
        not isinstance(value["prior"], dict) or set(value["prior"]) != {"run_id", "work_key"}
        or not all(isinstance(item, str) and item for item in value["prior"].values())
    ):
        raise StateError("staging_target_intent_invalid")
    return value


def measurement_terminal(store, target, record):
    """A final INCOMPLETE judgment is not an unresolved remote operation.

    Require every planned step to be resolved or provably never submitted.
    Traffic-done alone is insufficient: it can contain unknown invocation outcomes;
    conversely, a definitive rejection can leave safely blocked continuation steps.
    """
    if not record or not record.get("result") or not record.get("assessment") or record.get("reason"):
        return False
    owner = store.run(record["binding_run_id"])
    traffic = store.run(record["traffic_run_id"])
    key = record["work_key"]
    if owner.read_completed("integrity-failure", missing_ok=True):
        return False
    deployment = traffic.read(key + "/deployment", missing_ok=True)
    if not deployment or deployment.get("details", {}).get("provisioning_state") != "active":
        return False
    plan = traffic.read_completed(key + "/plan", missing_ok=True)
    if not plan or [item["index"] for item in plan["attempts"]] != list(range(1, 11)):
        return False
    for attempt in plan["attempts"]:
        base = key + f"/traffic/attempt-{attempt['index']:02d}"
        session = traffic.read(base + "/session", missing_ok=True)
        if (not target.is_prompt or session) and (
            not session or session.get("status") not in {"ready", "rejected"}
            or session.get("status") == "ready" and not session.get("session_id")
        ):
            return False
        blocked = bool(session and session.get("status") == "rejected")
        if not attempt["steps"]:
            return False
        for step in attempt["steps"]:
            receipt = traffic.read(base + "/" + step["step_id"], missing_ok=True)
            if receipt and receipt.get("status") == "blocked":
                if not blocked:
                    return False
                continue
            if blocked:
                return False
            resolved = receipt and (
                receipt.get("status") in {"completed", "failed"}
                or receipt.get("status") == "incomplete"
                and receipt.get("error_code") == "invocation_response_incomplete"
            )
            if not resolved or (
                receipt.get("error_code") == "invocation_response_pending"
            ):
                return False
            blocked = receipt.get("response") is None or target.is_prompt and not receipt.get("response_id")
    insights = key + "/insights"
    if traffic.read(insights + "/start", missing_ok=True) and not (
        traffic.read_completed(insights, missing_ok=True)
        or str((traffic.read(insights + "/poll", missing_ok=True) or {}).get("status")).casefold()
        in {"failed", "canceled", "cancelled"}
    ):
        return False
    assessment = record["assessment"]
    assessed = store.run(assessment["run_id"])
    pending = owner.read(f"targets/{target.key}/assessment", missing_ok=True)
    if pending and (
        pending.get("status") != "applied"
        or pending.get("work_key", key) != key
        or pending.get("artifact") != assessment["artifact"]
    ):
        return False
    result = assessed.read_artifact(assessment["artifact"], missing_ok=True)
    return bool(result and result.get("status") == record["status"] and (
        result["status"] in {"PASS", "FAIL", "INCOMPLETE"}
    ))


def require_safe_prior(catalog, store, target):
    from .runner import _staging_history

    latest = _staging_history(catalog, store, (target,)).get(target.key)
    candidates = [latest] if latest else []
    # A selected-but-unbound target in an interrupted launch must not disappear.
    for mode in ("full", "incremental", MODE):
        active = store.outbox("staging").read(mode, missing_ok=True)
        if not active:
            continue
        records = store.run(active["run_id"])
        saved = records.read_completed("selection")
        if target.key not in [item["key"] for item in saved["targets"]]:
            continue
        bound = records.read(f"targets/{target.key}/source", missing_ok=True)
        if bound is None:
            raise QualityError("staging_target_prior_unfinished")
        candidates.append({**bound, "binding_run_id": active["run_id"]})
    if any(not measurement_terminal(store, target, item) for item in candidates):
        raise QualityError("staging_target_prior_unfinished")
    return latest


def plan(args, catalog, store, revision, today, target):
    control = store.outbox("staging")
    active = control.read(MODE, missing_ok=True)
    if active is not None:
        if set(active) != _CONTROL_FIELDS or active["full"] is not False or (
            type(active["completed"]) is not bool
        ):
            raise StateError("staging_target_resume_invalid")
        records = store.run(active["run_id"])
        intent = read_intent(records, required=True)
        if any(active[key] != intent[key] for key in _CONTROL_FIELDS - {"full", "completed"}):
            raise StateError("staging_target_resume_invalid")
        frozen_selection = {"targets": [{
            "key": intent["target"], "action": "traffic", "reasons": list(REASONS),
        }]}
        if records.read_completed("selection") != frozen_selection:
            raise StateError("staging_target_selection_changed")
        run = records.read_completed("run", missing_ok=True)
        if run is not None and any(run.get(key) != value for key, value in {
            "kind": "staging", "source_revision": intent["source_revision"],
            "report_date": intent["report_date"], "targets": [intent["target"]],
        }.items()):
            raise StateError("staging_target_resume_invalid")
        advancing = args.new_run and args.after_run == active["run_id"]
        if not advancing:
            if args.after_run is not None and args.after_run != intent["after_run"] or (
                args.new_run and args.after_run != intent["after_run"]
            ):
                raise QualityError("staging_target_request_conflict")
            if target.key != intent["target"]:
                raise QualityError("staging_target_resume_target_changed")
            if revision != intent["source_revision"]:
                raise QualityError("staging_target_resume_source_changed")
            from .runner import _staging_history
            latest = _staging_history(catalog, store, (target,)).get(target.key)
            own = records.read(f"targets/{target.key}/source", missing_ok=True)
            expected = {"run_id": active["run_id"], "work_key": intent["work_key"]} if own else intent["prior"]
            if reference(latest) != expected:
                raise StateError("staging_target_superseded")
            return active, date.fromisoformat(intent["report_date"]), selection(target)
        previous_target = catalog.target(intent["target"])
        bound = records.read(f"targets/{previous_target.key}/source", missing_ok=True)
        if not measurement_terminal(store, previous_target, bound):
            raise QualityError("staging_target_prior_unfinished")
    elif not args.new_run or args.after_run is not None:
        raise QualityError("staging_target_new_request_required")
    prior = require_safe_prior(catalog, store, target)
    run_id = f"staging-{today.isoformat()}-{revision[:12]}-target-{uuid.uuid4().hex[:8]}"
    intent = {
        "run_id": run_id, "source_revision": revision, "report_date": today.isoformat(),
        "target": target.key, "after_run": args.after_run,
        "work_key": f"targets/{target.key}/work-{uuid.uuid4().hex}", "prior": reference(prior),
    }
    records = store.run(run_id)
    records.save_completed(INTENT, intent)
    records.save_completed("selection", {"targets": [{
        "key": target.key, "action": "traffic", "reasons": list(REASONS),
    }]})
    active = {key: value for key, value in intent.items() if key not in {"work_key", "prior"}}
    active.update(full=False, completed=False)
    control.save_progress(MODE, active)
    return active, today, selection(target)
