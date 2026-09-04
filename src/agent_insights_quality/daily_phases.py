from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from agent_insights_quality.automation_policy import AutomationPolicy, load_automation_policy
from agent_insights_quality.catalogs import agent_model_contract, catalog_hashes, load_catalogs
from agent_insights_quality.daily_lifecycle import (
    AGENT_ORDER,
    DailyLifecycle,
    DailyLock,
    DailyRecord,
    artifact_reference,
    daily_runtime_root,
)
from agent_insights_quality.live import LiveRuntime, TelemetryOnlyRuntime
from agent_insights_quality.models import (
    AgentResult,
    InsightEvidence,
    InsightRunCheckpoint,
    InvocationEvidence,
    RequestCompletionEvidence,
    SemanticAssertionEvidence,
    TraceAssertionEvidence,
    VersionResult,
    linked_operations_match_scope,
)
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.registry import load_registry, version_entry
from agent_insights_quality.runner import (
    _baseline_validation_decision,
    _issue_activation_decision,
    _minimum_passing_trace_observations,
    _required_trace_operations,
    _validate_baseline_trace_evidence,
    _with_trace_assertions,
)
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    atomic_json,
    content_hash,
    immutable_json,
    read_json,
    runtime_root,
)
from agent_insights_quality.validation_copilot import copilot_claimant_reference
from agent_insights_quality.validation_local import current_clean_commit
from agent_insights_quality.validation_rules import (
    daily_issue_side_requests,
    issue_observation_context,
)

_TRAFFIC_SCHEMA = ROOT / "schemas" / "daily-traffic-receipt.schema.json"
_VERIFICATION_SCHEMA = ROOT / "schemas" / "daily-verification-result.schema.json"
_INSIGHT_SCHEMA = ROOT / "schemas" / "daily-insight-receipt.schema.json"
_PHASE_SCHEMA = ROOT / "schemas" / "daily-phase-manifest.schema.json"
_VERIFICATION_LEASE = timedelta(hours=2)


@dataclass(frozen=True)
class DailyTarget:
    agent_name: str
    logical_version: str
    foundry_version: str
    content_digest: str
    agent_type: str
    traffic_path: Path
    expected: dict[str, Any] | None


def run_daily_traffic_agent(
    agent_name: str,
    *,
    base: Path | None = None,
    profile_factory: Callable[[str], RuntimeProfile] | None = None,
    runtime_factory: Callable[[RuntimeProfile], Any] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    if agent_name not in AGENT_ORDER:
        raise ContractError("Daily traffic Agent is unknown")
    private_root = (base or runtime_root()).resolve()
    active = _open_phase(private_root, {"PREPARED", "TRAFFIC"})
    lock = _coordinator_lock(private_root)
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=private_root)
        current = lifecycle.read_active()
        if current.value["state"] == "PREPARED":
            current = lifecycle.transition(current, next_state="TRAFFIC")
        elif current.value["state"] != "TRAFFIC":
            raise ContractError("Daily traffic phase is not open")
        active = current
    lane_lock = DailyLock(_run_root(active, private_root) / "traffic" / agent_name / "lane.lock")
    with lane_lock:
        capacity = _acquire_capacity(
            active,
            private_root,
            phase="traffic",
            maximum=active.value["bindings"]["policy"]["max_parallel_agents"],
        )
        try:
            profile, runtime, targets = _phase_context(
                active,
                private_root,
                agent_name=agent_name,
                profile_factory=profile_factory,
                runtime_factory=runtime_factory,
                allowed_states={"TRAFFIC"},
            )
            del profile
            completed = 0
            for index, target in enumerate(targets):
                path = _traffic_receipt_path(active, private_root, target)
                if path.is_file():
                    _load_traffic_receipt(path, active, target)
                    completed += 1
                    continue
                if index:
                    previous = _load_traffic_receipt(
                        _traffic_receipt_path(active, private_root, targets[index - 1]),
                        active,
                        targets[index - 1],
                    )
                    _wait_inter_version_pacing(
                        previous["invocation"]["completed_at"],
                        active.value["bindings"]["policy"][
                            "daily_inter_version_pacing_seconds"
                        ],
                        now=now,
                        sleeper=sleeper,
                    )
                elif not any(
                    _traffic_receipt_path(active, private_root, item).is_file()
                    for item in targets
                ):
                    delay = (
                        AGENT_ORDER.index(agent_name)
                        * active.value["bindings"]["policy"][
                            "agent_start_stagger_seconds"
                        ]
                    )
                    if delay:
                        sleeper(delay)
                requests = daily_issue_side_requests(target.traffic_path)
                invocation = runtime.invoke_version(
                    agent_name=target.agent_name,
                    agent_type=target.agent_type,
                    foundry_version=target.foundry_version,
                    traffic_path=target.traffic_path,
                    seed=_traffic_seed(active, target, index),
                    requests=requests,
                )
                receipt = _traffic_receipt(
                    active,
                    target,
                    invocation,
                    requests=requests,
                )
                with lock:
                    current = _assert_current(
                        active,
                        private_root,
                        allowed_states={"TRAFFIC"},
                    )
                    immutable_json(path, receipt)
                    _load_traffic_receipt(path, active, target)
                    active = current
                completed += 1
            _advance_phase_if_complete(
                active,
                private_root,
                source_phase="traffic",
                next_state="VERIFICATION",
                artifact_field="traffic_manifest",
                artifact_loader=_load_traffic_receipt_for_manifest,
            )
            current = DailyLifecycle(
                lock=_coordinator_lock(private_root),
                base=private_root,
            ).read_active()
            return {
                "status": "traffic_complete",
                "state": current.value["state"],
                "agent": agent_name,
                "completed_target_count": completed,
                "target_count": 5,
                "observations_per_target": 10,
                "telemetry_queries": 0,
                "insight_runs": 0,
            }
        finally:
            capacity.release()


def verify_next_daily_target(
    *,
    base: Path | None = None,
    profile_factory: Callable[[str], RuntimeProfile] | None = None,
    runtime_factory: Callable[[RuntimeProfile], Any] | None = None,
    claimant_reference: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    private_root = (base or runtime_root()).resolve()
    active = _open_phase(private_root, {"VERIFICATION"})
    claimant = claimant_reference or copilot_claimant_reference()
    claim = _claim_verification_target(active, private_root, claimant, now=now)
    if claim is None:
        return {
            "status": "verification_capacity_busy",
            "retryable": True,
            "state": "VERIFICATION",
        }
    target = _target_by_identity(
        active,
        str(claim["agent_name"]),
        str(claim["logical_version"]),
    )
    try:
        _, runtime, _ = _phase_context(
            active,
            private_root,
            agent_name=target.agent_name,
            profile_factory=profile_factory,
            runtime_factory=runtime_factory or TelemetryOnlyRuntime,
            allowed_states={"VERIFICATION"},
        )
        traffic = _load_traffic_receipt(
            _traffic_receipt_path(active, private_root, target),
            active,
            target,
        )
        result = _verify_target(
            target,
            traffic,
            runtime,
            policy=load_automation_policy(),
        )
        if result.status == "inconclusive":
            attempt = _verification_attempt(
                active,
                target,
                traffic,
                claim,
                result,
            )
            immutable_json(
                _verification_attempt_path(active, private_root, target, attempt),
                attempt,
            )
            _release_claim(
                active,
                private_root,
                claimant,
                expected_claim=claim,
            )
            return {
                "status": "verification_incomplete",
                "retryable": True,
                "authority": _target_key(target),
                "error_code": result.error_code,
            }
        value = _verification_result(active, target, traffic, claim, result)
        lock = _coordinator_lock(private_root)
        with lock:
            _assert_claim_current(active, private_root, claimant, claim)
            path = _verification_result_path(active, private_root, target)
            immutable_json(path, value)
            _claim_path(active, private_root, target).unlink()
        eligible_status = (
            "passed" if target.expected is None else "observed"
        )
        if result.status != eligible_status:
            _fail_daily_verification(
                active,
                private_root,
                target,
                value,
            )
            return {
                "status": "verification_failed",
                "state": "FAILED",
                "authority": _target_key(target),
                "error_code": result.error_code,
                "endpoint_requests": 0,
                "insight_runs": 0,
            }
        _advance_phase_if_complete(
            active,
            private_root,
            source_phase="verification",
            next_state="INSIGHTS",
            artifact_field="verification_manifest",
            artifact_loader=_load_verification_for_manifest,
        )
        current = DailyLifecycle(
            lock=_coordinator_lock(private_root),
            base=private_root,
        ).read_active()
        return {
            "status": "verified",
            "state": current.value["state"],
            "authority": _target_key(target),
            "result_status": result.status,
            "endpoint_requests": 0,
            "insight_runs": 0,
        }
    except Exception:
        _release_claim(
            active,
            private_root,
            claimant,
            expected_claim=claim,
            tolerate_stale=True,
        )
        raise


def release_daily_verification_claim(
    *,
    base: Path | None = None,
    claimant_reference: str | None = None,
) -> dict[str, Any]:
    private_root = (base or runtime_root()).resolve()
    active = _open_phase(private_root, {"VERIFICATION"})
    claimant = claimant_reference or copilot_claimant_reference()
    released = _release_claim(active, private_root, claimant)
    if released is None:
        raise ContractError("This evaluator has no active Daily verification claim")
    return {
        "status": "verification_claim_released",
        "authority": f"{released['agent_name']}/{released['logical_version']}",
        "claimant_reference": claimant,
    }


def run_daily_insights_agent(
    agent_name: str,
    *,
    base: Path | None = None,
    profile_factory: Callable[[str], RuntimeProfile] | None = None,
    runtime_factory: Callable[[RuntimeProfile], Any] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    if agent_name not in AGENT_ORDER:
        raise ContractError("Daily Insights Agent is unknown")
    private_root = (base or runtime_root()).resolve()
    active = _open_phase(private_root, {"INSIGHTS"})
    lane_lock = DailyLock(_run_root(active, private_root) / "insights" / agent_name / "lane.lock")
    with lane_lock:
        capacity = _acquire_capacity(
            active,
            private_root,
            phase="insights",
            maximum=active.value["bindings"]["policy"]["max_parallel_agents"],
        )
        try:
            _, runtime, targets = _phase_context(
                active,
                private_root,
                agent_name=agent_name,
                profile_factory=profile_factory,
                runtime_factory=runtime_factory,
                allowed_states={"INSIGHTS"},
            )
            registry = _registry(active)
            policy = load_automation_policy()
            _ensure_agent_monitor_reset(
                active,
                private_root,
                agent_name,
                runtime,
                monitor_id=registry["agents"][agent_name]["monitor_id"],
                now=now,
            )
            for target in targets:
                path = _insight_receipt_path(active, private_root, target)
                if path.is_file():
                    _load_insight_receipt(path, active, target)
                    continue
                traffic = _load_traffic_receipt(
                    _traffic_receipt_path(active, private_root, target),
                    active,
                    target,
                )
                verification = _load_verification_result(
                    _verification_result_path(active, private_root, target),
                    active,
                    target,
                    base=private_root,
                )
                value = _run_target_insights(
                    active,
                    private_root,
                    target,
                    traffic,
                    verification,
                    runtime,
                    monitor_id=registry["agents"][agent_name]["monitor_id"],
                    policy=policy,
                    now=now,
                )
                lock = _coordinator_lock(private_root)
                with lock:
                    _assert_current(active, private_root, allowed_states={"INSIGHTS"})
                    immutable_json(path, value)
            _publish_insight_manifest_if_complete(active, private_root)
            return {
                "status": "insights_complete",
                "agent": agent_name,
                "completed_target_count": 5,
                "target_count": 5,
                "endpoint_requests": 0,
            }
        finally:
            capacity.release()


def daily_phase_outputs(
    active: DailyRecord,
    *,
    base: Path,
) -> tuple[list[AgentResult], dict[tuple[str, str, tuple[str, ...]], dict[str, Any]]]:
    for phase in ("traffic", "verification", "insights"):
        _load_phase_manifest(active, base, phase)
    results: list[AgentResult] = []
    proofs: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}
    for agent_name in AGENT_ORDER:
        targets = _targets(active, agent_name)
        versions = []
        for target in targets:
            receipt = _load_insight_receipt(
                _insight_receipt_path(active, base, target),
                active,
                target,
            )
            result = _version_result(receipt["result"])
            versions.append(result)
            for item in receipt["card_trace_proofs"].values():
                operations = tuple(item["operation_ids"])
                proofs[(agent_name, target.logical_version, operations)] = dict(
                    item["trace_proof"]
                )
        results.append(
            AgentResult(
                agent_name=agent_name,
                baseline=versions[0],
                issues=versions[1:],
            )
        )
    return results, proofs


def _load_phase_manifest(
    active: DailyRecord,
    base: Path,
    phase: str,
) -> dict[str, Any]:
    field = {
        "traffic": "traffic_manifest",
        "verification": "verification_manifest",
        "insights": "insight_manifest",
    }[phase]
    reference = active.value["artifacts"][field]
    if not isinstance(reference, Mapping):
        raise ContractError(f"Daily {phase} phase manifest is missing")
    path = (daily_runtime_root(base) / str(reference["path"])).resolve()
    if daily_runtime_root(base).resolve() not in path.parents:
        raise ContractError("Daily phase manifest path escapes the runtime root")
    value = read_json(path)
    _validate_schema(value, _PHASE_SCHEMA, "Daily phase manifest")
    if (
        value["execution_id"] != active.value["execution_id"]
        or value["run_contract_digest"]
        != active.value["bindings"]["run_contract_digest"]
        or value["phase"] != phase
        or value["manifest_digest"] != reference["digest"]
        or value["manifest_digest"]
        != content_hash(
            {key: item for key, item in value.items() if key != "manifest_digest"}
        )
        or [
            (item["agent_name"], item["logical_version"])
            for item in value["artifacts"]
        ]
        != [
            (target.agent_name, target.logical_version)
            for target in _identity_targets(active)
        ]
    ):
        raise ContractError(f"Daily {phase} phase manifest binding is stale")
    return value


def phase_progress(active: DailyRecord, base: Path) -> dict[str, Any]:
    targets = _identity_targets(active)
    pending_traffic = [
        _target_key(target)
        for target in targets
        if not _traffic_receipt_path(active, base, target).is_file()
    ]
    pending_verification = [
        _target_key(target)
        for target in targets
        if not _verification_result_path(active, base, target).is_file()
    ]
    pending_insights = [
        _target_key(target)
        for target in targets
        if not _insight_receipt_path(active, base, target).is_file()
    ]
    claims = _active_claims(
        active,
        base,
        datetime.now(UTC),
        targets=targets,
        prune_expired=False,
    )
    return {
        "traffic_completed_target_count": 25 - len(pending_traffic),
        "traffic_pending_target_count": len(pending_traffic),
        "pending_traffic_targets": pending_traffic,
        "verification_completed_target_count": 25 - len(pending_verification),
        "verification_pending_target_count": len(pending_verification),
        "pending_verification_targets": pending_verification,
        "verification_active_claim_count": len(claims),
        "insight_completed_target_count": 25 - len(pending_insights),
        "insight_pending_target_count": len(pending_insights),
        "pending_insight_targets": pending_insights,
    }


def compute_insight_lookback(
    *,
    traffic_started_at: str,
    insight_started_at: datetime,
    start_margin_seconds: int,
    precision_minutes: int,
    minimum_hours: float,
    maximum_hours: float,
) -> dict[str, Any]:
    started = datetime.fromisoformat(traffic_started_at.replace("Z", "+00:00"))
    if started.tzinfo is None or insight_started_at.tzinfo is None:
        raise ContractError("Daily Insight lookback timestamps require timezones")
    elapsed = (
        insight_started_at.astimezone(UTC) - started.astimezone(UTC)
    ).total_seconds() + start_margin_seconds
    if elapsed <= 0 or precision_minutes < 1:
        raise ContractError("Daily Insight lookback inputs are invalid")
    quantum = precision_minutes * 60
    rounded_hours = math.ceil(elapsed / quantum) * quantum / 3600
    lookback_hours = max(minimum_hours, rounded_hours)
    if lookback_hours > maximum_hours:
        raise ContractError("Daily Insight lookback exceeds the reviewed maximum")
    value = {
        "traffic_started_at": started.astimezone(UTC).isoformat(),
        "insight_started_at": insight_started_at.astimezone(UTC).isoformat(),
        "start_margin_seconds": start_margin_seconds,
        "precision_minutes": precision_minutes,
        "minimum_hours": minimum_hours,
        "maximum_hours": maximum_hours,
        "lookback_hours": lookback_hours,
        "calculation_digest": "",
    }
    value["calculation_digest"] = content_hash(
        {key: item for key, item in value.items() if key != "calculation_digest"}
    )
    return value


def _run_target_insights(
    active: DailyRecord,
    base: Path,
    target: DailyTarget,
    traffic: Mapping[str, Any],
    verification: Mapping[str, Any],
    runtime: Any,
    *,
    monitor_id: str,
    policy: AutomationPolicy,
    now: Callable[[], datetime],
) -> dict[str, Any]:
    operation_ids = tuple(verification["result"]["operation_ids"])
    if not operation_ids:
        raise ContractError("Daily Insight target lacks verified operations")
    intent_path = _insight_intent_path(active, base, target)
    outcome_path = _insight_outcome_path(active, base, target)
    checkpoint: InsightRunCheckpoint | None = None
    if intent_path.is_file():
        intent = read_json(intent_path)
        _validate_insight_intent(intent, active, target, verification)
        if outcome_path.is_file():
            checkpoint = _load_insight_outcome(outcome_path, intent)
        else:
            status, discovered = runtime.discover_insights_run(
                agent_name=target.agent_name,
                monitor_id=monitor_id,
                foundry_version=target.foundry_version,
                operation_ids=operation_ids,
            )
            if status != "matched" or discovered is None:
                raise ContractError(
                    "Daily Insight start intent has no unique provider outcome"
                )
            checkpoint = discovered
            immutable_json(outcome_path, _insight_outcome(intent, discovered))
    else:
        started_at = now()
        lookback = compute_insight_lookback(
            traffic_started_at=str(traffic["invocation"]["started_at"]),
            insight_started_at=started_at,
            start_margin_seconds=policy.insight_start_margin_seconds,
            precision_minutes=policy.insight_lookback_precision_minutes,
            minimum_hours=policy.insight_lookback_hours,
            maximum_hours=policy.insight_lookback_max_hours,
        )
        intent = _insight_intent(
            active,
            target,
            verification,
            operation_ids,
            lookback,
        )
        lock = _coordinator_lock(base)
        with lock:
            _assert_current(active, base, allowed_states={"INSIGHTS"})
            immutable_json(intent_path, intent)

        def persist(value: InsightRunCheckpoint) -> None:
            with lock:
                _assert_current(active, base, allowed_states={"INSIGHTS"})
                immutable_json(outcome_path, _insight_outcome(intent, value))

        checkpoint = runtime.start_insights_run(
            agent_name=target.agent_name,
            monitor_id=monitor_id,
            foundry_version=target.foundry_version,
            operation_ids=operation_ids,
            lookback_hours=float(lookback["lookback_hours"]),
            start_margin_seconds=policy.insight_start_margin_seconds,
            intent_reference=str(intent["intent_digest"]),
            persist=persist,
        )
    assert checkpoint is not None
    run = runtime.finish_insights_run(
        agent_name=target.agent_name,
        monitor_id=monitor_id,
        foundry_version=target.foundry_version,
        operation_ids=operation_ids,
        checkpoint=checkpoint,
    )
    if run.status.casefold() != "succeeded":
        raise ContractError("Daily Agent Insights run did not succeed")
    cards = tuple(run.insights)
    for card in cards:
        if (
            card.agent_version != target.foundry_version
            or not linked_operations_match_scope(
                card.linked_operation_ids,
                operation_ids,
            )
        ):
            raise ContractError("Daily Agent Insights card linkage is out of scope")
    proofs = {}
    for card in cards:
        linked = tuple(sorted(card.linked_operation_ids))
        key = content_hash(linked)
        proofs[key] = {
            "operation_ids": list(linked),
            "trace_proof": runtime.trace_behavior_evidence(linked),
        }
    result = _version_result(verification["result"])
    result = replace(
        result,
        insight_references=[item.reference for item in cards],
        window_start=run.window_start,
        window_end=run.window_end,
        observed_insights=list(cards),
    )
    if target.expected is None:
        candidate_count = len(cards) + run.invalid_insight_count
        result.status = "passed" if candidate_count == 0 else "not_at_bar"
        result.error_code = (
            None if candidate_count == 0 else "unexpected_baseline_insight"
        )
    elif cards:
        result.status = "observed"
        result.error_code = None
        result.observed_insight = cards[0] if len(cards) == 1 else None
    else:
        result.status = "not_at_bar"
        result.error_code = (
            "invalid_insight_linkage"
            if run.invalid_insight_count
            else "missing_insight"
        )
    intent = read_json(intent_path)
    value = {
        "schema_version": "1.0.0",
        "kind": "daily-version-insight-receipt",
        "execution_id": active.value["execution_id"],
        "run_contract_digest": active.value["bindings"]["run_contract_digest"],
        "agent_name": target.agent_name,
        "logical_version": target.logical_version,
        "traffic_receipt_digest": traffic["receipt_digest"],
        "verification_result_digest": verification["result_digest"],
        "lookback": intent["lookback"],
        "intent": intent,
        "run": {
            "run_reference": run.run_reference,
            "window_start": run.window_start,
            "window_end": run.window_end,
            "status": run.status,
            "insight_count": len(cards),
            "invalid_insight_count": run.invalid_insight_count,
        },
        "card_trace_proofs": proofs,
        "result": asdict(result),
        "receipt_digest": "",
    }
    value["receipt_digest"] = content_hash(
        {key: item for key, item in value.items() if key != "receipt_digest"}
    )
    _validate_schema(value, _INSIGHT_SCHEMA, "Daily Insight receipt")
    return value


def _verify_target(
    target: DailyTarget,
    traffic: Mapping[str, Any],
    runtime: Any,
    *,
    policy: AutomationPolicy,
) -> VersionResult:
    invocation = _invocation(traffic["invocation"])
    operation_ids = runtime.wait_for_telemetry(
        agent_name=target.agent_name,
        foundry_version=target.foundry_version,
        invocation=invocation,
        poll_seconds=policy.clean_window_poll_seconds,
        maximum_wait_seconds=policy.clean_window_max_wait_seconds,
    )
    required = _required_trace_operations(
        agent={"type": target.agent_type},
        expected=target.expected,
        traffic_path=target.traffic_path,
        request_count=invocation.request_count,
    )
    runtime.verify_trace_contract(
        agent_name=target.agent_name,
        foundry_version=target.foundry_version,
        operation_ids=operation_ids,
        required_operations_by_request=required,
        window_start=invocation.started_at,
        window_end=invocation.completed_at,
    )
    trace_evidence: dict[str, Any] = {}
    maturity: dict[str, Any] | None = None

    def stable(value: dict[str, Any]) -> None:
        nonlocal trace_evidence
        trace_evidence = value

    def mature(value: dict[str, Any]) -> None:
        nonlocal maturity
        maturity = value

    requests = daily_issue_side_requests(target.traffic_path)
    trace_results = runtime.trace_assertion_evidence_for_requests(
        agent_name=target.agent_name,
        foundry_version=target.foundry_version,
        operation_ids=operation_ids,
        response_references=invocation.response_references,
        window_start=invocation.started_at,
        window_end=invocation.completed_at,
        requests=requests,
        stabilization_seconds=policy.trace_assertion_stabilization_seconds,
        on_first_pass=lambda: None,
        poll_seconds=policy.clean_window_poll_seconds,
        maximum_wait_seconds=policy.clean_window_max_wait_seconds,
        minimum_passing_trace_observations=_minimum_passing_trace_observations(
            requests,
            (
                issue_observation_context(target.traffic_path)
                if target.expected is not None
                else None
            ),
        ),
        on_stable=stable,
        on_maturity_proof=mature,
    )
    invocation = _with_trace_assertions(invocation, trace_results)
    if not trace_evidence:
        trace_evidence = runtime.trace_behavior_evidence(operation_ids)
    context = (
        issue_observation_context(target.traffic_path)
        if target.expected is not None
        else None
    )
    if context is None:
        decided, acceptance = _baseline_validation_decision(invocation, maturity)
        baseline_error = None
        try:
            _validate_baseline_trace_evidence(
                agent={
                    "name": target.agent_name,
                    "type": target.agent_type,
                    "baseline_contract": _agent_baseline_contract(target.agent_name),
                },
                invocation=invocation,
                trace_evidence=trace_evidence,
                accepted_unknown_count=(
                    len(acceptance["unknown_attempt_indices"])
                    if acceptance is not None
                    else 0
                ),
            )
        except ContractError:
            decided = False
            baseline_error = "baseline_evidence_failed"
        status = "passed" if decided else "failed"
        error_code = None if decided else baseline_error or "baseline_evidence_failed"
    else:
        decided, acceptance = _issue_activation_decision(context, invocation, maturity)
        status = "observed" if decided else "failed"
        error_code = None if decided else "issue_activation_failed"
    if not decided and not _evidence_is_conclusive(invocation, target):
        status = "inconclusive"
        error_code = "telemetry_evidence_incomplete"
    return VersionResult(
        logical_version=target.logical_version,
        foundry_version=target.foundry_version,
        status=status,
        operation_ids=list(operation_ids),
        window_start=invocation.started_at,
        window_end=invocation.completed_at,
        error_code=error_code,
        endpoint_request_count=invocation.request_count,
        endpoint_response_count=invocation.response_count,
        endpoint_usable_response_count=invocation.usable_response_count,
        semantic_assertion_count=invocation.semantic_assertion_count,
        semantic_assertions_passed=invocation.semantic_assertions_passed,
        trace_assertion_count=invocation.trace_assertion_count,
        trace_assertions_passed=invocation.trace_assertions_passed,
        trace_contract_verified=True,
        trace_behavior_summary=trace_evidence,
        trace_maturity_proof=maturity,
        trace_unknown_acceptance=acceptance,
        endpoint_request_summaries=list(invocation.request_summaries),
    )


def _fail_daily_verification(
    active: DailyRecord,
    base: Path,
    target: DailyTarget,
    verification: Mapping[str, Any],
) -> None:
    failure = {
        "schema_version": "1.0.0",
        "kind": "daily-verification-failure",
        "execution_id": active.value["execution_id"],
        "run_contract_digest": active.value["bindings"]["run_contract_digest"],
        "agent_name": target.agent_name,
        "logical_version": target.logical_version,
        "verification_result_digest": verification["result_digest"],
        "reason_code": str(verification["result"]["error_code"]),
        "failure_digest": "",
    }
    failure["failure_digest"] = content_hash(
        {key: item for key, item in failure.items() if key != "failure_digest"}
    )
    path = _run_root(active, base) / "verification" / "failure.json"
    lock = _coordinator_lock(base)
    with lock:
        current = _assert_current(active, base, allowed_states={"VERIFICATION"})
        immutable_json(path, failure)
        DailyLifecycle(lock=lock, base=base).transition(
            current,
            next_state="FAILED",
            artifact_updates={
                "failure": artifact_reference(
                    path,
                    daily_runtime_root(base),
                    failure["failure_digest"],
                )
            },
        )


def _traffic_receipt(
    active: DailyRecord,
    target: DailyTarget,
    invocation: InvocationEvidence,
    *,
    requests: list[dict[str, Any]],
) -> dict[str, Any]:
    if (
        invocation.operation_ids
        or invocation.allow_window_correlation
        or invocation.request_count != len(requests)
        or invocation.response_count != len(requests)
        or len(invocation.response_references) != len(requests)
        or len(set(invocation.response_references)) != len(requests)
        or sum(
            item.activation_gate for item in invocation.request_summaries
        )
        != 10
    ):
        raise ContractError(
            "Daily traffic did not produce ten definitive issue-side observations"
        )
    value = {
        "schema_version": "1.0.0",
        "kind": "daily-version-traffic-receipt",
        "execution_id": active.value["execution_id"],
        "run_contract_digest": active.value["bindings"]["run_contract_digest"],
        "agent_name": target.agent_name,
        "logical_version": target.logical_version,
        "foundry_version": target.foundry_version,
        "content_digest": target.content_digest,
        "traffic_contract_digest": content_hash(requests),
        "ambiguity_state": "definitive",
        "invocation": asdict(invocation),
        "receipt_digest": "",
    }
    value["receipt_digest"] = content_hash(
        {key: item for key, item in value.items() if key != "receipt_digest"}
    )
    _validate_schema(value, _TRAFFIC_SCHEMA, "Daily traffic receipt")
    return value


def _verification_result(
    active: DailyRecord,
    target: DailyTarget,
    traffic: Mapping[str, Any],
    claim: Mapping[str, Any],
    result: VersionResult,
) -> dict[str, Any]:
    value = {
        "schema_version": "1.0.0",
        "kind": "daily-version-verification-result",
        "execution_id": active.value["execution_id"],
        "run_contract_digest": active.value["bindings"]["run_contract_digest"],
        "agent_name": target.agent_name,
        "logical_version": target.logical_version,
        "traffic_receipt_digest": traffic["receipt_digest"],
        "claim_digest": claim["claim_digest"],
        "conclusive": True,
        "result": asdict(result),
        "result_digest": "",
    }
    value["result_digest"] = content_hash(
        {key: item for key, item in value.items() if key != "result_digest"}
    )
    _validate_schema(value, _VERIFICATION_SCHEMA, "Daily verification result")
    return value


def _verification_attempt(
    active: DailyRecord,
    target: DailyTarget,
    traffic: Mapping[str, Any],
    claim: Mapping[str, Any],
    result: VersionResult,
) -> dict[str, Any]:
    value = {
        "schema_version": "1.0.0",
        "kind": "daily-version-verification-attempt",
        "execution_id": active.value["execution_id"],
        "run_contract_digest": active.value["bindings"]["run_contract_digest"],
        "agent_name": target.agent_name,
        "logical_version": target.logical_version,
        "traffic_receipt_digest": traffic["receipt_digest"],
        "claim_digest": claim["claim_digest"],
        "conclusive": False,
        "result": asdict(result),
        "attempt_digest": "",
    }
    value["attempt_digest"] = content_hash(
        {key: item for key, item in value.items() if key != "attempt_digest"}
    )
    return value


def _claim_verification_target(
    active: DailyRecord,
    base: Path,
    claimant: str,
    *,
    now: Callable[[], datetime],
) -> dict[str, Any] | None:
    moment = now().astimezone(UTC)
    lock = _coordinator_lock(base)
    with lock:
        current = _assert_current(active, base, allowed_states={"VERIFICATION"})
        claims = _active_claims(
            current,
            base,
            moment,
            targets=_identity_targets(current),
        )
        owned = [item for item in claims if item["claimant_reference"] == claimant]
        if len(owned) > 1:
            raise ContractError("Daily evaluator owns multiple claims")
        if owned:
            return owned[0]
        if len(claims) >= int(
            current.value["bindings"]["policy"]["daily_verification_max_parallel"]
        ):
            return None
        claimed_keys = {
            (str(item["agent_name"]), str(item["logical_version"]))
            for item in claims
        }
        target = next(
            (
                item
                for item in _identity_targets(current)
                if not _verification_result_path(current, base, item).is_file()
                and (item.agent_name, item.logical_version) not in claimed_keys
            ),
            None,
        )
        if target is None:
            return None
        value = {
            "schema_version": "1.0.0",
            "kind": "daily-verification-claim",
            "execution_id": current.value["execution_id"],
            "run_contract_digest": current.value["bindings"]["run_contract_digest"],
            "agent_name": target.agent_name,
            "logical_version": target.logical_version,
            "claimant_reference": claimant,
            "claim_nonce": uuid.uuid4().hex,
            "claimed_at": moment.isoformat(),
            "lease_expires_at": (moment + _VERIFICATION_LEASE).isoformat(),
            "claim_digest": "",
        }
        value["claim_digest"] = content_hash(
            {key: item for key, item in value.items() if key != "claim_digest"}
        )
        atomic_json(_claim_path(current, base, target), value)
        return value


def _active_claims(
    active: DailyRecord,
    base: Path,
    moment: datetime,
    *,
    targets: list[DailyTarget] | None = None,
    prune_expired: bool = True,
) -> list[dict[str, Any]]:
    result = []
    for target in targets or _identity_targets(active):
        path = _claim_path(active, base, target)
        if not path.is_file():
            continue
        value = read_json(path)
        _validate_claim(value, active, target)
        expiry = datetime.fromisoformat(str(value["lease_expires_at"]))
        if expiry.astimezone(UTC) <= moment.astimezone(UTC):
            if prune_expired:
                path.unlink()
            continue
        if _verification_result_path(active, base, target).is_file():
            raise ContractError("Completed Daily verification retains an active claim")
        result.append(value)
    return result


def _release_claim(
    active: DailyRecord,
    base: Path,
    claimant: str,
    *,
    expected_claim: Mapping[str, Any] | None = None,
    tolerate_stale: bool = False,
) -> dict[str, Any] | None:
    lock = _coordinator_lock(base)
    with lock:
        try:
            current = _assert_current(active, base, allowed_states={"VERIFICATION"})
        except ContractError:
            if tolerate_stale:
                return None
            raise
        owned = []
        for target in _identity_targets(current):
            path = _claim_path(current, base, target)
            if not path.is_file():
                continue
            value = read_json(path)
            _validate_claim(value, current, target)
            if value["claimant_reference"] == claimant:
                owned.append((path, target, value))
        if len(owned) > 1:
            raise ContractError("Daily evaluator owns multiple claims")
        if not owned:
            return None
        path, target, value = owned[0]
        if expected_claim is not None and value["claim_digest"] != expected_claim["claim_digest"]:
            if tolerate_stale:
                return None
            raise ContractError("Daily verification claim changed before release")
        if _verification_result_path(current, base, target).is_file():
            raise ContractError("Completed Daily verification cannot be released")
        path.unlink()
        return value


def _advance_phase_if_complete(
    active: DailyRecord,
    base: Path,
    *,
    source_phase: str,
    next_state: str,
    artifact_field: str,
    artifact_loader: Callable[[Path, DailyRecord, DailyTarget], Mapping[str, Any]],
) -> None:
    lock = _coordinator_lock(base)
    with lock:
        current = DailyLifecycle(lock=lock, base=base).read_active()
        if current.value["state"] == next_state:
            return
        if current.value["state"] != source_phase.upper():
            raise ContractError("Daily phase barrier changed before completion")
        references = []
        for target in _targets(current):
            path = _phase_artifact_path(current, base, source_phase, target)
            if not path.is_file():
                return
            value = artifact_loader(path, current, target)
            references.append(
                {
                    "agent_name": target.agent_name,
                    "logical_version": target.logical_version,
                    "path": path.relative_to(daily_runtime_root(base)).as_posix(),
                    "digest": str(
                        value[
                            {
                                "traffic": "receipt_digest",
                                "verification": "result_digest",
                            }[source_phase]
                        ]
                    ),
                }
            )
        manifest = _phase_manifest(current, source_phase, references)
        path = _phase_manifest_path(current, base, source_phase)
        immutable_json(path, manifest)
        DailyLifecycle(lock=lock, base=base).transition(
            current,
            next_state=next_state,
            artifact_updates={
                artifact_field: artifact_reference(
                    path,
                    daily_runtime_root(base),
                    manifest["manifest_digest"],
                )
            },
        )


def _publish_insight_manifest_if_complete(active: DailyRecord, base: Path) -> None:
    lock = _coordinator_lock(base)
    with lock:
        current = DailyLifecycle(lock=lock, base=base).read_active()
        if current.value["state"] != "INSIGHTS":
            raise ContractError("Daily Insights phase changed before publication")
        references = []
        for target in _targets(current):
            path = _insight_receipt_path(current, base, target)
            if not path.is_file():
                return
            value = _load_insight_receipt(path, current, target)
            references.append(
                {
                    "agent_name": target.agent_name,
                    "logical_version": target.logical_version,
                    "path": path.relative_to(daily_runtime_root(base)).as_posix(),
                    "digest": value["receipt_digest"],
                }
            )
        manifest = _phase_manifest(current, "insights", references)
        path = _phase_manifest_path(current, base, "insights")
        immutable_json(path, manifest)
        DailyLifecycle(lock=lock, base=base).transition(
            current,
            next_state="INSIGHTS",
            artifact_updates={
                "insight_manifest": artifact_reference(
                    path,
                    daily_runtime_root(base),
                    manifest["manifest_digest"],
                )
            },
        )


def _phase_manifest(
    active: DailyRecord,
    phase: str,
    references: list[dict[str, str]],
) -> dict[str, Any]:
    value = {
        "schema_version": "1.0.0",
        "kind": "daily-phase-manifest",
        "execution_id": active.value["execution_id"],
        "run_contract_digest": active.value["bindings"]["run_contract_digest"],
        "phase": phase,
        "target_count": 25,
        "artifacts": references,
        "manifest_digest": "",
    }
    value["manifest_digest"] = content_hash(
        {key: item for key, item in value.items() if key != "manifest_digest"}
    )
    _validate_schema(value, _PHASE_SCHEMA, "Daily phase manifest")
    return value


def _phase_context(
    active: DailyRecord,
    base: Path,
    *,
    agent_name: str,
    profile_factory: Callable[[str], RuntimeProfile] | None,
    runtime_factory: Callable[[RuntimeProfile], Any] | None,
    allowed_states: set[str],
) -> tuple[RuntimeProfile, Any, list[DailyTarget]]:
    _assert_current(active, base, allowed_states=allowed_states)
    agents, issues = load_catalogs()
    if catalog_hashes(agents, issues) != active.value["bindings"]["catalog_hashes"]:
        raise ContractError("Daily catalogs changed after preparation")
    if _policy_payload(load_automation_policy()) != active.value["bindings"]["policy"]:
        raise ContractError("Daily policy changed after preparation")
    profile = (profile_factory or RuntimeProfile.from_env)("daily")
    profile.assert_insights_connection()
    profile.assert_test_agent_model(agent_model_contract(agents))
    registry = load_registry(
        profile.registry_path,
        profile="daily",
        catalog_hashes=active.value["bindings"]["catalog_hashes"],
    )
    if content_hash(registry) != active.value["bindings"]["registry"]["content_digest"]:
        raise ContractError("Daily registry changed after preparation")
    runtime = _FencedRuntime(
        (runtime_factory or LiveRuntime)(profile),
        lambda: _assert_current(active, base, allowed_states=allowed_states),
    )
    return profile, runtime, _targets(active, agent_name, registry=registry)


def _targets(
    active: DailyRecord,
    agent_name: str | None = None,
    *,
    registry: Mapping[str, Any] | None = None,
) -> list[DailyTarget]:
    agents, issues = load_catalogs()
    agent_by_name = {item["name"]: item for item in agents["agents"]}
    issue_by_id = {item["id"]: item for item in issues["issues"]}
    registry_value = registry or _registry(active)
    result = []
    names = [agent_name] if agent_name is not None else list(AGENT_ORDER)
    for name in names:
        agent = agent_by_name[name]
        logical_versions = ["v0", *active.value["bindings"]["selection"][name]]
        for logical_version in logical_versions:
            entry = version_entry(registry_value, name, logical_version)
            issue = issue_by_id.get(logical_version)
            result.append(
                DailyTarget(
                    agent_name=name,
                    logical_version=logical_version,
                    foundry_version=entry["foundry_version"],
                    content_digest=entry["content_digest"],
                    agent_type=agent["type"],
                    traffic_path=(
                        ROOT / agent["baseline_path"] / "traffic.json"
                        if issue is None
                        else ROOT / issue["implementation"] / "traffic.json"
                    ),
                    expected=issue,
                )
            )
    if agent_name is None and len(result) != 25:
        raise ContractError("Daily target inventory must contain exactly 25 versions")
    return result


def _identity_targets(active: DailyRecord) -> list[DailyTarget]:
    return [
        DailyTarget(
            agent_name=agent_name,
            logical_version=logical_version,
            foundry_version="",
            content_digest="",
            agent_type="",
            traffic_path=Path(),
            expected=None,
        )
        for agent_name in AGENT_ORDER
        for logical_version in [
            "v0",
            *active.value["bindings"]["selection"][agent_name],
        ]
    ]


def _target_by_identity(
    active: DailyRecord,
    agent_name: str,
    logical_version: str,
) -> DailyTarget:
    matches = [
        item
        for item in _targets(active, agent_name)
        if item.logical_version == logical_version
    ]
    if len(matches) != 1:
        raise ContractError("Daily target identity is invalid")
    return matches[0]


def _registry(active: DailyRecord) -> dict[str, Any]:
    profile = RuntimeProfile.from_env("daily")
    return load_registry(
        profile.registry_path,
        profile="daily",
        catalog_hashes=active.value["bindings"]["catalog_hashes"],
    )


def _open_phase(base: Path, states: set[str]) -> DailyRecord:
    active = DailyLifecycle(lock=_coordinator_lock(base), base=base).read_active()
    if active.value["state"] not in states:
        raise ContractError(
            f"Daily lifecycle is not ready for this phase (current: {active.value['state']})"
        )
    return active


def _assert_current(
    expected: DailyRecord,
    base: Path,
    *,
    allowed_states: set[str],
) -> DailyRecord:
    current = DailyLifecycle(lock=_coordinator_lock(base), base=base).read_active()
    if (
        current.value["state"] not in allowed_states
        or current.value["execution_id"] != expected.value["execution_id"]
        or current.value["bindings"]["run_contract_digest"]
        != expected.value["bindings"]["run_contract_digest"]
        or current.value["bindings"]["checkout_commit_sha"] != current_clean_commit()
    ):
        raise ContractError("Stale Daily phase worker is fenced")
    return current


class _FencedRuntime:
    def __init__(self, runtime: Any, fence: Callable[[], Any]) -> None:
        self._runtime = runtime
        self._fence = fence

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._runtime, name)
        if not callable(value):
            return value

        def fenced(*args: Any, **kwargs: Any) -> Any:
            self._fence()
            result = value(*args, **kwargs)
            self._fence()
            return result

        return fenced


def _wait_inter_version_pacing(
    completed_at: str,
    seconds: int,
    *,
    now: Callable[[], datetime],
    sleeper: Callable[[float], None],
) -> None:
    completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    moment = now()
    if completed.tzinfo is None or moment.tzinfo is None:
        raise ContractError("Daily pacing timestamps require timezones")
    remaining = (
        completed.astimezone(UTC) + timedelta(seconds=seconds) - moment.astimezone(UTC)
    ).total_seconds()
    if remaining > 0:
        sleeper(remaining)


def _evidence_is_conclusive(
    invocation: InvocationEvidence,
    target: DailyTarget,
) -> bool:
    required_trace = (
        target.expected is None
        or "trace" in issue_observation_context(target.traffic_path)["required_surfaces"]
    )
    return all(
        item.response_count == 1
        and item.usable_response
        and all(value.evidence_sufficient for value in item.assertion_results)
        and (
            not required_trace
            or (
                item.error_code != "missing_evidence"
                and all(
                    value.evidence_sufficient
                    for value in item.trace_assertion_results
                )
            )
        )
        for item in invocation.request_summaries
    )


def _invocation(value: Mapping[str, Any]) -> InvocationEvidence:
    return InvocationEvidence(
        operation_ids=tuple(value.get("operation_ids") or []),
        response_references=tuple(value["response_references"]),
        started_at=str(value["started_at"]),
        completed_at=str(value["completed_at"]),
        request_count=int(value["request_count"]),
        allow_window_correlation=bool(value["allow_window_correlation"]),
        response_count=int(value["response_count"]),
        usable_response_count=int(value["usable_response_count"]),
        semantic_assertion_count=int(value["semantic_assertion_count"]),
        semantic_assertions_passed=int(value["semantic_assertions_passed"]),
        trace_assertion_count=int(value.get("trace_assertion_count") or 0),
        trace_assertions_passed=int(value.get("trace_assertions_passed") or 0),
        request_summaries=tuple(_request_summary(item) for item in value["request_summaries"]),
        session_references=tuple(value.get("session_references") or []),
    )


def _request_summary(value: Mapping[str, Any]) -> RequestCompletionEvidence:
    return RequestCompletionEvidence(
        request_index=int(value["request_index"]),
        response_count=int(value["response_count"]),
        usable_response=bool(value["usable_response"]),
        semantic_assertion_count=int(value["semantic_assertion_count"]),
        semantic_assertions_passed=int(value["semantic_assertions_passed"]),
        assertion_results=tuple(
            SemanticAssertionEvidence(
                assertion=str(item["assertion"]),
                passed=bool(item["passed"]),
                evidence_sufficient=bool(item["evidence_sufficient"]),
            )
            for item in value["assertion_results"]
        ),
        activation_gate=bool(value["activation_gate"]),
        direct_terminal_response_count=int(value["direct_terminal_response_count"]),
        function_call_count=int(value["function_call_count"]),
        trace_assertion_count=int(value.get("trace_assertion_count") or 0),
        trace_assertions_passed=int(value.get("trace_assertions_passed") or 0),
        trace_assertion_results=tuple(
            TraceAssertionEvidence(
                assertion=str(item["assertion"]),
                passed=bool(item["passed"]),
                evidence_sufficient=bool(item["evidence_sufficient"]),
            )
            for item in value.get("trace_assertion_results", [])
        ),
        error_code=value.get("error_code"),
    )


def _version_result(value: Mapping[str, Any]) -> VersionResult:
    primary = value.get("observed_insight")
    return VersionResult(
        logical_version=str(value["logical_version"]),
        foundry_version=str(value["foundry_version"]),
        status=str(value["status"]),
        operation_ids=list(value.get("operation_ids") or []),
        insight_references=list(value.get("insight_references") or []),
        window_start=value.get("window_start"),
        window_end=value.get("window_end"),
        error_code=value.get("error_code"),
        observed_insight=_insight(primary) if isinstance(primary, Mapping) else None,
        observed_insights=[_insight(item) for item in value.get("observed_insights", [])],
        endpoint_request_count=int(value.get("endpoint_request_count") or 0),
        endpoint_response_count=int(value.get("endpoint_response_count") or 0),
        endpoint_usable_response_count=int(value.get("endpoint_usable_response_count") or 0),
        semantic_assertion_count=int(value.get("semantic_assertion_count") or 0),
        semantic_assertions_passed=int(value.get("semantic_assertions_passed") or 0),
        trace_assertion_count=int(value.get("trace_assertion_count") or 0),
        trace_assertions_passed=int(value.get("trace_assertions_passed") or 0),
        trace_contract_verified=bool(value.get("trace_contract_verified")),
        trace_behavior_summary=dict(value.get("trace_behavior_summary") or {}),
        trace_maturity_proof=value.get("trace_maturity_proof"),
        trace_unknown_acceptance=value.get("trace_unknown_acceptance"),
        endpoint_request_summaries=[
            _request_summary(item) for item in value.get("endpoint_request_summaries", [])
        ],
    )


def _insight(value: Mapping[str, Any]) -> InsightEvidence:
    return InsightEvidence(
        reference=str(value["reference"]),
        agent_version=str(value["agent_version"]),
        title=str(value["title"]),
        description=str(value["description"]),
        category=str(value["category"]),
        severity=str(value["severity"]),
        proposed_fix=str(value["proposed_fix"]),
        linked_operation_ids=tuple(value["linked_operation_ids"]),
        trace_count=int(value["trace_count"]),
        updated_at=str(value["updated_at"]),
    )


def _load_traffic_receipt(
    path: Path,
    active: DailyRecord,
    target: DailyTarget,
) -> dict[str, Any]:
    value = read_json(path)
    _validate_schema(value, _TRAFFIC_SCHEMA, "Daily traffic receipt")
    if (
        value["execution_id"] != active.value["execution_id"]
        or value["run_contract_digest"] != active.value["bindings"]["run_contract_digest"]
        or value["agent_name"] != target.agent_name
        or value["logical_version"] != target.logical_version
        or value["foundry_version"] != target.foundry_version
        or value["content_digest"] != target.content_digest
        or value["traffic_contract_digest"]
        != content_hash(daily_issue_side_requests(target.traffic_path))
        or value["receipt_digest"]
        != content_hash(
            {key: item for key, item in value.items() if key != "receipt_digest"}
        )
    ):
        raise ContractError("Daily traffic receipt binding is stale")
    _traffic_receipt(
        active,
        target,
        _invocation(value["invocation"]),
        requests=daily_issue_side_requests(target.traffic_path),
    )
    return value


def _load_verification_result(
    path: Path,
    active: DailyRecord,
    target: DailyTarget,
    *,
    base: Path,
) -> dict[str, Any]:
    value = read_json(path)
    _validate_schema(value, _VERIFICATION_SCHEMA, "Daily verification result")
    traffic = _load_traffic_receipt(
        _traffic_receipt_path(active, base, target),
        active,
        target,
    )
    if (
        value["execution_id"] != active.value["execution_id"]
        or value["run_contract_digest"] != active.value["bindings"]["run_contract_digest"]
        or value["agent_name"] != target.agent_name
        or value["logical_version"] != target.logical_version
        or value["traffic_receipt_digest"] != traffic["receipt_digest"]
        or value["result_digest"]
        != content_hash(
            {key: item for key, item in value.items() if key != "result_digest"}
        )
        or _version_result(value["result"]).status == "inconclusive"
    ):
        raise ContractError("Daily verification result binding is stale")
    return value


def _load_insight_receipt(
    path: Path,
    active: DailyRecord,
    target: DailyTarget,
) -> dict[str, Any]:
    value = read_json(path)
    _validate_schema(value, _INSIGHT_SCHEMA, "Daily Insight receipt")
    if (
        value["execution_id"] != active.value["execution_id"]
        or value["run_contract_digest"] != active.value["bindings"]["run_contract_digest"]
        or value["agent_name"] != target.agent_name
        or value["logical_version"] != target.logical_version
        or value["receipt_digest"]
        != content_hash(
            {key: item for key, item in value.items() if key != "receipt_digest"}
        )
    ):
        raise ContractError("Daily Insight receipt binding is stale")
    return value


def _load_traffic_receipt_for_manifest(
    path: Path,
    active: DailyRecord,
    target: DailyTarget,
) -> Mapping[str, Any]:
    return _load_traffic_receipt(path, active, target)


def _load_verification_for_manifest(
    path: Path,
    active: DailyRecord,
    target: DailyTarget,
) -> Mapping[str, Any]:
    value = read_json(path)
    _validate_schema(value, _VERIFICATION_SCHEMA, "Daily verification result")
    if (
        value["agent_name"] != target.agent_name
        or value["logical_version"] != target.logical_version
        or value["execution_id"] != active.value["execution_id"]
        or value["run_contract_digest"] != active.value["bindings"]["run_contract_digest"]
        or value["result_digest"]
        != content_hash(
            {key: item for key, item in value.items() if key != "result_digest"}
        )
    ):
        raise ContractError("Daily verification result binding is stale")
    return value


def _insight_intent(
    active: DailyRecord,
    target: DailyTarget,
    verification: Mapping[str, Any],
    operation_ids: tuple[str, ...],
    lookback: Mapping[str, Any],
) -> dict[str, Any]:
    value = {
        "schema_version": "1.0.0",
        "kind": "daily-insight-start-intent",
        "execution_id": active.value["execution_id"],
        "run_contract_digest": active.value["bindings"]["run_contract_digest"],
        "agent_name": target.agent_name,
        "logical_version": target.logical_version,
        "foundry_version": target.foundry_version,
        "verification_result_digest": verification["result_digest"],
        "operation_ids_digest": content_hash(operation_ids),
        "lookback": dict(lookback),
        "intent_digest": "",
    }
    value["intent_digest"] = content_hash(
        {key: item for key, item in value.items() if key != "intent_digest"}
    )
    return value


def _ensure_agent_monitor_reset(
    active: DailyRecord,
    base: Path,
    agent_name: str,
    runtime: Any,
    *,
    monitor_id: str,
    now: Callable[[], datetime],
) -> None:
    intent_path = _monitor_reset_intent_path(active, base, agent_name)
    outcome_path = _monitor_reset_outcome_path(active, base, agent_name)
    if outcome_path.is_file():
        outcome = read_json(outcome_path)
        if (
            outcome.get("kind") != "daily-agent-monitor-reset-outcome"
            or outcome.get("execution_id") != active.value["execution_id"]
            or outcome.get("agent_name") != agent_name
            or outcome.get("intent_digest")
            != read_json(intent_path).get("intent_digest")
            or outcome.get("outcome_digest")
            != content_hash(
                {
                    key: item
                    for key, item in outcome.items()
                    if key != "outcome_digest"
                }
            )
        ):
            raise ContractError("Daily monitor-reset outcome binding is stale")
        return
    if intent_path.is_file():
        raise ContractError(
            "Daily monitor reset has an unresolved provider outcome; it will not be repeated"
        )
    moment = now()
    if moment.tzinfo is None:
        raise ContractError("Daily monitor-reset clock requires a timezone")
    intent = {
        "schema_version": "1.0.0",
        "kind": "daily-agent-monitor-reset-intent",
        "execution_id": active.value["execution_id"],
        "run_contract_digest": active.value["bindings"]["run_contract_digest"],
        "agent_name": agent_name,
        "monitor_reference": content_hash({"monitor_id": monitor_id}),
        "requested_at": moment.astimezone(UTC).isoformat(),
        "intent_digest": "",
    }
    intent["intent_digest"] = content_hash(
        {key: item for key, item in intent.items() if key != "intent_digest"}
    )
    lock = _coordinator_lock(base)
    with lock:
        _assert_current(active, base, allowed_states={"INSIGHTS"})
        immutable_json(intent_path, intent)
    runtime.reset_monitor(agent_name, monitor_id)
    outcome = {
        "schema_version": "1.0.0",
        "kind": "daily-agent-monitor-reset-outcome",
        "execution_id": active.value["execution_id"],
        "agent_name": agent_name,
        "intent_digest": intent["intent_digest"],
        "completed_at": now().astimezone(UTC).isoformat(),
        "outcome_digest": "",
    }
    outcome["outcome_digest"] = content_hash(
        {key: item for key, item in outcome.items() if key != "outcome_digest"}
    )
    with lock:
        _assert_current(active, base, allowed_states={"INSIGHTS"})
        immutable_json(outcome_path, outcome)


def _validate_insight_intent(
    value: Mapping[str, Any],
    active: DailyRecord,
    target: DailyTarget,
    verification: Mapping[str, Any],
) -> None:
    if (
        value.get("kind") != "daily-insight-start-intent"
        or value.get("execution_id") != active.value["execution_id"]
        or value.get("run_contract_digest")
        != active.value["bindings"]["run_contract_digest"]
        or value.get("agent_name") != target.agent_name
        or value.get("logical_version") != target.logical_version
        or value.get("foundry_version") != target.foundry_version
        or value.get("verification_result_digest") != verification["result_digest"]
        or value.get("operation_ids_digest")
        != content_hash(tuple(verification["result"]["operation_ids"]))
        or value.get("intent_digest")
        != content_hash(
            {key: item for key, item in value.items() if key != "intent_digest"}
        )
    ):
        raise ContractError("Daily Insight start intent binding is stale")


def _insight_outcome(
    intent: Mapping[str, Any],
    checkpoint: InsightRunCheckpoint,
) -> dict[str, Any]:
    value = {
        "schema_version": "1.0.0",
        "kind": "daily-insight-start-outcome",
        "intent_digest": intent["intent_digest"],
        "run_id": checkpoint.run_id,
        "before_revisions": {
            key: list(item) for key, item in checkpoint.before_revisions.items()
        },
        "outcome_digest": "",
    }
    value["outcome_digest"] = content_hash(
        {key: item for key, item in value.items() if key != "outcome_digest"}
    )
    return value


def _load_insight_outcome(
    path: Path,
    intent: Mapping[str, Any],
) -> InsightRunCheckpoint:
    value = read_json(path)
    if (
        value.get("kind") != "daily-insight-start-outcome"
        or value.get("intent_digest") != intent["intent_digest"]
        or value.get("outcome_digest")
        != content_hash(
            {key: item for key, item in value.items() if key != "outcome_digest"}
        )
    ):
        raise ContractError("Daily Insight start outcome binding is stale")
    return InsightRunCheckpoint(
        run_id=str(value["run_id"]),
        before_revisions={
            str(key): (str(item[0]), int(item[1]))
            for key, item in value["before_revisions"].items()
        },
    )


def _validate_claim(
    value: Mapping[str, Any],
    active: DailyRecord,
    target: DailyTarget,
) -> None:
    if (
        value.get("kind") != "daily-verification-claim"
        or value.get("execution_id") != active.value["execution_id"]
        or value.get("run_contract_digest")
        != active.value["bindings"]["run_contract_digest"]
        or value.get("agent_name") != target.agent_name
        or value.get("logical_version") != target.logical_version
        or value.get("claim_digest")
        != content_hash(
            {key: item for key, item in value.items() if key != "claim_digest"}
        )
    ):
        raise ContractError("Daily verification claim binding is stale")


def _assert_claim_current(
    active: DailyRecord,
    base: Path,
    claimant: str,
    claim: Mapping[str, Any],
) -> None:
    current = _assert_current(active, base, allowed_states={"VERIFICATION"})
    target = _target_by_identity(
        current,
        str(claim["agent_name"]),
        str(claim["logical_version"]),
    )
    value = read_json(_claim_path(current, base, target))
    _validate_claim(value, current, target)
    if (
        value["claimant_reference"] != claimant
        or value["claim_digest"] != claim["claim_digest"]
        or datetime.fromisoformat(value["lease_expires_at"]).astimezone(UTC)
        <= datetime.now(UTC)
        or _verification_result_path(current, base, target).is_file()
    ):
        raise ContractError("Daily verification claim is no longer publishable")


def _validate_schema(value: Mapping[str, Any], path: Path, label: str) -> None:
    errors = sorted(
        Draft202012Validator(
            read_json(path),
            format_checker=FormatChecker(),
        ).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise ContractError(f"{label} schema error at {location}: {errors[0].message}")


def _policy_payload(policy: AutomationPolicy) -> dict[str, Any]:
    return {
        "max_parallel_agents": policy.max_parallel_agents,
        "daily_verification_max_parallel": policy.daily_verification_max_parallel,
        "daily_inter_version_pacing_seconds": policy.daily_inter_version_pacing_seconds,
        "max_recovery_versions": policy.max_recovery_versions,
        "agent_start_stagger_seconds": policy.agent_start_stagger_seconds,
        "insight_lookback_hours": policy.insight_lookback_hours,
        "insight_lookback_max_hours": policy.insight_lookback_max_hours,
        "insight_lookback_precision_minutes": policy.insight_lookback_precision_minutes,
        "clean_window_poll_seconds": policy.clean_window_poll_seconds,
        "clean_window_ingestion_margin_seconds": policy.clean_window_ingestion_margin_seconds,
        "clean_window_max_wait_seconds": policy.clean_window_max_wait_seconds,
        "trace_assertion_stabilization_seconds": policy.trace_assertion_stabilization_seconds,
        "insight_start_margin_seconds": policy.insight_start_margin_seconds,
        "telemetry_resource_set": policy.telemetry_resource_set,
    }


def _agent_baseline_contract(agent_name: str) -> dict[str, Any]:
    agents, _ = load_catalogs()
    return next(
        item["baseline_contract"]
        for item in agents["agents"]
        if item["name"] == agent_name
    )


def _traffic_seed(active: DailyRecord, target: DailyTarget, index: int) -> int:
    return int(
        content_hash(
            {
                "execution_id": active.value["execution_id"],
                "agent": target.agent_name,
                "version": target.logical_version,
                "index": index,
            }
        ).split(":", 1)[1][:16],
        16,
    )


def _acquire_capacity(
    active: DailyRecord,
    base: Path,
    *,
    phase: str,
    maximum: int,
) -> DailyLock:
    for slot in range(1, maximum + 1):
        lock = DailyLock(
            _run_root(active, base)
            / "capacity"
            / phase
            / f"slot-{slot:02d}.lock"
        )
        try:
            lock.acquire()
        except ContractError:
            continue
        return lock
    raise ContractError(f"Daily {phase} parallel capacity is already active")


def _coordinator_lock(base: Path) -> DailyLock:
    return DailyLock(daily_runtime_root(base) / "coordinator.lock")


def _run_root(active: DailyRecord, base: Path) -> Path:
    return (
        daily_runtime_root(base)
        / "runs"
        / active.value["bindings"]["public_run_id"]
    )


def _target_key(target: DailyTarget) -> str:
    return f"{target.agent_name}/{target.logical_version}"


def _traffic_receipt_path(
    active: DailyRecord,
    base: Path,
    target: DailyTarget,
) -> Path:
    return (
        _run_root(active, base)
        / "traffic"
        / "receipts"
        / target.agent_name
        / f"{target.logical_version}.json"
    )


def _verification_result_path(
    active: DailyRecord,
    base: Path,
    target: DailyTarget,
) -> Path:
    return (
        _run_root(active, base)
        / "verification"
        / "results"
        / target.agent_name
        / f"{target.logical_version}.json"
    )


def _verification_attempt_path(
    active: DailyRecord,
    base: Path,
    target: DailyTarget,
    value: Mapping[str, Any],
) -> Path:
    return (
        _run_root(active, base)
        / "verification"
        / "attempts"
        / target.agent_name
        / target.logical_version
        / f"{str(value['attempt_digest']).removeprefix('sha256:')}.json"
    )


def _claim_path(active: DailyRecord, base: Path, target: DailyTarget) -> Path:
    return (
        _run_root(active, base)
        / "verification"
        / "claims"
        / target.agent_name
        / f"{target.logical_version}.json"
    )


def _insight_receipt_path(
    active: DailyRecord,
    base: Path,
    target: DailyTarget,
) -> Path:
    return (
        _run_root(active, base)
        / "insights"
        / "receipts"
        / target.agent_name
        / f"{target.logical_version}.json"
    )


def _insight_intent_path(
    active: DailyRecord,
    base: Path,
    target: DailyTarget,
) -> Path:
    return (
        _run_root(active, base)
        / "insights"
        / "intents"
        / target.agent_name
        / f"{target.logical_version}.json"
    )


def _insight_outcome_path(
    active: DailyRecord,
    base: Path,
    target: DailyTarget,
) -> Path:
    return (
        _run_root(active, base)
        / "insights"
        / "start-outcomes"
        / target.agent_name
        / f"{target.logical_version}.json"
    )


def _monitor_reset_intent_path(
    active: DailyRecord,
    base: Path,
    agent_name: str,
) -> Path:
    return (
        _run_root(active, base)
        / "insights"
        / "monitor-resets"
        / agent_name
        / "intent.json"
    )


def _monitor_reset_outcome_path(
    active: DailyRecord,
    base: Path,
    agent_name: str,
) -> Path:
    return (
        _run_root(active, base)
        / "insights"
        / "monitor-resets"
        / agent_name
        / "outcome.json"
    )


def _phase_manifest_path(active: DailyRecord, base: Path, phase: str) -> Path:
    return _run_root(active, base) / "phase-manifests" / f"{phase}.json"


def _phase_artifact_path(
    active: DailyRecord,
    base: Path,
    phase: str,
    target: DailyTarget,
) -> Path:
    if phase == "traffic":
        return _traffic_receipt_path(active, base, target)
    if phase == "verification":
        return _verification_result_path(active, base, target)
    raise ContractError("Daily phase artifact type is invalid")
