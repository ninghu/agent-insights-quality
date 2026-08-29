from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Protocol

from agent_insights_quality.automation_policy import (
    TRACE_ASSERTION_STABILIZATION_SECONDS,
)
from agent_insights_quality.models import (
    AgentResult,
    InsightRunCheckpoint,
    InsightRunEvidence,
    InvocationEvidence,
    VersionResult,
    linked_operations_match_scope,
)
from agent_insights_quality.registry import version_entry
from agent_insights_quality.runtime_state import VersionCheckpointStore
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    InsightWindowExpiredError,
    TraceAssertionActivationError,
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


def _stage_error_code(error: Exception) -> str:
    return (
        error.code
        if isinstance(error, _VersionStageError)
        else type(error).__name__
    )


def _preflight_error_code(stage: str, error: Exception) -> str:
    if stage == "clean_window" and "pre-existing traces" in str(error):
        return "clean_window_not_empty"
    return f"{stage}_failed"


def _progress(runtime: Any, message: str) -> None:
    reporter = getattr(runtime, "report_progress", None)
    if callable(reporter):
        reporter(message)


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
    ) -> InvocationEvidence: ...

    def wait_for_telemetry(
        self,
        *,
        agent_name: str,
        foundry_version: str,
        invocation: InvocationEvidence,
    ) -> tuple[str, ...]: ...

    def verify_trace_contract(
        self,
        *,
        agent_name: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        required_operations: tuple[str, ...],
        window_start: str,
        window_end: str,
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
        stabilization_seconds: int,
        on_first_pass: Callable[[], None],
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
    ) -> InsightRunEvidence: ...


def execute(
    *,
    agents: dict[str, Any],
    issues: dict[str, Any],
    selected: dict[str, list[str]],
    registry: dict[str, Any],
    runtime: RuntimePort,
    seed: int,
    lookback_hours: float = 0.1,
    trace_assertion_stabilization_seconds: int = (
        TRACE_ASSERTION_STABILIZATION_SECONDS
    ),
    clean_window_poll_seconds: int = 15,
    clean_window_ingestion_margin_seconds: int = 30,
    clean_window_max_wait_seconds: int = 1200,
    trace_assertion_stabilization_seconds: int = 180,
    insight_start_margin_seconds: int = 30,
    max_recovery_versions: int = 3,
    agent_start_stagger_seconds: int = 0,
    checkpoint_store: VersionCheckpointStore | None = None,
) -> list[AgentResult]:
    _progress(
        runtime,
        f"qualification started: {len(selected)} Agents, "
        f"{sum(len(values) for values in selected.values())} issues",
    )
    agent_by_name = {item["name"]: item for item in agents["agents"]}
    issue_by_id = {item["id"]: item for item in issues["issues"]}
    results: dict[str, AgentResult] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(
                _execute_agent,
                agent_by_name[agent_name],
                [issue_by_id[value] for value in issue_ids],
                registry,
                runtime,
                seed,
                lookback_hours,
                trace_assertion_stabilization_seconds,
                clean_window_poll_seconds,
                clean_window_ingestion_margin_seconds,
                clean_window_max_wait_seconds,
                trace_assertion_stabilization_seconds,
                insight_start_margin_seconds,
                _RecoveryBudget(max_recovery_versions),
                checkpoint_store,
                index * agent_start_stagger_seconds,
            ): agent_name
            for index, (agent_name, issue_ids) in enumerate(selected.items())
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    ordered = [results[agent["name"]] for agent in agents["agents"]]
    _progress(runtime, "qualification runtime completed")
    return ordered


def _execute_agent(
    agent: dict[str, Any],
    issue_items: list[dict[str, Any]],
    registry: dict[str, Any],
    runtime: RuntimePort,
    seed: int,
    lookback_hours: float,
    trace_assertion_stabilization_seconds: int,
    clean_window_poll_seconds: int,
    clean_window_ingestion_margin_seconds: int,
    clean_window_max_wait_seconds: int,
    trace_assertion_stabilization_seconds: int,
    insight_start_margin_seconds: int,
    recovery_budget: _RecoveryBudget,
    checkpoint_store: VersionCheckpointStore | None,
    start_delay_seconds: int,
) -> AgentResult:
    name = agent["name"]
    start_stagger = _StartStagger(start_delay_seconds)
    monitor_id = registry["agents"][name]["monitor_id"]
    baseline: VersionResult | None = None
    resuming = checkpoint_store is not None and checkpoint_store.has_progress(name)
    if not resuming:
        try:
            _progress(runtime, f"{name}: reset monitor")
            runtime.reset_monitor(name, monitor_id)
        except Exception as error:
            baseline = VersionResult(
                logical_version="v0",
                foundry_version=version_entry(registry, name, "v0")[
                    "foundry_version"
                ],
                status="inconclusive",
                error_code=_preflight_error_code("monitor_reset", error),
            )
        if baseline is None:
            try:
                _progress(runtime, f"{name}: verify clean window")
                runtime.wait_for_clean_window(
                    name,
                    lookback_hours,
                    poll_seconds=clean_window_poll_seconds,
                    ingestion_margin_seconds=clean_window_ingestion_margin_seconds,
                    max_wait_seconds=clean_window_max_wait_seconds,
                )
            except Exception as error:
                baseline = VersionResult(
                    logical_version="v0",
                    foundry_version=version_entry(registry, name, "v0")[
                        "foundry_version"
                    ],
                    status="inconclusive",
                    error_code=_preflight_error_code("clean_window", error),
                )
    if baseline is None:
        try:
            _progress(runtime, f"{name}/v0: started")
            started = time.monotonic()
            baseline_had_progress = (
                checkpoint_store is not None
                and checkpoint_store.has_version_progress(name, "v0")
            )
            baseline_cached = (
                checkpoint_store.result(
                    name,
                    "v0",
                    version_entry(registry, name, "v0")["foundry_version"],
                    version_entry(registry, name, "v0")["content_digest"],
                )
                if checkpoint_store is not None and baseline_had_progress
                else None
            )
            if resuming and not baseline_had_progress:
                runtime.wait_for_clean_window(
                    name,
                    lookback_hours,
                    poll_seconds=clean_window_poll_seconds,
                    ingestion_margin_seconds=clean_window_ingestion_margin_seconds,
                    max_wait_seconds=clean_window_max_wait_seconds,
                )
                runtime.reset_monitor(name, monitor_id)
                resuming = False
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
                trace_assertion_stabilization_seconds=(
                    trace_assertion_stabilization_seconds
                ),
                clean_window_poll_seconds=clean_window_poll_seconds,
                clean_window_ingestion_margin_seconds=(
                    clean_window_ingestion_margin_seconds
                ),
                clean_window_max_wait_seconds=clean_window_max_wait_seconds,
                trace_assertion_stabilization_seconds=(
                    trace_assertion_stabilization_seconds
                ),
                insight_start_margin_seconds=insight_start_margin_seconds,
                recovery_budget=recovery_budget,
                checkpoint_store=checkpoint_store,
                start_stagger=start_stagger,
            )
            if resuming and baseline_cached is None:
                resuming = False
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
    if baseline.status not in {"passed", "not_at_bar"}:
        _progress(
            runtime,
            f"{name}/v0: incomplete ({baseline.error_code})",
        )
        skipped = [
            VersionResult(
                logical_version=item["id"],
                foundry_version=version_entry(registry, name, item["id"])[
                    "foundry_version"
                ],
                status="skipped_baseline",
            )
            for item in issue_items
        ]
        return AgentResult(name, baseline, skipped)

    results = []
    blocked_by_unaccounted_run = False
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
            issue_had_progress = (
                checkpoint_store is not None
                and checkpoint_store.has_version_progress(name, issue["id"])
            )
            issue_registry = version_entry(registry, name, issue["id"])
            issue_cached = (
                checkpoint_store.result(
                    name,
                    issue["id"],
                    issue_registry["foundry_version"],
                    issue_registry["content_digest"],
                )
                if checkpoint_store is not None and issue_had_progress
                else None
            )
            if resuming and not issue_had_progress:
                runtime.wait_for_clean_window(
                    name,
                    lookback_hours,
                    poll_seconds=clean_window_poll_seconds,
                    ingestion_margin_seconds=clean_window_ingestion_margin_seconds,
                    max_wait_seconds=clean_window_max_wait_seconds,
                )
                runtime.reset_monitor(name, monitor_id)
                resuming = False
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
                trace_assertion_stabilization_seconds=(
                    trace_assertion_stabilization_seconds
                ),
                clean_window_poll_seconds=clean_window_poll_seconds,
                clean_window_ingestion_margin_seconds=(
                    clean_window_ingestion_margin_seconds
                ),
                clean_window_max_wait_seconds=clean_window_max_wait_seconds,
                trace_assertion_stabilization_seconds=(
                    trace_assertion_stabilization_seconds
                ),
                insight_start_margin_seconds=insight_start_margin_seconds,
                recovery_budget=recovery_budget,
                checkpoint_store=checkpoint_store,
                start_stagger=start_stagger,
            )
            if resuming and issue_cached is None:
                resuming = False
        except Exception as error:
            result = VersionResult(
                logical_version=issue["id"],
                foundry_version=version_entry(registry, name, issue["id"])[
                    "foundry_version"
                ],
                status="inconclusive",
                error_code=_stage_error_code(error),
            )
        _progress(
            runtime,
            f"{name}/{issue['id']}: {result.status}"
            + (f" ({result.error_code})" if result.error_code else "")
            + f" in {time.monotonic() - started:.1f}s",
        )
        results.append(result)
        if (result.error_code or "").startswith("insight_run_unaccounted"):
            blocked_by_unaccounted_run = True
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
    trace_assertion_stabilization_seconds: int,
    clean_window_poll_seconds: int,
    clean_window_ingestion_margin_seconds: int,
    clean_window_max_wait_seconds: int,
    trace_assertion_stabilization_seconds: int,
    insight_start_margin_seconds: int,
    recovery_budget: _RecoveryBudget,
    checkpoint_store: VersionCheckpointStore | None,
    start_stagger: _StartStagger,
) -> VersionResult:
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
                trace_assertion_stabilization_seconds=(
                    trace_assertion_stabilization_seconds
                ),
                insight_start_margin_seconds=insight_start_margin_seconds,
                checkpoint_store=checkpoint_store,
                start_stagger=start_stagger,
            )
        except _VersionStageError as error:
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
                    checkpoint_store=checkpoint_store,
                )
                if quarantined is not None:
                    return quarantined
                raise
            _progress(
                runtime,
                f"{agent['name']}/{logical_version}: recovering {error.code}",
            )
            if error.code.startswith(
                "invocation_failed"
            ) or error.code.startswith(
                "insight_run_start_failed"
            ) or error.code.startswith(
                "insight_run_terminal_failed"
            ) or error.code == "insight_window_expired":
                if checkpoint_store is not None:
                    checkpoint_store.clear(
                        agent["name"],
                        logical_version,
                        registry_entry["foundry_version"],
                        registry_entry["content_digest"],
                    )
                runtime.wait_for_clean_window(
                    agent["name"],
                    lookback_hours,
                    poll_seconds=clean_window_poll_seconds,
                    ingestion_margin_seconds=clean_window_ingestion_margin_seconds,
                    max_wait_seconds=clean_window_max_wait_seconds,
                )
                runtime.reset_monitor(agent["name"], monitor_id)


def _recoverable(error: _VersionStageError) -> bool:
    if error.code == "insight_window_expired":
        return True
    if error.code.endswith("_credential"):
        return False
    match = re.search(r"_http_([0-9]{3})$", error.code)
    if match:
        status = int(match.group(1))
        return status in {408, 424, 429} or 500 <= status <= 599
    if error.code == "invocation_failed":
        return "before a response was received" in str(error.cause)
    if error.code == "insight_run_start_failed":
        return "before a response was received" in str(error.cause)
    return error.code.startswith(
        (
            "telemetry_failed",
            "trace_contract_failed",
            "trace_evidence_failed",
            "trace_assertion_failed",
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
) -> None:
    expected_requests = (
        int(agent["baseline_contract"]["request_count"])
        if baseline
        else invocation.request_count
    )
    if invocation.request_count != expected_requests:
        raise _VersionStageError(
            "endpoint_contract_failed",
            ContractError(
                f"{agent['name']}/{logical_version} expected "
                f"{expected_requests} endpoint requests"
            ),
        )
    if not (
        invocation.request_count
        == invocation.response_count
        == invocation.usable_response_count
    ):
        raise _VersionStageError(
            "endpoint_contract_failed",
            ContractError(
                f"{agent['name']}/{logical_version} endpoint evidence is incomplete"
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
        item.response_count != 1
        or not item.usable_response
        or item.direct_terminal_response_count != 1
        or item.function_call_count != 0
        for item in summaries
    ):
        raise _VersionStageError(
            "endpoint_contract_failed",
            ContractError(
                f"{agent['name']}/{logical_version} violated the direct Prompt "
                "response contract"
            ),
        )
    semantic_mode = agent["baseline_contract"]["semantic_assertions"]
    if baseline and (
        invocation.semantic_assertion_count < 1
        or invocation.semantic_assertions_passed
        != invocation.semantic_assertion_count
        or (
            semantic_mode == "required_per_request"
            and any(item.semantic_assertion_count < 1 for item in summaries)
        )
    ):
        raise _VersionStageError(
            "baseline_assertion_failed",
            ContractError(
                f"{agent['name']}/{logical_version} baseline semantic evidence "
                "is incomplete"
            ),
        )
    if not baseline and agent["type"] == "prompt" and not any(
        item.activation_gate for item in summaries
    ):
        raise _VersionStageError(
            "issue_activation_failed",
            ContractError(
                f"{agent['name']}/{logical_version} issue activation "
                "evidence is incomplete"
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
    agent: dict[str, Any],
    invocation: InvocationEvidence,
) -> bool:
    gates = [item for item in invocation.request_summaries if item.activation_gate]
    if not gates:
        return agent["type"] != "prompt"
    return all(
        item.semantic_assertion_count + item.trace_assertion_count > 0
        and item.semantic_assertions_passed == item.semantic_assertion_count
        and item.trace_assertions_passed == item.trace_assertion_count
        for item in gates
    )


def _validate_baseline_trace_evidence(
    *,
    agent: dict[str, Any],
    invocation: InvocationEvidence,
    trace_evidence: dict[str, Any],
) -> None:
    request_count = invocation.request_count
    terminal_mode = agent["baseline_contract"]["terminal_response"]
    terminal_complete = (
        int(trace_evidence.get("terminal_response_count") or 0) == request_count
        and int(trace_evidence.get("terminal_output_count") or 0) == request_count
    )
    if terminal_mode == "explicit_span_attributes":
        terminal_complete = terminal_complete and (
            int(trace_evidence.get("explicit_terminal_success_count") or 0)
            == request_count
            and int(trace_evidence.get("explicit_terminal_output_count") or 0)
            == request_count
        )
    else:
        terminal_complete = terminal_complete and (
            int(trace_evidence.get("assistant_response_count") or 0)
            == request_count
        )
    if not terminal_complete:
        raise ContractError(
            f"{agent['name']} baseline lacks one successful terminal output "
            "signal per request"
        )
    if int(trace_evidence.get("unhandled_error_count") or 0) != 0:
        raise ContractError(
            f"{agent['name']} baseline contains an unhandled error signal"
        )
    if agent["type"] == "prompt" and (
        int(trace_evidence.get("operation_count") or 0) != request_count
        or bool(trace_evidence.get("tool_call_counts"))
        or int(trace_evidence.get("tool_response_count") or 0) != 0
    ):
        raise ContractError(
            f"{agent['name']} baseline trace is not a direct Prompt execution"
        )


def _activation_failure_result(
    *,
    logical_version: str,
    foundry_version: str,
    operation_ids: tuple[str, ...],
    invocation: InvocationEvidence,
    trace_evidence: dict[str, Any],
) -> VersionResult:
    return VersionResult(
        logical_version=logical_version,
        foundry_version=foundry_version,
        status="inconclusive",
        operation_ids=list(operation_ids),
        window_start=invocation.started_at,
        window_end=invocation.completed_at,
        error_code="issue_activation_failed",
        endpoint_request_count=invocation.request_count,
        endpoint_response_count=invocation.response_count,
        endpoint_usable_response_count=invocation.usable_response_count,
        semantic_assertion_count=invocation.semantic_assertion_count,
        semantic_assertions_passed=invocation.semantic_assertions_passed,
        trace_assertion_count=invocation.trace_assertion_count,
        trace_assertions_passed=invocation.trace_assertions_passed,
        trace_contract_verified=True,
        trace_behavior_summary=trace_evidence,
        endpoint_request_summaries=list(invocation.request_summaries),
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
        f"{agent_name}/{result.logical_version}: activation evidence {reason}; "
        "discarding the persisted Agent Insights run result",
    )
    try:
        runtime.finish_insights_run(
            agent_name=agent_name,
            monitor_id=monitor_id,
            foundry_version=foundry_version,
            operation_ids=operation_ids,
            checkpoint=checkpoint,
        )
    except Exception:
        _progress(
            runtime,
            f"{agent_name}/{result.logical_version}: persisted Agent Insights run "
            "remains quarantined by the saved activation failure",
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
    trace_assertion_stabilization_seconds: int,
    insight_start_margin_seconds: int,
    checkpoint_store: VersionCheckpointStore | None,
    start_stagger: _StartStagger,
) -> VersionResult:
    foundry_version = registry_entry["foundry_version"]
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
    required_operations = tuple(
        expected["trace_contract"]["operations"]
        if expected is not None
        else ("invoke_agent", "chat")
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
                required_operations=required_operations,
                window_start=invocation.started_at,
                window_end=invocation.completed_at,
            )
        except Exception as error:
            raise _VersionStageError("trace_contract_failed", error) from error
        if checkpoint_store is not None:
            checkpoint_store.save_trace_verified(*checkpoint_args)
    _progress(runtime, f"{agent['name']}/{logical_version}: trace contract verified")
    insight_checkpoint = (
        checkpoint_store.insight_run(*checkpoint_args)
        if checkpoint_store is not None
        else None
    )
    activation_rejection = (
        checkpoint_store.activation_rejection(*checkpoint_args)
        if checkpoint_store is not None and expected is not None
        else None
    )
    if activation_rejection is not None:
        rejection_code, rejection_trace_evidence = activation_rejection
        return _activation_failure_result(
            runtime=runtime,
            agent=agent,
            monitor_id=monitor_id,
            logical_version=logical_version,
            foundry_version=foundry_version,
            operation_ids=operation_ids,
            invocation=invocation,
            trace_evidence=rejection_trace_evidence,
            insight_checkpoint=insight_checkpoint,
            checkpoint_store=checkpoint_store,
            checkpoint_args=checkpoint_args,
            error_code=rejection_code,
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
    try:
        trace_evidence = runtime.trace_behavior_evidence(operation_ids)
        if expected is None:
            _validate_baseline_trace_evidence(
                agent=agent,
                invocation=invocation,
                trace_evidence=trace_evidence,
            )
    except Exception as error:
        if expected is None:
            raise _VersionStageError("baseline_evidence_failed", error) from error
        raise _VersionStageError("trace_evidence_failed", error) from error
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
        if checkpoint_store is not None:
            checkpoint_store.mark_insight_start_pending(*checkpoint_args)
        try:
            insight_checkpoint = runtime.start_insights_run(
                agent_name=agent["name"],
                monitor_id=monitor_id,
                foundry_version=foundry_version,
                operation_ids=operation_ids,
                lookback_hours=lookback_hours,
                start_margin_seconds=insight_start_margin_seconds,
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
            if expected is not None:
                raise _VersionStageError(
                    "issue_activation_failed",
                    ContractError(
                        "First passing trace snapshot arrived after the guarded "
                        "Agent Insights start window"
                    ),
                ) from error
            raise _VersionStageError("insight_run_start_failed", error) from error
        except Exception as error:
            raise _VersionStageError("insight_run_start_failed", error) from error

    if expected is not None:
        activation_error = False
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
                    stabilization_seconds=trace_assertion_stabilization_seconds,
                    on_first_pass=start_insight_run_once,
                ),
            )
        except _VersionStageError as error:
            if error.code != "issue_activation_failed":
                raise
            activation_error = True
        except TraceAssertionActivationError:
            activation_error = True
        except Exception as error:
            raise _VersionStageError("issue_activation_failed", error) from error
        if activation_error:
            result = _activation_failure_result(
                logical_version=logical_version,
                foundry_version=foundry_version,
                operation_ids=operation_ids,
                invocation=invocation,
                trace_evidence=trace_evidence,
            )
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
                reason="did not stabilize",
            )
            return result
        if checkpoint_store is not None:
            checkpoint_store.save_invocation(*checkpoint_args, invocation)
        if not _issue_activation_evidence_complete(agent, invocation):
            result = _activation_failure_result(
                logical_version=logical_version,
                foundry_version=foundry_version,
                operation_ids=operation_ids,
                invocation=invocation,
                trace_evidence=trace_evidence,
            )
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
                reason="trace assertions did not pass",
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
    if len(matching) != 1:
        result.status = "not_at_bar"
        result.error_code = "expected_exactly_one_insight"
        if checkpoint_store is not None:
            checkpoint_store.save_result(*checkpoint_args, result)
        return result
    insight = matching[0]
    if insight.trace_count < int(expected["trace_contract"]["minimum_traces"]):
        result.status = "not_at_bar"
        result.error_code = "insufficient_trace_evidence"
        if checkpoint_store is not None:
            checkpoint_store.save_result(*checkpoint_args, result)
        return result
    result.status = "observed"
    result.observed_insight = insight
    if checkpoint_store is not None:
        checkpoint_store.save_result(*checkpoint_args, result)
    return result


def _activation_failure_result(
    *,
    runtime: RuntimePort,
    agent: dict[str, Any],
    monitor_id: str,
    logical_version: str,
    foundry_version: str,
    operation_ids: tuple[str, ...],
    invocation: InvocationEvidence,
    trace_evidence: dict[str, Any],
    insight_checkpoint: InsightRunCheckpoint | None,
    checkpoint_store: VersionCheckpointStore | None,
    checkpoint_args: tuple[str, str, str, str],
    error_code: str = "issue_activation_failed",
) -> VersionResult:
    if checkpoint_store is not None:
        checkpoint_store.save_activation_rejection(
            *checkpoint_args,
            trace_evidence,
            error_code,
        )
    window_start = invocation.started_at
    window_end = invocation.completed_at
    if insight_checkpoint is not None:
        try:
            discarded = runtime.finish_insights_run(
                agent_name=agent["name"],
                monitor_id=monitor_id,
                foundry_version=foundry_version,
                operation_ids=operation_ids,
                checkpoint=insight_checkpoint,
            )
        except Exception as error:
            raise _VersionStageError("insight_run_poll_failed", error) from error
        window_start = discarded.window_start
        window_end = discarded.window_end
        _progress(
            runtime,
            f"{agent['name']}/{logical_version}: activation failed; "
            f"discarded {len(discarded.insights)} Agent Insights cards",
        )
    result = VersionResult(
        logical_version=logical_version,
        foundry_version=foundry_version,
        status="inconclusive",
        operation_ids=list(operation_ids),
        window_start=window_start,
        window_end=window_end,
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
        endpoint_request_summaries=list(invocation.request_summaries),
    )
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
    if (
        insight_checkpoint is None
        or invocation is None
        or operation_ids is None
    ):
        return None
    existing_rejection = checkpoint_store.activation_rejection(*checkpoint_args)
    if existing_rejection is not None:
        raise _VersionStageError(
            "insight_run_unaccounted",
            RuntimeError("Started Agent Insights run could not be safely accounted"),
        )
    rejection_code = error.code
    trace_evidence: dict[str, Any] = {}
    checkpoint_store.save_activation_rejection(
        *checkpoint_args,
        trace_evidence,
        rejection_code,
    )
    try:
        return _activation_failure_result(
            runtime=runtime,
            agent=agent,
            monitor_id=monitor_id,
            logical_version=logical_version,
            foundry_version=registry_entry["foundry_version"],
            operation_ids=operation_ids,
            invocation=invocation,
            trace_evidence=trace_evidence,
            insight_checkpoint=insight_checkpoint,
            checkpoint_store=checkpoint_store,
            checkpoint_args=checkpoint_args,
            error_code=rejection_code,
        )
    except _VersionStageError as drain_error:
        raise _VersionStageError(
            "insight_run_unaccounted",
            RuntimeError("Started Agent Insights run could not be safely accounted"),
        ) from drain_error
