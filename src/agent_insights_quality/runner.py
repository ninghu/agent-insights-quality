from __future__ import annotations

import re
import threading
import time
import math
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from agent_insights_quality.models import (
    AgentResult,
    InsightRunCheckpoint,
    InsightRunEvidence,
    InvocationEvidence,
    SKIPPED_VERSION_STATUSES,
    VersionResult,
    linked_operations_match_scope,
    request_completion_payload,
)
from agent_insights_quality.validation_trace_gap_policy import (
    daily_target_decision,
    validate_trace_maturity_proof,
)
from agent_insights_quality.registry import version_entry
from agent_insights_quality.runtime_state import VersionCheckpointStore
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    InsightWindowExpiredError,
    TraceAssertionActivationError,
    content_hash,
)
from agent_insights_quality.validation_rules import (
    daily_issue_side_requests,
    execution_context,
    issue_observation_context,
    validation_matrix,
)


class _VersionStageError(Exception):
    def __init__(self, code: str, cause: Exception) -> None:
        self.cause = cause
        if isinstance(cause, InsightWindowExpiredError):
            self.code = "insight_window_expired"
            super().__init__(self.code)
            return
        suffix = ""
        http = re.search(r"\bHTTP ([0-9]{3})\b", str(cause))
        if http:
            suffix = f"_http_{http.group(1)}"
        elif "Azure CLI" in str(cause):
            suffix = "_credential"
        elif "deadline" in str(cause).casefold():
            suffix = "_timeout"
        self.code = code + suffix
        super().__init__(self.code)


class _RecoveryBudget:
    def __init__(
        self,
        maximum: int,
        checkpoint_store: VersionCheckpointStore | None = None,
        agent_name: str | None = None,
    ) -> None:
        self._maximum = maximum
        self._checkpoint_store = checkpoint_store
        self._agent_name = agent_name
        self._claimed = 0
        self._lock = threading.Lock()

    def claim(self) -> bool:
        if self._checkpoint_store is not None and self._agent_name is not None:
            return self._checkpoint_store.claim_agent_recovery(
                self._agent_name,
                self._maximum,
            )
        with self._lock:
            if self._claimed >= self._maximum:
                return False
            self._claimed += 1
            return True


class _StartStagger:
    def __init__(self, seconds: int) -> None:
        self._seconds = seconds
        self._used = False

    def wait_once(self, runtime: Any, agent_name: str) -> None:
        if self._used:
            return
        self._used = True
        if self._seconds:
            _progress(
                runtime,
                f"{agent_name}: staggering endpoint traffic by {self._seconds}s",
            )
            time.sleep(self._seconds)


class _MonitorReset:
    def __init__(self) -> None:
        self._completed = False

    def ensure(
        self,
        runtime: RuntimePort,
        *,
        agent_name: str,
        monitor_id: str,
        checkpoint_store: VersionCheckpointStore | None,
    ) -> None:
        if self._completed:
            return
        if checkpoint_store is None:
            runtime.reset_monitor(agent_name, monitor_id)
        else:
            checkpoint_store.ensure_agent_monitor_reset(
                agent_name,
                content_hash({"monitor_id": monitor_id}),
                lambda: runtime.reset_monitor(agent_name, monitor_id),
            )
        self._completed = True


def _stage_error_code(error: Exception) -> str:
    return (
        error.code
        if isinstance(error, _VersionStageError)
        else type(error).__name__
    )


def _telemetry_validation_error_code(error_code: str | None) -> bool:
    return bool(error_code) and str(error_code).startswith(
        (
            "telemetry_failed",
            "trace_contract_failed",
            "trace_evidence_failed",
            "trace_assertion_failed",
        )
    )


def _insight_provider_error_code(error_code: str | None) -> bool:
    return bool(error_code) and str(error_code).startswith(
        (
            "insight_monitor_reset_failed",
            "insight_run_poll_failed",
            "insight_run_terminal_failed",
            "insight_window_expired",
        )
    )


def _preflight_error_code(stage: str, error: Exception) -> str:
    if stage == "clean_window" and "pre-existing traces" in str(error):
        return "clean_window_not_empty"
    return f"{stage}_failed"


def _progress(runtime: Any, message: str) -> None:
    reporter = getattr(runtime, "report_progress", None)
    if callable(reporter):
        reporter(message)


def _version_insight_lookback(
    runtime: Any,
    invocation: InvocationEvidence,
    *,
    minimum_hours: float,
    maximum_hours: float,
    precision_minutes: int,
    margin_seconds: int,
) -> dict[str, Any]:
    started = datetime.fromisoformat(
        invocation.started_at.replace("Z", "+00:00")
    )
    current_time = getattr(runtime, "current_time", None)
    observed = (
        current_time()
        if callable(current_time)
        else datetime.fromisoformat(
            invocation.completed_at.replace("Z", "+00:00")
        )
    )
    if (
        started.tzinfo is None
        or observed.tzinfo is None
        or precision_minutes < 1
    ):
        raise ContractError("Daily Insight lookback inputs are invalid")
    elapsed = (
        observed.astimezone(UTC) - started.astimezone(UTC)
    ).total_seconds() + margin_seconds
    quantum = precision_minutes * 60
    hours = max(
        minimum_hours,
        math.ceil(max(elapsed, 0) / quantum) * quantum / 3600,
    )
    if hours > maximum_hours:
        raise ContractError("Daily Insight lookback exceeds the reviewed maximum")
    value = {
        "traffic_started_at": started.astimezone(UTC).isoformat(),
        "insight_started_at": observed.astimezone(UTC).isoformat(),
        "margin_seconds": margin_seconds,
        "precision_minutes": precision_minutes,
        "minimum_hours": minimum_hours,
        "maximum_hours": maximum_hours,
        "lookback_hours": hours,
        "calculation_digest": "",
    }
    value["calculation_digest"] = content_hash(
        {key: item for key, item in value.items() if key != "calculation_digest"}
    )
    return value


def _required_trace_operations(
    *,
    agent: dict[str, Any],
    expected: dict[str, Any] | None,
    traffic_path: Path,
    request_count: int,
) -> tuple[tuple[str, ...], ...]:
    requests = daily_issue_side_requests(traffic_path)
    if len(requests) != request_count:
        raise ContractError("Traffic request operation contract is incomplete")
    required: list[tuple[str, ...]] = []
    for request in requests:
        expected_request = (
            request.get("expected") if isinstance(request, dict) else None
        )
        trace_assertions = (
            expected_request.get("trace_assertions")
            if isinstance(expected_request, dict)
            else None
        )
        sequence = next(
            (
                item.get("operations")
                for item in trace_assertions or []
                if isinstance(item, dict)
                and item.get("kind") == "operation_sequence"
                and item.get("name") == "required_operation_sequence"
            ),
            None,
        )
        if isinstance(sequence, list) and sequence:
            required.append(tuple(str(operation) for operation in sequence))
        elif (
            expected is not None
            and isinstance(expected_request, dict)
            and expected_request.get("activation_gate") is True
        ):
            required.append(tuple(expected["trace_contract"]["operations"]))
        else:
            required.append(("invoke_agent", "chat"))
    return tuple(required)


class RuntimePort(Protocol):
    def reset_monitor(self, agent_name: str, monitor_id: str) -> None: ...

    def wait_for_clean_window(
        self,
        agent_name: str,
        lookback_hours: float,
        *,
        poll_seconds: int,
        ingestion_margin_seconds: int,
        max_wait_seconds: int,
    ) -> None: ...

    def invoke_version(
        self,
        *,
        agent_name: str,
        agent_type: str,
        foundry_version: str,
        traffic_path: Path,
        seed: int,
        requests: list[dict[str, Any]],
    ) -> InvocationEvidence: ...

    def wait_for_telemetry(
        self,
        *,
        agent_name: str,
        foundry_version: str,
        invocation: InvocationEvidence,
        poll_seconds: int,
        maximum_wait_seconds: int,
        minimum_grace_seconds: int,
        maximum_poll_seconds: int,
        age_bounded: bool,
    ) -> tuple[str, ...]: ...

    def verify_trace_contract(
        self,
        *,
        agent_name: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        required_operations_by_request: tuple[tuple[str, ...], ...],
        window_start: str,
        window_end: str,
        poll_seconds: int,
        maximum_wait_seconds: int,
        maximum_poll_seconds: int,
        age_bounded: bool,
    ) -> None: ...

    def trace_behavior_evidence(
        self,
        operation_ids: tuple[str, ...],
    ) -> dict[str, Any]: ...

    def trace_assertion_evidence(
        self,
        *,
        agent_name: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        response_references: tuple[str, ...],
        window_start: str,
        window_end: str,
        traffic_path: Path,
        requests: list[dict[str, Any]],
        stabilization_seconds: int,
        on_first_pass: Callable[[], None],
        minimum_passing_trace_observations: int,
        on_stable: Callable[[dict[str, Any]], None] | None = None,
        on_maturity_proof: Callable[[dict[str, Any]], None] | None = None,
        poll_seconds: int,
        maximum_wait_seconds: int,
        minimum_grace_seconds: int,
        maximum_poll_seconds: int,
        age_bounded: bool,
    ) -> tuple[tuple[Any, ...], ...]: ...

    def start_insights_run(
        self,
        *,
        agent_name: str,
        monitor_id: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        lookback_hours: float,
        start_margin_seconds: int,
        intent_reference: str,
        persist: Callable[[InsightRunCheckpoint], None],
    ) -> InsightRunCheckpoint: ...

    def finish_insights_run(
        self,
        *,
        agent_name: str,
        monitor_id: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        checkpoint: InsightRunCheckpoint,
        validate_window: bool = True,
    ) -> InsightRunEvidence: ...

    def discover_insights_run(
        self,
        *,
        agent_name: str,
        monitor_id: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
    ) -> tuple[str, InsightRunCheckpoint | None]: ...


def execute(
    *,
    agents: dict[str, Any],
    issues: dict[str, Any],
    selected: dict[str, list[str]],
    registry: dict[str, Any],
    runtime: RuntimePort,
    seed: int,
    lookback_hours: float = 0.1,
    lookback_max_hours: float = 24,
    lookback_precision_minutes: int = 1,
    clean_window_poll_seconds: int = 15,
    clean_window_ingestion_margin_seconds: int = 30,
    clean_window_max_wait_seconds: int = 1200,
    trace_assertion_stabilization_seconds: int = 180,
    trace_hydration_grace_seconds: int = 0,
    trace_hydration_maximum_wait_seconds: int = 15 * 60,
    trace_hydration_maximum_poll_seconds: int = 15,
    insight_start_margin_seconds: int = 30,
    max_recovery_versions: int = 3,
    agent_start_stagger_seconds: int = 0,
    inter_version_pacing_seconds: int = 0,
    checkpoint_store: VersionCheckpointStore | None = None,
) -> list[AgentResult]:
    _progress(
        runtime,
        f"qualification started: {len(selected)} Agents, "
        f"{sum(len(values) for values in selected.values())} issues",
    )
    ordered = [
        execute_agent(
            agent_name=agent["name"],
            agents=agents,
            issues=issues,
            selected=selected,
            registry=registry,
            runtime=runtime,
            seed=seed,
            lookback_hours=lookback_hours,
            lookback_max_hours=lookback_max_hours,
            lookback_precision_minutes=lookback_precision_minutes,
            clean_window_poll_seconds=clean_window_poll_seconds,
            clean_window_ingestion_margin_seconds=(
                clean_window_ingestion_margin_seconds
            ),
            clean_window_max_wait_seconds=clean_window_max_wait_seconds,
            trace_assertion_stabilization_seconds=(
                trace_assertion_stabilization_seconds
            ),
            trace_hydration_grace_seconds=trace_hydration_grace_seconds,
            trace_hydration_maximum_wait_seconds=(
                trace_hydration_maximum_wait_seconds
            ),
            trace_hydration_maximum_poll_seconds=(
                trace_hydration_maximum_poll_seconds
            ),
            insight_start_margin_seconds=insight_start_margin_seconds,
            max_recovery_versions=max_recovery_versions,
            checkpoint_store=checkpoint_store,
            start_delay_seconds=index * agent_start_stagger_seconds,
            inter_version_pacing_seconds=inter_version_pacing_seconds,
        )
        for index, agent in enumerate(agents["agents"])
        if agent["name"] in selected
    ]
    _progress(runtime, "qualification runtime completed")
    return ordered


def execute_agent(
    *,
    agent_name: str,
    agents: dict[str, Any],
    issues: dict[str, Any],
    selected: dict[str, list[str]],
    registry: dict[str, Any],
    runtime: RuntimePort,
    seed: int,
    lookback_hours: float = 0.1,
    lookback_max_hours: float = 24,
    lookback_precision_minutes: int = 1,
    clean_window_poll_seconds: int = 15,
    clean_window_ingestion_margin_seconds: int = 30,
    clean_window_max_wait_seconds: int = 1200,
    trace_assertion_stabilization_seconds: int = 180,
    trace_hydration_grace_seconds: int = 0,
    trace_hydration_maximum_wait_seconds: int = 15 * 60,
    trace_hydration_maximum_poll_seconds: int = 15,
    insight_start_margin_seconds: int = 30,
    max_recovery_versions: int = 3,
    checkpoint_store: VersionCheckpointStore | None = None,
    start_delay_seconds: int = 0,
    inter_version_pacing_seconds: int = 0,
    accepted_baseline: VersionResult | None = None,
) -> AgentResult:
    agent_by_name = {item["name"]: item for item in agents["agents"]}
    issue_by_id = {item["id"]: item for item in issues["issues"]}
    if agent_name not in selected or agent_name not in agent_by_name:
        raise ContractError("Daily Agent lane assignment is invalid")
    issue_ids = selected[agent_name]
    if len(issue_ids) != len(set(issue_ids)) or not set(issue_ids).issubset(issue_by_id):
        raise ContractError("Daily Agent lane issue assignment is invalid")
    return _execute_agent(
        agent_by_name[agent_name],
        [issue_by_id[value] for value in issue_ids],
        registry,
        runtime,
        seed,
        lookback_hours,
        lookback_max_hours,
        lookback_precision_minutes,
        clean_window_poll_seconds,
        clean_window_ingestion_margin_seconds,
        clean_window_max_wait_seconds,
        trace_assertion_stabilization_seconds,
        trace_hydration_grace_seconds,
        trace_hydration_maximum_wait_seconds,
        trace_hydration_maximum_poll_seconds,
        insight_start_margin_seconds,
        _RecoveryBudget(
            max_recovery_versions,
            checkpoint_store,
            agent_name,
        ),
        checkpoint_store,
        start_delay_seconds,
        inter_version_pacing_seconds,
        accepted_baseline,
    )


def _execute_agent(
    agent: dict[str, Any],
    issue_items: list[dict[str, Any]],
    registry: dict[str, Any],
    runtime: RuntimePort,
    seed: int,
    lookback_hours: float,
    lookback_max_hours: float,
    lookback_precision_minutes: int,
    clean_window_poll_seconds: int,
    clean_window_ingestion_margin_seconds: int,
    clean_window_max_wait_seconds: int,
    trace_assertion_stabilization_seconds: int,
    trace_hydration_grace_seconds: int,
    trace_hydration_maximum_wait_seconds: int,
    trace_hydration_maximum_poll_seconds: int,
    insight_start_margin_seconds: int,
    recovery_budget: _RecoveryBudget,
    checkpoint_store: VersionCheckpointStore | None,
    start_delay_seconds: int,
    inter_version_pacing_seconds: int,
    accepted_baseline: VersionResult | None,
) -> AgentResult:
    name = agent["name"]
    start_stagger = _StartStagger(start_delay_seconds)
    monitor_reset = _MonitorReset()
    monitor_id = registry["agents"][name]["monitor_id"]
    baseline = accepted_baseline
    if baseline is not None and (
        baseline.logical_version != "v0"
        or baseline.status != "passed"
        or baseline.error_code is not None
    ):
        raise ContractError("Accepted Daily baseline recovery is invalid")
    if baseline is None:
        try:
            _progress(runtime, f"{name}/v0: started")
            started = time.monotonic()
            claim = (
                checkpoint_store.version_execution_claim(name, "v0")
                if checkpoint_store is not None
                else nullcontext()
            )
            with claim:
                baseline = _execute_version_with_recovery(
                    runtime=runtime,
                    agent=agent,
                    monitor_id=monitor_id,
                    logical_version="v0",
                    registry_entry=version_entry(registry, name, "v0"),
                    traffic_path=ROOT / agent["baseline_path"] / "traffic.json",
                    seed=seed,
                    expected=None,
                    lookback_hours=lookback_hours,
                    lookback_max_hours=lookback_max_hours,
                    lookback_precision_minutes=lookback_precision_minutes,
                    clean_window_poll_seconds=clean_window_poll_seconds,
                    clean_window_ingestion_margin_seconds=(
                        clean_window_ingestion_margin_seconds
                    ),
                    clean_window_max_wait_seconds=clean_window_max_wait_seconds,
                    trace_assertion_stabilization_seconds=(
                        trace_assertion_stabilization_seconds
                    ),
                    trace_hydration_grace_seconds=trace_hydration_grace_seconds,
                    trace_hydration_maximum_wait_seconds=(
                        trace_hydration_maximum_wait_seconds
                    ),
                    trace_hydration_maximum_poll_seconds=(
                        trace_hydration_maximum_poll_seconds
                    ),
                    insight_start_margin_seconds=insight_start_margin_seconds,
                    recovery_budget=recovery_budget,
                    checkpoint_store=checkpoint_store,
                    start_stagger=start_stagger,
                    monitor_reset=monitor_reset,
                )
            _progress(
                runtime,
                f"{name}/v0: {baseline.status} in "
                f"{time.monotonic() - started:.1f}s",
            )
        except Exception as error:
            baseline = VersionResult(
                logical_version="v0",
                foundry_version=version_entry(registry, name, "v0")[
                    "foundry_version"
                ],
                status="inconclusive",
                error_code=_stage_error_code(error),
            )
            if _telemetry_validation_error_code(baseline.error_code):
                baseline.status = "skipped_telemetry"
            elif _insight_provider_error_code(baseline.error_code):
                baseline.status = "skipped_insight"
            if (
                checkpoint_store is not None
                and checkpoint_store.has_version_progress(name, "v0")
                and _telemetry_validation_error_code(baseline.error_code)
            ):
                entry = version_entry(registry, name, "v0")
                checkpoint_store.save_result(
                    name,
                    "v0",
                    entry["foundry_version"],
                    entry["content_digest"],
                    baseline,
                )
    if baseline.status not in {
        "passed",
        "not_at_bar",
        *SKIPPED_VERSION_STATUSES,
    }:
        _progress(
            runtime,
            f"{name}/v0: incomplete ({baseline.error_code})",
        )

    results = []
    blocked_by_unaccounted_run = False
    if issue_items and inter_version_pacing_seconds:
        _progress(
            runtime,
            f"{name}: pacing {inter_version_pacing_seconds}s before next version",
        )
        time.sleep(inter_version_pacing_seconds)
    for index, issue in enumerate(issue_items, start=1):
        started = time.monotonic()
        if blocked_by_unaccounted_run:
            result = VersionResult(
                logical_version=issue["id"],
                foundry_version=version_entry(registry, name, issue["id"])[
                    "foundry_version"
                ],
                status="inconclusive",
                error_code="previous_insight_run_unaccounted",
            )
            _progress(
                runtime,
                f"{name}/{issue['id']}: inconclusive "
                "(previous_insight_run_unaccounted)",
            )
            results.append(result)
            continue
        _progress(runtime, f"{name}/{issue['id']}: started")
        try:
            claim = (
                checkpoint_store.version_execution_claim(name, issue["id"])
                if checkpoint_store is not None
                else nullcontext()
            )
            with claim:
                result = _execute_version_with_recovery(
                    runtime=runtime,
                    agent=agent,
                    monitor_id=monitor_id,
                    logical_version=issue["id"],
                    registry_entry=version_entry(registry, name, issue["id"]),
                    traffic_path=ROOT / issue["implementation"] / "traffic.json",
                    seed=seed + index,
                    expected=issue,
                    lookback_hours=lookback_hours,
                    lookback_max_hours=lookback_max_hours,
                    lookback_precision_minutes=lookback_precision_minutes,
                    clean_window_poll_seconds=clean_window_poll_seconds,
                    clean_window_ingestion_margin_seconds=(
                        clean_window_ingestion_margin_seconds
                    ),
                    clean_window_max_wait_seconds=clean_window_max_wait_seconds,
                    trace_assertion_stabilization_seconds=(
                        trace_assertion_stabilization_seconds
                    ),
                    trace_hydration_grace_seconds=trace_hydration_grace_seconds,
                    trace_hydration_maximum_wait_seconds=(
                        trace_hydration_maximum_wait_seconds
                    ),
                    trace_hydration_maximum_poll_seconds=(
                        trace_hydration_maximum_poll_seconds
                    ),
                    insight_start_margin_seconds=insight_start_margin_seconds,
                    recovery_budget=recovery_budget,
                    checkpoint_store=checkpoint_store,
                    start_stagger=start_stagger,
                    monitor_reset=monitor_reset,
                )
        except Exception as error:
            result = VersionResult(
                logical_version=issue["id"],
                foundry_version=version_entry(registry, name, issue["id"])[
                    "foundry_version"
                ],
                status="inconclusive",
                error_code=_stage_error_code(error),
            )
            if _telemetry_validation_error_code(result.error_code):
                result.status = "skipped_telemetry"
            elif _insight_provider_error_code(result.error_code):
                result.status = "skipped_insight"
            if (
                checkpoint_store is not None
                and checkpoint_store.has_version_progress(name, issue["id"])
                and _telemetry_validation_error_code(result.error_code)
            ):
                entry = version_entry(registry, name, issue["id"])
                checkpoint_store.save_result(
                    name,
                    issue["id"],
                    entry["foundry_version"],
                    entry["content_digest"],
                    result,
                )
        _progress(
            runtime,
            f"{name}/{issue['id']}: {result.status}"
            + (f" ({result.error_code})" if result.error_code else "")
            + f" in {time.monotonic() - started:.1f}s",
        )
        results.append(result)
        if checkpoint_store is not None:
            issue_registry = version_entry(registry, name, issue["id"])
            checkpoint_args = (
                name,
                issue["id"],
                issue_registry["foundry_version"],
                issue_registry["content_digest"],
            )
            blocked_by_unaccounted_run = (
                checkpoint_store.insight_drain_pending(*checkpoint_args)
                or checkpoint_store.insight_start_pending(*checkpoint_args)
            )
        if index < len(issue_items) and inter_version_pacing_seconds:
            _progress(
                runtime,
                f"{name}: pacing {inter_version_pacing_seconds}s before next version",
            )
            time.sleep(inter_version_pacing_seconds)
    return AgentResult(name, baseline, results)


def _execute_version_with_recovery(
    *,
    runtime: RuntimePort,
    agent: dict[str, Any],
    monitor_id: str,
    logical_version: str,
    registry_entry: dict[str, str],
    traffic_path: Path,
    seed: int,
    expected: dict[str, Any] | None,
    lookback_hours: float,
    lookback_max_hours: float,
    lookback_precision_minutes: int,
    clean_window_poll_seconds: int,
    clean_window_ingestion_margin_seconds: int,
    clean_window_max_wait_seconds: int,
    trace_assertion_stabilization_seconds: int,
    trace_hydration_grace_seconds: int,
    trace_hydration_maximum_wait_seconds: int,
    trace_hydration_maximum_poll_seconds: int,
    insight_start_margin_seconds: int,
    recovery_budget: _RecoveryBudget,
    checkpoint_store: VersionCheckpointStore | None,
    start_stagger: _StartStagger,
    monitor_reset: _MonitorReset,
) -> VersionResult:
    _reconcile_unresolved_insight_start(
        runtime=runtime,
        agent=agent,
        monitor_id=monitor_id,
        logical_version=logical_version,
        registry_entry=registry_entry,
        lookback_hours=lookback_hours,
        clean_window_poll_seconds=clean_window_poll_seconds,
        clean_window_ingestion_margin_seconds=(
            clean_window_ingestion_margin_seconds
        ),
        clean_window_max_wait_seconds=clean_window_max_wait_seconds,
        checkpoint_store=checkpoint_store,
    )
    while True:
        try:
            return _execute_version(
                runtime=runtime,
                agent=agent,
                monitor_id=monitor_id,
                logical_version=logical_version,
                registry_entry=registry_entry,
                traffic_path=traffic_path,
                seed=seed,
                expected=expected,
                lookback_hours=lookback_hours,
                lookback_max_hours=lookback_max_hours,
                lookback_precision_minutes=lookback_precision_minutes,
                trace_assertion_stabilization_seconds=(
                    trace_assertion_stabilization_seconds
                ),
                trace_hydration_grace_seconds=trace_hydration_grace_seconds,
                trace_hydration_maximum_wait_seconds=(
                    trace_hydration_maximum_wait_seconds
                ),
                trace_hydration_maximum_poll_seconds=(
                    trace_hydration_maximum_poll_seconds
                ),
                insight_start_margin_seconds=insight_start_margin_seconds,
                checkpoint_store=checkpoint_store,
                start_stagger=start_stagger,
                monitor_reset=monitor_reset,
            )
        except _VersionStageError as error:
            if _telemetry_validation_error_code(error.code):
                invocation = (
                    checkpoint_store.invocation(
                        agent["name"],
                        logical_version,
                        registry_entry["foundry_version"],
                        registry_entry["content_digest"],
                    )
                    if checkpoint_store is not None
                    else None
                )
                if invocation is None:
                    raise
                result = VersionResult(
                    logical_version=logical_version,
                    foundry_version=registry_entry["foundry_version"],
                    status="skipped_telemetry",
                    operation_ids=list(
                        checkpoint_store.operation_ids(
                            agent["name"],
                            logical_version,
                            registry_entry["foundry_version"],
                            registry_entry["content_digest"],
                        )
                        or ()
                    ),
                    window_start=invocation.started_at,
                    window_end=invocation.completed_at,
                    error_code=error.code,
                    endpoint_request_count=invocation.request_count,
                    endpoint_response_count=invocation.response_count,
                    endpoint_usable_response_count=(
                        invocation.usable_response_count
                    ),
                    semantic_assertion_count=invocation.semantic_assertion_count,
                    semantic_assertions_passed=(
                        invocation.semantic_assertions_passed
                    ),
                    endpoint_request_summaries=list(
                        invocation.request_summaries
                    ),
                )
                checkpoint_store.save_result(
                    agent["name"],
                    logical_version,
                    registry_entry["foundry_version"],
                    registry_entry["content_digest"],
                    result,
                )
                return result
            if (
                checkpoint_store is None
                or not _recoverable(error)
                or not recovery_budget.claim()
            ):
                quarantined = _quarantine_started_insight(
                    runtime=runtime,
                    agent=agent,
                    monitor_id=monitor_id,
                    logical_version=logical_version,
                    registry_entry=registry_entry,
                    error=error,
                    traffic_path=traffic_path,
                    checkpoint_store=checkpoint_store,
                )
                if quarantined is not None:
                    return quarantined
                raise
            _progress(
                runtime,
                f"{agent['name']}/{logical_version}: recovering {error.code}",
            )
            if error.code.startswith("insight_run_terminal_failed"):
                checkpoint_store.clear_insight_run(
                    agent["name"],
                    logical_version,
                    registry_entry["foundry_version"],
                    registry_entry["content_digest"],
                )


def _recoverable(error: _VersionStageError) -> bool:
    if error.code == "insight_window_expired":
        return False
    if error.code.endswith("_credential"):
        return False
    match = re.search(r"_http_([0-9]{3})$", error.code)
    if match:
        status = int(match.group(1))
        return status in {408, 424, 429} or 500 <= status <= 599
    if error.code == "invocation_failed":
        return False
    if error.code == "insight_run_start_failed":
        return False
    return error.code.startswith(
        (
            "insight_run_poll_failed",
            "insight_run_terminal_failed",
        )
    )


def _validate_endpoint_contract(
    *,
    agent: dict[str, Any],
    logical_version: str,
    baseline: bool,
    invocation: InvocationEvidence,
    traffic_path: Path,
) -> None:
    context = execution_context(traffic_path)
    expected_requests = len(daily_issue_side_requests(traffic_path))
    if invocation.request_count != expected_requests:
        raise _VersionStageError(
            "endpoint_contract_failed",
            ContractError(
                f"{agent['name']}/{logical_version} expected "
                f"{expected_requests} endpoint requests"
            ),
        )
    if (
        not 0 <= invocation.usable_response_count <= invocation.response_count
        <= invocation.request_count
    ):
        raise _VersionStageError(
            "endpoint_contract_failed",
            ContractError(
                f"{agent['name']}/{logical_version} endpoint evidence counts "
                "are inconsistent"
            ),
        )
    summaries = invocation.request_summaries
    if len(summaries) != invocation.request_count or [
        item.request_index for item in summaries
    ] != list(range(invocation.request_count)):
        raise _VersionStageError(
            "endpoint_contract_failed",
            ContractError(
                f"{agent['name']}/{logical_version} request summaries are incomplete"
            ),
        )
    if (
        sum(item.response_count for item in summaries)
        != invocation.response_count
        or sum(item.usable_response for item in summaries)
        != invocation.usable_response_count
    ):
        raise _VersionStageError(
            "endpoint_contract_failed",
            ContractError(
                f"{agent['name']}/{logical_version} endpoint summary counts "
                "are inconsistent"
            ),
        )
    observations = [item for item in summaries if item.activation_gate]
    if len(observations) != int(context["n"]):
        raise _VersionStageError(
            "endpoint_contract_failed",
            ContractError(
                f"{agent['name']}/{logical_version} expected "
                f"{context['n']} reviewed observations"
            ),
        )
    if any(
        len(item.assertion_results) != item.semantic_assertion_count
        or sum(result.passed for result in item.assertion_results)
        != item.semantic_assertions_passed
        for item in summaries
    ):
        raise _VersionStageError(
            "endpoint_contract_failed",
            ContractError(
                f"{agent['name']}/{logical_version} assertion results are incomplete"
            ),
        )
    if agent["type"] == "prompt" and any(
        item.function_call_count != 0 for item in summaries
    ):
        raise _VersionStageError(
            "endpoint_contract_failed",
            ContractError(
                f"{agent['name']}/{logical_version} violated the direct Prompt "
                "response contract"
            ),
        )
def _with_trace_assertions(
    invocation: InvocationEvidence,
    results: tuple[tuple[Any, ...], ...],
) -> InvocationEvidence:
    if len(results) != invocation.request_count:
        raise ContractError("Trace assertion request coverage is incomplete")
    summaries = tuple(
        replace(
            summary,
            trace_assertion_count=len(assertions),
            trace_assertions_passed=sum(item.passed for item in assertions),
            trace_assertion_results=assertions,
            error_code=(
                "assertion_failed"
                if any(
                    item.evidence_sufficient and not item.passed
                    for item in assertions
                )
                else "missing_evidence"
                if any(
                    not item.evidence_sufficient
                    for item in assertions
                )
                else None
            ),
        )
        for summary, assertions in zip(
            invocation.request_summaries,
            results,
            strict=True,
        )
    )
    return replace(
        invocation,
        trace_assertion_count=sum(item.trace_assertion_count for item in summaries),
        trace_assertions_passed=sum(
            item.trace_assertions_passed for item in summaries
        ),
        request_summaries=summaries,
    )


def _issue_activation_evidence_complete(
    context: dict[str, Any],
    invocation: InvocationEvidence,
) -> bool:
    complete, _ = _issue_activation_decision(context, invocation)
    return complete


def _issue_activation_decision(
    context: dict[str, Any],
    invocation: InvocationEvidence,
    *,
    direct_prompt_contract: bool = False,
) -> tuple[bool, dict[str, Any] | None]:
    gates = [item for item in invocation.request_summaries if item.activation_gate]
    return daily_target_decision(
        target_role="issue",
        validation_mode=str(context["validation_mode"]),
        n=int(context["n"]),
        k=int(context["k"]),
        required_surfaces=context["required_surfaces"],
        summaries=[
            request_completion_payload(item)
            for item in gates
        ],
        identity_verified=(
            len(invocation.response_references)
            == invocation.request_count
            and invocation.allow_window_correlation is False
        ),
        direct_prompt_contract=direct_prompt_contract,
    )


def _baseline_validation_decision(
    invocation: InvocationEvidence,
    *,
    direct_prompt_contract: bool = False,
) -> tuple[bool, dict[str, Any] | None]:
    n, k = validation_matrix("baseline")
    gates = [item for item in invocation.request_summaries if item.activation_gate]
    return daily_target_decision(
        target_role="baseline",
        validation_mode="baseline",
        n=n,
        k=k,
        required_surfaces=["semantic", "trace"],
        summaries=[request_completion_payload(item) for item in gates],
        identity_verified=(
            len(invocation.response_references) == invocation.request_count
            and invocation.allow_window_correlation is False
        ),
        direct_prompt_contract=direct_prompt_contract,
    )


def _daily_trace_maturity_proof_digest(
    trace_maturity_proof: dict[str, Any] | None,
) -> str | None:
    return validate_trace_maturity_proof(trace_maturity_proof)


def _role_pass_has_incomplete_misses(
    summary: dict[str, Any] | None,
) -> bool:
    if summary is None:
        return True
    miss_counts = summary.get("miss_counts")
    if not isinstance(miss_counts, dict):
        return True
    return any(
        int(count) > 0
        for category, count in miss_counts.items()
        if category != "complete_non_pass"
    )


def _issue_execution_context(traffic_path: Path) -> dict[str, Any]:
    return issue_observation_context(traffic_path)


def _minimum_passing_trace_observations(
    requests: list[dict[str, Any]],
    issue_context: dict[str, Any] | None,
) -> int:
    if issue_context is not None:
        return (
            int(issue_context["k"])
            if "trace" in issue_context["required_surfaces"]
            else 0
        )
    traced_observations = sum(
        request["expected"].get("activation_gate") is True
        and bool(request["expected"].get("trace_assertions"))
        for request in requests
    )
    return min(validation_matrix("baseline")[1], traced_observations)


def _validate_cached_execution(
    result: VersionResult,
    requests: list[dict[str, Any]],
    context: dict[str, Any],
) -> None:
    expected_gates = [
        request["expected"].get("activation_gate") is True
        for request in requests
    ]
    actual_gates = [
        summary.activation_gate for summary in result.endpoint_request_summaries
    ]
    if (
        result.endpoint_request_count != len(requests)
        or len(actual_gates) != len(expected_gates)
        or actual_gates != expected_gates
        or sum(actual_gates) != int(context["n"])
    ):
        raise ContractError(
            "Cached Daily evidence does not match the current execution plan"
        )


def _issue_semantic_activation_evidence_complete(
    context: dict[str, Any],
    invocation: InvocationEvidence,
) -> bool:
    if "semantic" not in context["required_surfaces"]:
        return True
    gates = [item for item in invocation.request_summaries if item.activation_gate]
    return len(gates) == int(context["n"]) and sum(
        item.semantic_assertion_count > 0
        and item.semantic_assertions_passed == item.semantic_assertion_count
        for item in gates
    ) >= int(context["k"])


def _validate_baseline_trace_evidence(
    *,
    agent: dict[str, Any],
    invocation: InvocationEvidence,
    trace_evidence: dict[str, Any],
    role_pass_count: int,
) -> None:
    required_count = role_pass_count
    terminal_mode = agent["baseline_contract"]["terminal_response"]
    terminal_complete = (
        int(trace_evidence.get("terminal_response_count") or 0) >= required_count
        and int(trace_evidence.get("terminal_output_count") or 0) >= required_count
    )
    if terminal_mode == "explicit_span_attributes":
        terminal_complete = terminal_complete and (
            int(trace_evidence.get("explicit_terminal_success_count") or 0)
            >= required_count
            and int(trace_evidence.get("explicit_terminal_output_count") or 0)
            >= required_count
        )
    else:
        terminal_complete = terminal_complete and (
            int(trace_evidence.get("assistant_response_count") or 0)
            >= required_count
        )
    if not terminal_complete:
        raise ContractError(
            f"{agent['name']} baseline lacks one successful terminal output "
            "signal per request"
        )
def _activation_failure_result(
    *,
    logical_version: str,
    foundry_version: str,
    operation_ids: tuple[str, ...],
    invocation: InvocationEvidence,
    trace_evidence: dict[str, Any],
    error_code: str = "issue_activation_failed",
    trace_contract_verified: bool = True,
    trace_maturity_proof: dict[str, Any] | None = None,
    role_pass_summary: dict[str, Any] | None = None,
) -> VersionResult:
    return VersionResult(
        logical_version=logical_version,
        foundry_version=foundry_version,
        status="inconclusive",
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
        trace_contract_verified=trace_contract_verified,
        trace_behavior_summary=trace_evidence,
        trace_maturity_proof=trace_maturity_proof,
        role_pass_summary=role_pass_summary,
        endpoint_request_summaries=list(invocation.request_summaries),
    )


def _baseline_recovery_is_safe(
    result: VersionResult,
    invocation: InvocationEvidence | None,
) -> bool:
    return (
        result.status == "inconclusive"
        and result.error_code == "baseline_evidence_incomplete"
        and _baseline_evidence_is_strict(result, invocation)
    )


def _baseline_evidence_is_strict(
    result: VersionResult,
    invocation: InvocationEvidence | None,
) -> bool:
    summaries = result.endpoint_request_summaries
    return (
        invocation is not None
        and result.endpoint_request_count > 0
        and result.endpoint_request_count
        == result.endpoint_response_count
        == result.endpoint_usable_response_count
        == len(result.operation_ids)
        == len(summaries)
        == invocation.request_count
        == invocation.response_count
        == invocation.usable_response_count
        == len(invocation.response_references)
        and len(set(invocation.response_references))
        == len(invocation.response_references)
        and all(invocation.response_references)
        and all(
            item.response_count == 1
            and item.usable_response
            and item.semantic_assertions_passed == item.semantic_assertion_count
            and item.trace_assertions_passed == item.trace_assertion_count
            and all(
                assertion.passed and assertion.evidence_sufficient
                for assertion in item.assertion_results
            )
            and all(
                assertion.passed and assertion.evidence_sufficient
                for assertion in item.trace_assertion_results
            )
            for item in summaries
        )
        and result.trace_contract_verified
        and result.trace_assertions_passed == result.trace_assertion_count
        and int(result.trace_behavior_summary.get("unhandled_error_count") or 0) == 0
    )


def _save_and_drain_rejected_insight_run(
    *,
    runtime: RuntimePort,
    agent_name: str,
    monitor_id: str,
    foundry_version: str,
    operation_ids: tuple[str, ...],
    checkpoint: InsightRunCheckpoint | None,
    checkpoint_store: VersionCheckpointStore | None,
    checkpoint_args: tuple[str, str, str, str],
    result: VersionResult,
    reason: str,
) -> None:
    if checkpoint_store is not None:
        checkpoint_store.save_rejected_result(
            *checkpoint_args,
            result,
            drain_pending=checkpoint is not None,
        )
    if checkpoint is None:
        return
    _drain_rejected_insight_run(
        runtime=runtime,
        agent_name=agent_name,
        monitor_id=monitor_id,
        foundry_version=foundry_version,
        operation_ids=operation_ids,
        checkpoint=checkpoint,
        checkpoint_store=checkpoint_store,
        checkpoint_args=checkpoint_args,
        result=result,
        reason=reason,
    )


def _drain_rejected_insight_run(
    *,
    runtime: RuntimePort,
    agent_name: str,
    monitor_id: str,
    foundry_version: str,
    operation_ids: tuple[str, ...],
    checkpoint: InsightRunCheckpoint,
    checkpoint_store: VersionCheckpointStore | None,
    checkpoint_args: tuple[str, str, str, str],
    result: VersionResult,
    reason: str,
) -> None:
    _progress(
        runtime,
        f"{agent_name}/{result.logical_version}: evidence {reason}; "
        "discarding the persisted Agent Insights run result",
    )
    try:
        runtime.finish_insights_run(
            agent_name=agent_name,
            monitor_id=monitor_id,
            foundry_version=foundry_version,
            operation_ids=operation_ids,
            checkpoint=checkpoint,
            validate_window=False,
        )
    except Exception:
        _progress(
            runtime,
            f"{agent_name}/{result.logical_version}: persisted Agent Insights run "
            "remains quarantined by the saved incomplete evidence",
        )
        return
    if checkpoint_store is not None:
        checkpoint_store.clear_insight_drain_pending(*checkpoint_args)


def _execute_version(
    *,
    runtime: RuntimePort,
    agent: dict[str, Any],
    monitor_id: str,
    logical_version: str,
    registry_entry: dict[str, str],
    traffic_path: Path,
    seed: int,
    expected: dict[str, Any] | None,
    lookback_hours: float,
    lookback_max_hours: float,
    lookback_precision_minutes: int,
    trace_assertion_stabilization_seconds: int,
    trace_hydration_grace_seconds: int,
    trace_hydration_maximum_wait_seconds: int,
    trace_hydration_maximum_poll_seconds: int,
    insight_start_margin_seconds: int,
    checkpoint_store: VersionCheckpointStore | None,
    start_stagger: _StartStagger,
    monitor_reset: _MonitorReset,
) -> VersionResult:
    foundry_version = registry_entry["foundry_version"]
    planned_requests = daily_issue_side_requests(traffic_path)
    issue_context = (
        _issue_execution_context(traffic_path) if expected is not None else None
    )
    checkpoint_args = (
        agent["name"],
        logical_version,
        foundry_version,
        registry_entry["content_digest"],
    )
    cached = (
        checkpoint_store.result(*checkpoint_args)
        if checkpoint_store is not None
        else None
    )
    if cached is not None:
        _validate_cached_execution(
            cached,
            planned_requests,
            issue_context or execution_context(traffic_path),
        )
        if (
            checkpoint_store is not None
            and checkpoint_store.insight_drain_pending(*checkpoint_args)
        ):
            checkpoint = checkpoint_store.insight_run(*checkpoint_args)
            if checkpoint is None:
                raise ContractError(
                    "Rejected Insight run checkpoint is missing while drain is pending"
                )
            _drain_rejected_insight_run(
                runtime=runtime,
                agent_name=agent["name"],
                monitor_id=monitor_id,
                foundry_version=foundry_version,
                operation_ids=tuple(cached.operation_ids),
                checkpoint=checkpoint,
                checkpoint_store=checkpoint_store,
                checkpoint_args=checkpoint_args,
                result=cached,
                reason="was previously rejected",
            )
        return cached
    invocation = (
        checkpoint_store.invocation(*checkpoint_args)
        if checkpoint_store is not None
        else None
    )
    if invocation is None:
        start_stagger.wait_once(runtime, agent["name"])
        try:
            invocation = runtime.invoke_version(
                agent_name=agent["name"],
                agent_type=agent["type"],
                foundry_version=foundry_version,
                traffic_path=traffic_path,
                seed=seed,
                requests=planned_requests,
            )
        except Exception as error:
            raise _VersionStageError("invocation_failed", error) from error
        if checkpoint_store is not None:
            checkpoint_store.save_invocation(*checkpoint_args, invocation)
    _progress(
        runtime,
        f"{agent['name']}/{logical_version}: endpoint complete "
        f"({invocation.response_count}/{invocation.request_count} responses)",
    )
    _validate_endpoint_contract(
        agent=agent,
        logical_version=logical_version,
        baseline=expected is None,
        invocation=invocation,
        traffic_path=traffic_path,
    )
    operation_ids = (
        checkpoint_store.operation_ids(*checkpoint_args)
        if checkpoint_store is not None
        else None
    )
    if operation_ids is None:
        try:
            operation_ids = runtime.wait_for_telemetry(
                agent_name=agent["name"],
                foundry_version=foundry_version,
                invocation=invocation,
                poll_seconds=15,
                maximum_wait_seconds=trace_hydration_maximum_wait_seconds,
                minimum_grace_seconds=trace_hydration_grace_seconds,
                maximum_poll_seconds=trace_hydration_maximum_poll_seconds,
                age_bounded=True,
            )
        except Exception as error:
            raise _VersionStageError("telemetry_failed", error) from error
        if checkpoint_store is not None:
            checkpoint_store.save_operation_ids(*checkpoint_args, operation_ids)
    _progress(
        runtime,
        f"{agent['name']}/{logical_version}: telemetry correlated "
        f"({len(operation_ids)} operations)",
    )
    insight_checkpoint = (
        checkpoint_store.insight_run(*checkpoint_args)
        if checkpoint_store is not None
        else None
    )
    if (
        insight_checkpoint is None
        and checkpoint_store is not None
        and checkpoint_store.insight_start_pending(*checkpoint_args)
    ):
        raise _VersionStageError(
            "insight_run_start_failed",
            RuntimeError("Remote operation failed before a response was received"),
        )

    def start_insight_run_once() -> None:
        nonlocal insight_checkpoint
        if insight_checkpoint is not None:
            return
        if (
            expected is not None
            and not _issue_semantic_activation_evidence_complete(
                issue_context,
                invocation,
            )
        ):
            return
        try:
            monitor_reset.ensure(
                runtime,
                agent_name=agent["name"],
                monitor_id=monitor_id,
                checkpoint_store=checkpoint_store,
            )
        except Exception as error:
            raise _VersionStageError(
                "insight_monitor_reset_failed",
                error,
            ) from error
        if checkpoint_store is not None:
            checkpoint_store.mark_insight_start_pending(*checkpoint_args)
            intent_reference = str(
                checkpoint_store.insight_start_outcome(*checkpoint_args)[
                    "intent_digest"
                ]
            )
        else:
            intent_reference = content_hash(
                {
                    "agent_name": agent["name"],
                    "foundry_version": foundry_version,
                    "operation_ids": list(operation_ids),
                }
            )
        try:
            effective_lookback = (
                checkpoint_store.insight_lookback(*checkpoint_args)
                if checkpoint_store is not None
                else None
            ) or _version_insight_lookback(
                runtime,
                invocation,
                minimum_hours=lookback_hours,
                maximum_hours=lookback_max_hours,
                precision_minutes=lookback_precision_minutes,
                margin_seconds=insight_start_margin_seconds,
            )
            if checkpoint_store is not None:
                checkpoint_store.save_insight_lookback(
                    *checkpoint_args,
                    effective_lookback,
                )
            insight_checkpoint = runtime.start_insights_run(
                agent_name=agent["name"],
                monitor_id=monitor_id,
                foundry_version=foundry_version,
                operation_ids=operation_ids,
                lookback_hours=effective_lookback["lookback_hours"],
                start_margin_seconds=insight_start_margin_seconds,
                intent_reference=intent_reference,
                persist=(
                    lambda checkpoint: checkpoint_store.save_insight_run(
                        *checkpoint_args,
                        checkpoint,
                    )
                    if checkpoint_store is not None
                    else None
                ),
            )
        except InsightWindowExpiredError as error:
            if checkpoint_store is not None:
                checkpoint_store.record_insight_start_outcome(
                    *checkpoint_args,
                    status="explicit_no_run",
                )
            if expected is not None:
                raise _VersionStageError(
                    "issue_activation_failed",
                    ContractError(
                        "First exact Hosted mapping arrived after the guarded "
                        "Agent Insights start window"
                    ),
                ) from error
            raise _VersionStageError("insight_run_start_failed", error) from error
        except Exception as error:
            if checkpoint_store is not None:
                persisted = checkpoint_store.insight_run(*checkpoint_args)
                if persisted is not None:
                    insight_checkpoint = persisted
                    return
                checkpoint_store.record_insight_start_outcome(
                    *checkpoint_args,
                    status="unknown",
                )
            raise _VersionStageError("insight_run_start_failed", error) from error

    required_operations_by_request = _required_trace_operations(
        agent=agent,
        expected=expected,
        traffic_path=traffic_path,
        request_count=invocation.request_count,
    )
    trace_verified = (
        checkpoint_store.trace_verified(*checkpoint_args)
        if checkpoint_store is not None
        else False
    )
    if not trace_verified:
        try:
            runtime.verify_trace_contract(
                agent_name=agent["name"],
                foundry_version=foundry_version,
                operation_ids=operation_ids,
                required_operations_by_request=required_operations_by_request,
                window_start=invocation.started_at,
                window_end=invocation.completed_at,
                poll_seconds=15,
                maximum_wait_seconds=trace_hydration_maximum_wait_seconds,
                maximum_poll_seconds=trace_hydration_maximum_poll_seconds,
                age_bounded=True,
            )
        except Exception as error:
            raise _VersionStageError("trace_contract_failed", error) from error
        if checkpoint_store is not None:
            checkpoint_store.save_trace_verified(*checkpoint_args)
    _progress(runtime, f"{agent['name']}/{logical_version}: trace contract verified")
    trace_evidence: dict[str, Any] = {}
    trace_maturity_proof: dict[str, Any] | None = None
    if agent["type"] == "prompt":
        try:
            trace_evidence = runtime.trace_behavior_evidence(operation_ids)
        except Exception as error:
            if expected is None:
                raise _VersionStageError("baseline_evidence_failed", error) from error
            raise _VersionStageError("trace_evidence_failed", error) from error

    if agent["type"] != "prompt":
        evidence_error_code = (
            "issue_activation_failed"
            if expected is not None
            else "baseline_evidence_failed"
        )
        stabilization_error = False

        def capture_stable_trace_evidence(evidence: dict[str, Any]) -> None:
            nonlocal trace_evidence
            trace_evidence = evidence

        def capture_trace_maturity_proof(proof: dict[str, Any]) -> None:
            nonlocal trace_maturity_proof
            trace_maturity_proof = proof

        try:
            invocation = _with_trace_assertions(
                invocation,
                runtime.trace_assertion_evidence(
                    agent_name=agent["name"],
                    foundry_version=foundry_version,
                    operation_ids=operation_ids,
                    response_references=invocation.response_references,
                    window_start=invocation.started_at,
                    window_end=invocation.completed_at,
                    traffic_path=traffic_path,
                    requests=planned_requests,
                    stabilization_seconds=trace_assertion_stabilization_seconds,
                    on_first_pass=lambda: None,
                    minimum_passing_trace_observations=(
                        _minimum_passing_trace_observations(
                            planned_requests,
                            issue_context,
                        )
                    ),
                    on_stable=capture_stable_trace_evidence,
                    on_maturity_proof=capture_trace_maturity_proof,
                    poll_seconds=15,
                    maximum_wait_seconds=trace_hydration_maximum_wait_seconds,
                    minimum_grace_seconds=trace_hydration_grace_seconds,
                    maximum_poll_seconds=trace_hydration_maximum_poll_seconds,
                    age_bounded=True,
                ),
            )
        except _VersionStageError as error:
            if error.code != "issue_activation_failed":
                raise
            stabilization_error = True
        except TraceAssertionActivationError:
            stabilization_error = True
        except Exception as error:
            raise _VersionStageError("trace_assertion_failed", error) from error
        if stabilization_error:
            result = _activation_failure_result(
                logical_version=logical_version,
                foundry_version=foundry_version,
                operation_ids=operation_ids,
                invocation=invocation,
                trace_evidence=trace_evidence,
                error_code=evidence_error_code,
            )
            result.status = "skipped_telemetry"
            result.error_code = "trace_assertion_failed_mature"
            _save_and_drain_rejected_insight_run(
                runtime=runtime,
                agent_name=agent["name"],
                monitor_id=monitor_id,
                foundry_version=foundry_version,
                operation_ids=operation_ids,
                checkpoint=insight_checkpoint,
                checkpoint_store=checkpoint_store,
                checkpoint_args=checkpoint_args,
                result=result,
                reason="did not stabilize under exact Hosted correlation",
            )
            return result
        if checkpoint_store is not None:
            checkpoint_store.save_invocation(*checkpoint_args, invocation)

    if expected is not None:
        activation_complete, role_pass_summary = (
            _issue_activation_decision(
                issue_context,
                invocation,
                direct_prompt_contract=agent["type"] == "prompt",
            )
        )
        if not activation_complete:
            result = _activation_failure_result(
                logical_version=logical_version,
                foundry_version=foundry_version,
                operation_ids=operation_ids,
                invocation=invocation,
                trace_evidence=trace_evidence,
                trace_maturity_proof=trace_maturity_proof,
                role_pass_summary=role_pass_summary,
            )
            if not _role_pass_has_incomplete_misses(role_pass_summary):
                result.status = "skipped_agent_activation"
                result.error_code = "agent_activation_below_threshold"
            elif trace_maturity_proof is not None:
                result.status = "skipped_telemetry"
                result.error_code = "trace_assertion_failed_mature"
            _save_and_drain_rejected_insight_run(
                runtime=runtime,
                agent_name=agent["name"],
                monitor_id=monitor_id,
                foundry_version=foundry_version,
                operation_ids=operation_ids,
                checkpoint=insight_checkpoint,
                checkpoint_store=checkpoint_store,
                checkpoint_args=checkpoint_args,
                result=result,
                reason="required observation surfaces did not reach the reviewed threshold",
            )
            return result
    else:
        baseline_complete, role_pass_summary = (
            _baseline_validation_decision(
                invocation,
                direct_prompt_contract=agent["type"] == "prompt",
            )
        )
        try:
            _validate_baseline_trace_evidence(
                agent=agent,
                invocation=invocation,
                trace_evidence=trace_evidence,
                role_pass_count=(
                    int(role_pass_summary["pass_count"])
                    if role_pass_summary is not None
                    else 0
                ),
            )
        except Exception as error:
            baseline_complete = False
            stage_error = _VersionStageError("baseline_evidence_failed", error)
            if _recoverable(stage_error):
                raise stage_error from error
        if not baseline_complete:
            result = _activation_failure_result(
                logical_version=logical_version,
                foundry_version=foundry_version,
                operation_ids=operation_ids,
                invocation=invocation,
                trace_evidence=trace_evidence,
                error_code="baseline_evidence_failed",
                trace_maturity_proof=trace_maturity_proof,
                role_pass_summary=role_pass_summary,
            )
            if not _role_pass_has_incomplete_misses(role_pass_summary):
                result.status = "skipped_agent_activation"
                result.error_code = "agent_activation_below_threshold"
            elif trace_maturity_proof is not None:
                result.status = "skipped_telemetry"
                result.error_code = "trace_assertion_failed_mature"
            _save_and_drain_rejected_insight_run(
                runtime=runtime,
                agent_name=agent["name"],
                monitor_id=monitor_id,
                foundry_version=foundry_version,
                operation_ids=operation_ids,
                checkpoint=insight_checkpoint,
                checkpoint_store=checkpoint_store,
                checkpoint_args=checkpoint_args,
                result=result,
                reason="baseline evidence did not meet the reviewed threshold",
            )
            return result
    if insight_checkpoint is None:
        start_insight_run_once()
    assert insight_checkpoint is not None
    try:
        insight_run = runtime.finish_insights_run(
            agent_name=agent["name"],
            monitor_id=monitor_id,
            foundry_version=foundry_version,
            operation_ids=operation_ids,
            checkpoint=insight_checkpoint,
        )
    except Exception as error:
        raise _VersionStageError("insight_run_poll_failed", error) from error
    scoped_insights = tuple(
        item
        for item in insight_run.insights
        if item.agent_version == foundry_version
        and linked_operations_match_scope(
            item.linked_operation_ids,
            operation_ids,
        )
    )
    _progress(
        runtime,
        f"{agent['name']}/{logical_version}: Agent Insights "
        f"{insight_run.status.lower()} ({len(scoped_insights)} cards)",
    )
    result = VersionResult(
        logical_version=logical_version,
        foundry_version=foundry_version,
        status="inconclusive",
        operation_ids=list(operation_ids),
        insight_references=[item.reference for item in scoped_insights],
        window_start=insight_run.window_start,
        window_end=insight_run.window_end,
        observed_insights=list(scoped_insights),
        endpoint_request_count=invocation.request_count,
        endpoint_response_count=invocation.response_count,
        endpoint_usable_response_count=invocation.usable_response_count,
        semantic_assertion_count=invocation.semantic_assertion_count,
        semantic_assertions_passed=invocation.semantic_assertions_passed,
        trace_assertion_count=invocation.trace_assertion_count,
        trace_assertions_passed=invocation.trace_assertions_passed,
        trace_contract_verified=True,
        trace_behavior_summary=trace_evidence,
        trace_maturity_proof=trace_maturity_proof,
        role_pass_summary=role_pass_summary,
        endpoint_request_summaries=list(invocation.request_summaries),
    )
    if insight_run.status != "succeeded":
        raise _VersionStageError(
            "insight_run_terminal_failed",
            RuntimeError("Agent Insights run did not succeed"),
        )
    if expected is None:
        result.status = "passed" if not scoped_insights else "not_at_bar"
        if checkpoint_store is not None:
            checkpoint_store.save_result(*checkpoint_args, result)
        return result
    matching = list(scoped_insights)
    if not matching:
        result.status = "not_at_bar"
        result.error_code = "missing_insight"
        if checkpoint_store is not None:
            checkpoint_store.save_result(*checkpoint_args, result)
        return result
    result.status = "observed"
    result.observed_insight = matching[0] if len(matching) == 1 else None
    if checkpoint_store is not None:
        checkpoint_store.save_result(*checkpoint_args, result)
    return result


def _quarantine_started_insight(
    *,
    runtime: RuntimePort,
    agent: dict[str, Any],
    monitor_id: str,
    logical_version: str,
    registry_entry: dict[str, str],
    error: _VersionStageError,
    traffic_path: Path,
    checkpoint_store: VersionCheckpointStore | None,
) -> VersionResult | None:
    if checkpoint_store is None:
        return None
    checkpoint_args = (
        agent["name"],
        logical_version,
        registry_entry["foundry_version"],
        registry_entry["content_digest"],
    )
    insight_checkpoint = checkpoint_store.insight_run(*checkpoint_args)
    invocation = checkpoint_store.invocation(*checkpoint_args)
    operation_ids = checkpoint_store.operation_ids(*checkpoint_args)
    if invocation is None or operation_ids is None:
        return None
    role_pass_summary = None
    if logical_version != "v0":
        context = _issue_execution_context(traffic_path)
        _, role_pass_summary = _issue_activation_decision(
            context,
            invocation,
            direct_prompt_contract=agent["type"] == "prompt",
        )
    else:
        _, role_pass_summary = _baseline_validation_decision(
            invocation,
            direct_prompt_contract=agent["type"] == "prompt",
        )
    if insight_checkpoint is None:
        if not checkpoint_store.insight_start_pending(*checkpoint_args):
            result = _activation_failure_result(
                logical_version=logical_version,
                foundry_version=registry_entry["foundry_version"],
                operation_ids=operation_ids,
                invocation=invocation,
                trace_evidence={},
                trace_contract_verified=checkpoint_store.trace_verified(
                    *checkpoint_args
                ),
                role_pass_summary=role_pass_summary,
            )
            result.status = "skipped_insight"
            result.error_code = error.code
            checkpoint_store.save_result(*checkpoint_args, result)
            return result
        result = _activation_failure_result(
            logical_version=logical_version,
            foundry_version=registry_entry["foundry_version"],
            operation_ids=operation_ids,
            invocation=invocation,
            trace_evidence={},
            trace_contract_verified=checkpoint_store.trace_verified(*checkpoint_args),
            role_pass_summary=role_pass_summary,
        )
        result.error_code = "insight_run_start_unresolved"
        checkpoint_store.save_result(*checkpoint_args, result)
        return result
    result = _activation_failure_result(
        logical_version=logical_version,
        foundry_version=registry_entry["foundry_version"],
        operation_ids=operation_ids,
        invocation=invocation,
        trace_evidence={},
        trace_contract_verified=checkpoint_store.trace_verified(*checkpoint_args),
        role_pass_summary=role_pass_summary,
    )
    result.status = "skipped_insight"
    result.error_code = error.code
    _save_and_drain_rejected_insight_run(
        runtime=runtime,
        agent_name=agent["name"],
        monitor_id=monitor_id,
        foundry_version=registry_entry["foundry_version"],
        operation_ids=operation_ids,
        checkpoint=insight_checkpoint,
        checkpoint_store=checkpoint_store,
        checkpoint_args=checkpoint_args,
        result=result,
        reason="could not complete within the recovery budget",
    )
    return result


def _reconcile_unresolved_insight_start(
    *,
    runtime: RuntimePort,
    agent: dict[str, Any],
    monitor_id: str,
    logical_version: str,
    registry_entry: dict[str, str],
    lookback_hours: float,
    clean_window_poll_seconds: int,
    clean_window_ingestion_margin_seconds: int,
    clean_window_max_wait_seconds: int,
    checkpoint_store: VersionCheckpointStore | None,
) -> None:
    if checkpoint_store is None:
        return
    checkpoint_args = (
        agent["name"],
        logical_version,
        registry_entry["foundry_version"],
        registry_entry["content_digest"],
    )
    if (
        not checkpoint_store.insight_start_pending(*checkpoint_args)
        or checkpoint_store.insight_run(*checkpoint_args) is not None
        or checkpoint_store.result(*checkpoint_args) is None
    ):
        return
    _progress(
        runtime,
        f"{agent['name']}/{logical_version}: reconciling unresolved "
        "Agent Insights start",
    )
    operation_ids = checkpoint_store.operation_ids(*checkpoint_args) or ()
    discovery, discovered = runtime.discover_insights_run(
        agent_name=agent["name"],
        monitor_id=monitor_id,
        foundry_version=registry_entry["foundry_version"],
        operation_ids=operation_ids,
    )
    if discovery == "matched" and discovered is not None:
        checkpoint_store.save_insight_run(*checkpoint_args, discovered)
        return
    if discovery != "absent":
        checkpoint_store.record_insight_start_outcome(
            *checkpoint_args,
            status="unknown",
        )
        raise _VersionStageError(
            "insight_run_start_unresolved",
            ContractError("Agent Insights start discovery is ambiguous"),
        )
    runtime.wait_for_clean_window(
        agent["name"],
        lookback_hours,
        poll_seconds=clean_window_poll_seconds,
        ingestion_margin_seconds=clean_window_ingestion_margin_seconds,
        max_wait_seconds=clean_window_max_wait_seconds,
    )
    repeated, discovered = runtime.discover_insights_run(
        agent_name=agent["name"],
        monitor_id=monitor_id,
        foundry_version=registry_entry["foundry_version"],
        operation_ids=operation_ids,
    )
    if repeated == "matched" and discovered is not None:
        checkpoint_store.save_insight_run(*checkpoint_args, discovered)
        return
    if repeated != "absent":
        checkpoint_store.record_insight_start_outcome(
            *checkpoint_args,
            status="unknown",
        )
        raise _VersionStageError(
            "insight_run_start_unresolved",
            ContractError("Agent Insights start discovery became ambiguous"),
        )
    checkpoint_store.record_insight_start_outcome(
        *checkpoint_args,
        status="explicit_no_run",
    )
    checkpoint_store.prepare_insight_start_retry(*checkpoint_args)
