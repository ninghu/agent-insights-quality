from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Protocol

from agent_insights_quality.models import (
    AgentResult,
    InsightRunCheckpoint,
    InsightRunEvidence,
    InvocationEvidence,
    VersionResult,
)
from agent_insights_quality.registry import version_entry
from agent_insights_quality.runtime_state import VersionCheckpointStore
from agent_insights_quality.util import ROOT, InsightWindowExpiredError


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
    def __init__(self, maximum: int) -> None:
        self._maximum = maximum
        self._claimed = 0
        self._lock = threading.Lock()

    def claim(self) -> bool:
        with self._lock:
            if self._claimed >= self._maximum:
                return False
            self._claimed += 1
            return True


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

    def start_insights_run(
        self,
        *,
        agent_name: str,
        monitor_id: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        lookback_hours: float,
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
    lookback_hours: float = 3.0,
    clean_window_poll_seconds: int = 15,
    clean_window_ingestion_margin_seconds: int = 30,
    clean_window_max_wait_seconds: int = 12000,
    max_recovery_versions: int = 3,
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
    recovery_budget = _RecoveryBudget(max_recovery_versions)
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
                clean_window_poll_seconds,
                clean_window_ingestion_margin_seconds,
                clean_window_max_wait_seconds,
                recovery_budget,
                checkpoint_store,
            ): agent_name
            for agent_name, issue_ids in selected.items()
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
    clean_window_poll_seconds: int,
    clean_window_ingestion_margin_seconds: int,
    clean_window_max_wait_seconds: int,
    recovery_budget: _RecoveryBudget,
    checkpoint_store: VersionCheckpointStore | None,
) -> AgentResult:
    name = agent["name"]
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
                clean_window_poll_seconds=clean_window_poll_seconds,
                clean_window_ingestion_margin_seconds=(
                    clean_window_ingestion_margin_seconds
                ),
                clean_window_max_wait_seconds=clean_window_max_wait_seconds,
                recovery_budget=recovery_budget,
                checkpoint_store=checkpoint_store,
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
    for index, issue in enumerate(issue_items, start=1):
        started = time.monotonic()
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
                clean_window_poll_seconds=clean_window_poll_seconds,
                clean_window_ingestion_margin_seconds=(
                    clean_window_ingestion_margin_seconds
                ),
                clean_window_max_wait_seconds=clean_window_max_wait_seconds,
                recovery_budget=recovery_budget,
                checkpoint_store=checkpoint_store,
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
    clean_window_poll_seconds: int,
    clean_window_ingestion_margin_seconds: int,
    clean_window_max_wait_seconds: int,
    recovery_budget: _RecoveryBudget,
    checkpoint_store: VersionCheckpointStore | None,
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
                checkpoint_store=checkpoint_store,
            )
        except _VersionStageError as error:
            if (
                checkpoint_store is None
                or not _recoverable(error)
                or not recovery_budget.claim()
            ):
                raise
            _progress(
                runtime,
                f"{agent['name']}/{logical_version}: recovering {error.code}",
            )
            if error.code.startswith(
                "invocation_failed"
            ) or error.code.startswith(
                "insight_run_start_failed"
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
            "insight_run_poll_failed",
            "insight_run_terminal_failed",
        )
    )


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
    checkpoint_store: VersionCheckpointStore | None,
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
        return cached
    invocation = (
        checkpoint_store.invocation(*checkpoint_args)
        if checkpoint_store is not None
        else None
    )
    if invocation is None:
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
    if insight_checkpoint is None:
        if (
            checkpoint_store is not None
            and checkpoint_store.insight_start_pending(*checkpoint_args)
        ):
            raise _VersionStageError(
                "insight_run_start_failed",
                RuntimeError(
                    "Remote operation failed before a response was received"
                ),
            )
        if checkpoint_store is not None:
            checkpoint_store.mark_insight_start_pending(*checkpoint_args)
        try:
            insight_checkpoint = runtime.start_insights_run(
                agent_name=agent["name"],
                monitor_id=monitor_id,
                foundry_version=foundry_version,
                operation_ids=operation_ids,
                lookback_hours=lookback_hours,
                persist=(
                    lambda checkpoint: checkpoint_store.save_insight_run(
                        *checkpoint_args,
                        checkpoint,
                    )
                    if checkpoint_store is not None
                    else None
                ),
            )
        except Exception as error:
            raise _VersionStageError("insight_run_start_failed", error) from error
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
    _progress(
        runtime,
        f"{agent['name']}/{logical_version}: Agent Insights "
        f"{insight_run.status.lower()} ({len(insight_run.insights)} cards)",
    )
    result = VersionResult(
        logical_version=logical_version,
        foundry_version=foundry_version,
        status="inconclusive",
        operation_ids=list(operation_ids),
        insight_references=[item.reference for item in insight_run.insights],
        window_start=insight_run.window_start,
        window_end=insight_run.window_end,
        observed_insights=list(insight_run.insights),
        endpoint_request_count=invocation.request_count,
        endpoint_response_count=invocation.response_count,
        endpoint_usable_response_count=invocation.usable_response_count,
        semantic_assertion_count=invocation.semantic_assertion_count,
        semantic_assertions_passed=invocation.semantic_assertions_passed,
        trace_contract_verified=True,
    )
    if insight_run.status != "succeeded":
        if checkpoint_store is not None:
            checkpoint_store.clear_insight_run(*checkpoint_args)
        raise _VersionStageError(
            "insight_run_terminal_failed",
            RuntimeError("Agent Insights run did not succeed"),
        )
    if expected is None:
        result.status = "passed" if not insight_run.insights else "not_at_bar"
        if checkpoint_store is not None:
            checkpoint_store.save_result(*checkpoint_args, result)
        return result
    matching = [
        item
        for item in insight_run.insights
        if item.agent_version == foundry_version
        and set(item.linked_operation_ids).issubset(set(operation_ids))
        and set(item.linked_operation_ids)
    ]
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
