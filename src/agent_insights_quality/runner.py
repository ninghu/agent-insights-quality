from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Protocol

from agent_insights_quality.models import (
    AgentResult,
    InsightRunEvidence,
    InvocationEvidence,
    VersionResult,
)
from agent_insights_quality.registry import version_entry
from agent_insights_quality.util import ROOT


class _VersionStageError(Exception):
    def __init__(self, code: str, cause: Exception) -> None:
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

    def assert_clean_window(self, agent_name: str, lookback_hours: int) -> None: ...

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
    ) -> None: ...

    def run_insights(
        self,
        *,
        agent_name: str,
        monitor_id: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        lookback_hours: int,
    ) -> InsightRunEvidence: ...


def execute(
    *,
    agents: dict[str, Any],
    issues: dict[str, Any],
    selected: dict[str, list[str]],
    registry: dict[str, Any],
    runtime: RuntimePort,
    seed: int,
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
) -> AgentResult:
    name = agent["name"]
    monitor_id = registry["agents"][name]["monitor_id"]
    baseline: VersionResult | None = None
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
            runtime.assert_clean_window(name, 3)
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
            baseline = _execute_version(
                runtime=runtime,
                agent=agent,
                monitor_id=monitor_id,
                logical_version="v0",
                registry_entry=version_entry(registry, name, "v0"),
                traffic_path=ROOT / agent["baseline_path"] / "traffic.json",
                seed=seed,
                expected=None,
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
            result = _execute_version(
                runtime=runtime,
                agent=agent,
                monitor_id=monitor_id,
                logical_version=issue["id"],
                registry_entry=version_entry(registry, name, issue["id"]),
                traffic_path=ROOT / issue["implementation"] / "traffic.json",
                seed=seed + index,
                expected=issue,
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
        _progress(
            runtime,
            f"{name}/{issue['id']}: {result.status}"
            + (f" ({result.error_code})" if result.error_code else "")
            + f" in {time.monotonic() - started:.1f}s",
        )
        results.append(result)
    return AgentResult(name, baseline, results)


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
) -> VersionResult:
    foundry_version = registry_entry["foundry_version"]
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
    _progress(
        runtime,
        f"{agent['name']}/{logical_version}: endpoint complete "
        f"({invocation.response_count}/{invocation.request_count} responses)",
    )
    try:
        operation_ids = runtime.wait_for_telemetry(
            agent_name=agent["name"],
            foundry_version=foundry_version,
            invocation=invocation,
        )
    except Exception as error:
        raise _VersionStageError("telemetry_failed", error) from error
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
    try:
        runtime.verify_trace_contract(
            agent_name=agent["name"],
            foundry_version=foundry_version,
            operation_ids=operation_ids,
            required_operations=required_operations,
        )
    except Exception as error:
        raise _VersionStageError("trace_contract_failed", error) from error
    _progress(runtime, f"{agent['name']}/{logical_version}: trace contract verified")
    try:
        insight_run = runtime.run_insights(
            agent_name=agent["name"],
            monitor_id=monitor_id,
            foundry_version=foundry_version,
            operation_ids=operation_ids,
            lookback_hours=3,
        )
    except Exception as error:
        raise _VersionStageError("insight_run_failed", error) from error
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
        result.error_code = "insight_run_failed"
        return result
    if expected is None:
        result.status = "passed" if not insight_run.insights else "not_at_bar"
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
        return result
    insight = matching[0]
    if insight.trace_count < int(expected["trace_contract"]["minimum_traces"]):
        result.status = "not_at_bar"
        result.error_code = "insufficient_trace_evidence"
        return result
    result.status = "observed"
    result.observed_insight = insight
    return result
