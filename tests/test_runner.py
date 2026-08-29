from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from agent_insights_quality.catalogs import catalog_hashes, load_catalogs
from agent_insights_quality.live import LiveRuntime
from agent_insights_quality.models import (
    InsightEvidence,
    InsightRunCheckpoint,
    InsightRunEvidence,
    InvocationEvidence,
    RequestCompletionEvidence,
    SemanticAssertionEvidence,
    TraceAssertionEvidence,
    VersionResult,
)
from agent_insights_quality.runner import (
    _RecoveryBudget,
    _StartStagger,
    _execute_version,
    _execute_version_with_recovery,
    _validate_baseline_trace_evidence,
    execute,
)
from agent_insights_quality.runtime_state import VersionCheckpointStore
from agent_insights_quality.selection import select_daily
from agent_insights_quality.util import ContractError, InsightWindowExpiredError


def _registry(agents: dict, hashes: dict[str, str]) -> dict:
    return {
        "profile": "daily",
        "project_name": "agent-insights-quality",
        "catalog_hashes": hashes,
        "agents": {
            agent["name"]: {
                "monitor_id": f"monitor-{agent['name']}",
                "versions": {
                    logical: {
                        "foundry_version": logical,
                        "content_digest": "sha256:" + "a" * 64,
                    }
                    for logical in ["v0", *agent["issue_ids"]]
                },
            }
            for agent in agents["agents"]
        },
    }


class FakeRuntime:
    def __init__(
        self,
        *,
        baseline_noise: str | None = None,
        fail: str | None = None,
        reset_failure_agent: str | None = None,
        clean_window_failure_agent: str | None = None,
        probe_concurrency: bool = False,
    ):
        self.baseline_noise = baseline_noise
        self.fail = fail
        self.reset_failure_agent = reset_failure_agent
        self.clean_window_failure_agent = clean_window_failure_agent
        self.probe_concurrency = probe_concurrency
        self.invoked: list[str] = []
        self._active_agents: set[str] = set()
        self._concurrency_lock = threading.Lock()
        self.maximum_concurrent_agents = 0
        self.progress: list[str] = []
        self.reset_agents: list[str] = []
        self.clean_agents: list[str] = []

    def report_progress(self, message: str) -> None:
        self.progress.append(message)

    def reset_monitor(self, agent_name: str, monitor_id: str) -> None:
        assert agent_name in monitor_id
        self.reset_agents.append(agent_name)
        if agent_name == self.reset_failure_agent:
            raise RuntimeError("synthetic reset failure")

    def wait_for_clean_window(
        self,
        agent_name: str,
        lookback_hours: float,
        **kwargs,
    ) -> None:
        assert agent_name.endswith("-agent")
        assert lookback_hours == 0.1
        assert kwargs == {
            "poll_seconds": 15,
            "ingestion_margin_seconds": 30,
            "max_wait_seconds": 1200,
        }
        self.clean_agents.append(agent_name)
        if agent_name == self.clean_window_failure_agent:
            raise RuntimeError(f"{agent_name} has pre-existing traces")

    def invoke_version(
        self,
        *,
        agent_name: str,
        agent_type: str,
        foundry_version: str,
        traffic_path: Path,
        seed: int,
    ) -> InvocationEvidence:
        del seed
        if self.probe_concurrency:
            with self._concurrency_lock:
                assert agent_name not in self._active_agents
                self._active_agents.add(agent_name)
                self.maximum_concurrent_agents = max(
                    self.maximum_concurrent_agents,
                    len(self._active_agents),
                )
            time.sleep(0.02)
            with self._concurrency_lock:
                self._active_agents.remove(agent_name)
        self.invoked.append(foundry_version)
        if foundry_version == self.fail:
            raise RuntimeError("synthetic operational failure")
        payload = json.loads(traffic_path.read_text(encoding="utf-8"))
        request_count = len(payload["requests"])
        summaries = tuple(
            RequestCompletionEvidence(
                request_index=index,
                response_count=1,
                usable_response=True,
                semantic_assertion_count=1,
                semantic_assertions_passed=1,
                assertion_results=(
                    SemanticAssertionEvidence("synthetic_contract", True),
                ),
                activation_gate=bool(
                    request.get("expected", {}).get("activation_gate")
                ),
                direct_terminal_response_count=int(agent_type == "prompt"),
                function_call_count=0,
            )
            for index, request in enumerate(payload["requests"])
        )
        return InvocationEvidence(
            (),
            tuple(f"{foundry_version}-{index}" for index in range(request_count)),
            "2026-08-24T10:00:00+00:00",
            "2026-08-24T10:01:00+00:00",
            request_count,
            False,
            response_count=request_count,
            usable_response_count=request_count,
            semantic_assertion_count=request_count,
            semantic_assertions_passed=request_count,
            request_summaries=summaries,
        )

    def wait_for_telemetry(
        self,
        *,
        agent_name: str,
        foundry_version: str,
        invocation: InvocationEvidence,
    ) -> tuple[str, ...]:
        del agent_name, foundry_version
        return tuple(
            f"{index + 1:032x}" for index in range(invocation.request_count)
        )

    def trace_behavior_evidence(
        self,
        operation_ids: tuple[str, ...],
    ) -> dict:
        count = len(operation_ids)
        return {
            "operation_count": count,
            "tool_call_counts": {},
            "tool_response_count": 0,
            "successful_tool_response_count": 0,
            "error_codes": {},
            "assistant_response_count": count,
            "explicit_terminal_success_count": count,
            "explicit_terminal_output_count": count,
            "terminal_success_count": count,
            "terminal_output_count": count,
            "terminal_response_count": count,
            "handled_error_count": 0,
            "unhandled_error_count": 0,
        }

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
        on_first_pass,
    ) -> tuple[tuple[TraceAssertionEvidence, ...], ...]:
        payload = json.loads(traffic_path.read_text(encoding="utf-8"))
        assert agent_name.endswith("-agent")
        assert foundry_version
        assert len(operation_ids) == len(response_references)
        assert window_start < window_end
        assert stabilization_seconds == 180
        on_first_pass()
        return tuple(
            tuple(
                TraceAssertionEvidence(item["name"], True)
                for item in request["expected"].get("trace_assertions", [])
            )
            for request in payload["requests"]
        )

    def start_insights_run(
        self,
        *,
        agent_name: str,
        monitor_id: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        lookback_hours: float,
        start_margin_seconds: int,
        persist,
    ) -> InsightRunCheckpoint:
        del agent_name, monitor_id, foundry_version, operation_ids
        assert lookback_hours == 0.1
        assert start_margin_seconds == 30
        checkpoint = InsightRunCheckpoint("synthetic-run", {})
        persist(checkpoint)
        return checkpoint

    def finish_insights_run(
        self,
        *,
        agent_name: str,
        monitor_id: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        checkpoint: InsightRunCheckpoint,
    ) -> InsightRunEvidence:
        del agent_name, monitor_id
        assert checkpoint.run_id == "synthetic-run"
        if foundry_version == "v0" and self.baseline_noise is None:
            insights = ()
        else:
            version = self.baseline_noise if foundry_version == "v0" else foundry_version
            insights = (
                InsightEvidence(
                    reference="sha256:" + "b" * 64,
                    agent_version=version or foundry_version,
                    title="Synthetic finding",
                    description="One deterministic defect.",
                    category="output_quality",
                    severity="medium",
                    proposed_fix="Apply the reviewed bounded fix.",
                    linked_operation_ids=operation_ids,
                    trace_count=5,
                    updated_at="2026-08-24T10:02:00+00:00",
                ),
            )
        return InsightRunEvidence(
            run_reference="sha256:" + "c" * 64,
            window_start="2026-08-24T10:00:00+00:00",
            window_end="2026-08-24T10:02:00+00:00",
            status="succeeded",
            insights=insights,
        )

    def verify_trace_contract(
        self,
        *,
        agent_name: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        required_operations: tuple[str, ...],
        window_start: str,
        window_end: str,
    ) -> None:
        assert agent_name.endswith("-agent")
        assert foundry_version
        assert operation_ids
        assert "invoke_agent" in required_operations
        assert window_start < window_end


def _timed_trace_row(tool_name: str, *, timestamp: str) -> dict:
    return {
        "operation_id": f"{1:032x}",
        "operation_name": "execute_tool",
        "tool_name": tool_name,
        "tool_call_id": "",
        "error_type": "",
        "tool_ok": "",
        "tool_result": "",
        "tool_arguments": "",
        "messages": ["", ""],
        "timestamp": timestamp,
        "duration": 1.0,
        "span_name": f"tool.{tool_name}",
        "terminal_success": "",
        "terminal_output": "",
        "handled_error": "",
        "matched_reference": "issue-synthetic-0",
    }


class TimedTraceRuntime(FakeRuntime):
    def __init__(self, rows_at, *, finish_failures: int = 0) -> None:
        super().__init__()
        self.monotonic = 0.0
        self.wall = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
        self.rows_at = rows_at
        self.starts: list[tuple[float, datetime]] = []
        self.finishes = 0
        self.finish_failures = finish_failures
        self._monotonic = lambda: self.monotonic
        self._sleep = self._advance

    def _advance(self, seconds: float) -> None:
        self.monotonic += seconds
        self.wall += timedelta(seconds=seconds)

    def _trace_rows(
        self,
        operation_ids,
        response_references=(),
        foundry_version=None,
        agent_name=None,
        window_start=None,
        window_end=None,
    ):
        assert operation_ids == (f"{1:032x}",)
        assert response_references == ("issue-synthetic-0",)
        assert foundry_version == "issue-synthetic"
        assert agent_name == "finance-agent"
        assert window_start == "2026-08-24T10:00:00+00:00"
        assert window_end == "2026-08-24T10:01:00+00:00"
        return self.rows_at(self.monotonic)

    def trace_assertion_evidence(self, **kwargs):
        return LiveRuntime.trace_assertion_evidence(self, **kwargs)

    def start_insights_run(self, **kwargs) -> InsightRunCheckpoint:
        self.starts.append((self.monotonic, self.wall))
        if self.monotonic > 360 - kwargs["start_margin_seconds"]:
            raise InsightWindowExpiredError("synthetic guarded start expiry")
        return super().start_insights_run(**kwargs)

    def finish_insights_run(self, **kwargs) -> InsightRunEvidence:
        self.finishes += 1
        if self.finishes <= self.finish_failures:
            raise ContractError("synthetic drain interruption")
        return super().finish_insights_run(**kwargs)


def _write_timed_trace_traffic(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "requests": [
                    {
                        "id": "request_A1b2C3d4",
                        "request": {"body": {"input": "synthetic request"}},
                        "expected": {
                            "http_status": 200,
                            "activation_gate": True,
                            "trace_assertions": [
                                {
                                    "name": "one_lookup",
                                    "kind": "tool_call_count",
                                    "tool_name": "lookup",
                                    "count": 1,
                                }
                            ],
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _timed_version_kwargs(
    tmp_path: Path,
    runtime: TimedTraceRuntime,
    checkpoint_store: VersionCheckpointStore,
) -> dict:
    traffic_path = tmp_path / "traffic.json"
    _write_timed_trace_traffic(traffic_path)
    return {
        "runtime": runtime,
        "agent": {
            "name": "finance-agent",
            "type": "hosted",
            "baseline_contract": {"semantic_assertions": "required_per_request"},
        },
        "monitor_id": "monitor-finance-agent",
        "logical_version": "issue-synthetic",
        "registry_entry": {
            "foundry_version": "issue-synthetic",
            "content_digest": "sha256:" + "a" * 64,
        },
        "traffic_path": traffic_path,
        "seed": 1,
        "expected": {
            "trace_contract": {
                "operations": ["invoke_agent"],
                "minimum_traces": 1,
            }
        },
        "lookback_hours": 0.1,
        "trace_assertion_stabilization_seconds": 180,
        "insight_start_margin_seconds": 30,
        "checkpoint_store": checkpoint_store,
        "start_stagger": _StartStagger(0),
    }


def test_first_passing_trace_starts_insights_before_stabilization(
    tmp_path: Path,
) -> None:
    failing = [_timed_trace_row("different_lookup", timestamp="2026-08-29T12:00:00Z")]
    passing = [_timed_trace_row("lookup", timestamp="2026-08-29T12:02:15Z")]
    runtime = TimedTraceRuntime(
        lambda elapsed: failing if elapsed < 135 else passing
    )
    store = VersionCheckpointStore(tmp_path / "stages", "sha256:" + "d" * 64)

    result = _execute_version(**_timed_version_kwargs(tmp_path, runtime, store))

    assert result.status == "observed"
    assert [start[0] for start in runtime.starts] == [135]
    assert runtime.starts[0][0] < 360
    assert runtime.monotonic == 315
    assert runtime.finishes == 1


def test_late_duplicate_quarantines_started_run_and_resume_reuses_result(
    tmp_path: Path,
) -> None:
    first = _timed_trace_row("lookup", timestamp="2026-08-29T12:00:00Z")
    duplicate = _timed_trace_row("lookup", timestamp="2026-08-29T12:02:15Z")
    runtime = TimedTraceRuntime(
        lambda elapsed: [first] if elapsed < 135 else [first, duplicate]
    )
    store = VersionCheckpointStore(tmp_path / "stages", "sha256:" + "d" * 64)
    kwargs = _timed_version_kwargs(tmp_path, runtime, store)

    result = _execute_version(**kwargs)
    resumed = _execute_version(**kwargs)

    assert result == resumed
    assert result.status == "inconclusive"
    assert result.error_code == "issue_activation_failed"
    assert result.insight_references == []
    assert result.observed_insights == []
    assert [start[0] for start in runtime.starts] == [0]
    assert runtime.finishes == 1
    assert runtime.invoked.count("issue-synthetic") == 1


def test_late_external_operation_quarantines_started_run(
    tmp_path: Path,
) -> None:
    first = _timed_trace_row("lookup", timestamp="2026-08-29T12:00:00Z")
    external = {
        **first,
        "operation_id": f"{2:032x}",
        "timestamp": "2026-08-29T12:00:01Z",
    }
    runtime = TimedTraceRuntime(
        lambda elapsed: [first] if elapsed < 135 else [first, external]
    )
    store = VersionCheckpointStore(tmp_path / "stages", "sha256:" + "d" * 64)
    kwargs = _timed_version_kwargs(tmp_path, runtime, store)

    result = _execute_version(**kwargs)
    resumed = _execute_version(**kwargs)

    assert result == resumed
    assert result.status == "inconclusive"
    assert result.error_code == "issue_activation_failed"
    assert result.insight_references == []
    assert result.observed_insights == []
    assert [start[0] for start in runtime.starts] == [0]
    assert runtime.monotonic == 135
    assert runtime.finishes == 1
    assert runtime.invoked.count("issue-synthetic") == 1


def test_resume_retries_interrupted_rejected_run_drain(
    tmp_path: Path,
) -> None:
    first = _timed_trace_row("lookup", timestamp="2026-08-29T12:00:00Z")
    duplicate = _timed_trace_row("lookup", timestamp="2026-08-29T12:02:15Z")
    runtime = TimedTraceRuntime(
        lambda elapsed: [first] if elapsed < 135 else [first, duplicate],
        finish_failures=1,
    )
    store = VersionCheckpointStore(tmp_path / "stages", "sha256:" + "d" * 64)
    kwargs = _timed_version_kwargs(tmp_path, runtime, store)
    checkpoint_args = (
        "finance-agent",
        "issue-synthetic",
        "issue-synthetic",
        "sha256:" + "a" * 64,
    )

    result = _execute_version(**kwargs)
    assert store.insight_drain_pending(*checkpoint_args) is True

    resumed = _execute_version(**kwargs)

    assert resumed == result
    assert [start[0] for start in runtime.starts] == [0]
    assert runtime.finishes == 2
    assert store.insight_drain_pending(*checkpoint_args) is False


def test_late_first_pass_is_incomplete_without_recovery_retraffic(
    tmp_path: Path,
) -> None:
    failing = [_timed_trace_row("different_lookup", timestamp="2026-08-29T12:00:00Z")]
    passing = [_timed_trace_row("lookup", timestamp="2026-08-29T12:14:45Z")]
    runtime = TimedTraceRuntime(
        lambda elapsed: failing if elapsed < 885 else passing
    )
    store = VersionCheckpointStore(tmp_path / "stages", "sha256:" + "d" * 64)
    kwargs = _timed_version_kwargs(tmp_path, runtime, store)

    result = _execute_version_with_recovery(
        **kwargs,
        clean_window_poll_seconds=15,
        clean_window_ingestion_margin_seconds=30,
        clean_window_max_wait_seconds=1200,
        recovery_budget=_RecoveryBudget(3),
    )

    assert result.status == "inconclusive"
    assert result.error_code == "issue_activation_failed"
    assert [start[0] for start in runtime.starts] == [885]
    assert runtime.invoked.count("issue-synthetic") == 1
    assert runtime.clean_agents == []
    assert runtime.reset_agents == []


def test_transient_trace_query_failure_uses_existing_recovery(
    tmp_path: Path,
) -> None:
    passing = [_timed_trace_row("lookup", timestamp="2026-08-29T12:00:00Z")]

    class RecoveringTraceRuntime(TimedTraceRuntime):
        def __init__(self) -> None:
            super().__init__(lambda _elapsed: passing)
            self.assertion_attempts = 0

        def trace_assertion_evidence(self, **kwargs):
            self.assertion_attempts += 1
            if self.assertion_attempts == 1:
                raise ContractError("Remote operation failed with HTTP 503")
            return super().trace_assertion_evidence(**kwargs)

    runtime = RecoveringTraceRuntime()
    store = VersionCheckpointStore(tmp_path / "stages", "sha256:" + "d" * 64)
    kwargs = _timed_version_kwargs(tmp_path, runtime, store)

    result = _execute_version_with_recovery(
        **kwargs,
        clean_window_poll_seconds=15,
        clean_window_ingestion_margin_seconds=30,
        clean_window_max_wait_seconds=1200,
        recovery_budget=_RecoveryBudget(3),
    )

    assert result.status == "observed"
    assert runtime.assertion_attempts == 2
    assert len(runtime.starts) == 1


def test_runner_executes_20_issues() -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = select_daily(date(2026, 8, 24), agents, issues, hashes["issues"])
    runtime = FakeRuntime()
    results = execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=runtime,
        seed=1,
    )
    assert len(results) == 5
    assert sum(len(item.issues) for item in results) == 20
    assert all(item.baseline.status == "passed" for item in results)
    assert all(
        value.status == "observed" for item in results for value in item.issues
    )
    assert runtime.progress[0].startswith("qualification started")
    assert runtime.progress[-1] == "qualification runtime completed"
    assert any("endpoint complete" in message for message in runtime.progress)
    assert any("trace contract verified" in message for message in runtime.progress)


def test_runner_parallelizes_agents_but_not_versions_within_an_agent() -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = select_daily(date(2026, 8, 24), agents, issues, hashes["issues"])
    runtime = FakeRuntime(probe_concurrency=True)
    execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=runtime,
        seed=1,
    )
    assert runtime.maximum_concurrent_agents > 1


def test_runner_staggers_agent_start_burst(monkeypatch) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = select_daily(date(2026, 8, 24), agents, issues, hashes["issues"])
    sleeps = []
    monkeypatch.setattr(
        "agent_insights_quality.runner.time.sleep",
        sleeps.append,
    )
    execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=FakeRuntime(),
        seed=1,
        agent_start_stagger_seconds=2,
    )
    assert sorted(sleeps) == [2, 4, 6, 8]


def test_telemetry_recovery_reuses_private_invocation_checkpoint(
    tmp_path: Path,
) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = select_daily(date(2026, 8, 24), agents, issues, hashes["issues"])
    target = selected["weather-agent"][0]

    class RecoveringRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.telemetry_attempts: dict[str, int] = {}

        def wait_for_telemetry(
            self,
            *,
            agent_name: str,
            foundry_version: str,
            invocation: InvocationEvidence,
        ) -> tuple[str, ...]:
            self.telemetry_attempts[foundry_version] = (
                self.telemetry_attempts.get(foundry_version, 0) + 1
            )
            if (
                foundry_version == target
                and self.telemetry_attempts[foundry_version] == 1
            ):
                raise ContractError("Synthetic telemetry deadline")
            return super().wait_for_telemetry(
                agent_name=agent_name,
                foundry_version=foundry_version,
                invocation=invocation,
            )

    runtime = RecoveringRuntime()
    execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=runtime,
        seed=1,
        checkpoint_store=VersionCheckpointStore(
            tmp_path / "stages",
            "sha256:" + "d" * 64,
        ),
    )
    assert runtime.invoked.count(target) == 1
    assert runtime.telemetry_attempts[target] == 2


def test_recovery_budget_is_per_agent(tmp_path: Path) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = select_daily(date(2026, 8, 24), agents, issues, hashes["issues"])
    targets = {values[0] for values in selected.values()}

    class RecoveringRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.failed_once = set()

        def wait_for_telemetry(
            self,
            *,
            agent_name: str,
            foundry_version: str,
            invocation: InvocationEvidence,
        ) -> tuple[str, ...]:
            if foundry_version in targets and foundry_version not in self.failed_once:
                self.failed_once.add(foundry_version)
                raise ContractError("Synthetic telemetry deadline")
            return super().wait_for_telemetry(
                agent_name=agent_name,
                foundry_version=foundry_version,
                invocation=invocation,
            )

    runtime = RecoveringRuntime()
    results = execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=runtime,
        seed=1,
        checkpoint_store=VersionCheckpointStore(
            tmp_path / "stages",
            "sha256:" + "d" * 64,
        ),
    )
    assert runtime.failed_once == targets
    assert all(
        result.status == "observed"
        for agent_result in results
        for result in agent_result.issues
    )


def test_insight_poll_recovery_reuses_started_run_checkpoint(
    tmp_path: Path,
) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = select_daily(date(2026, 8, 24), agents, issues, hashes["issues"])
    target = selected["weather-agent"][0]

    class RecoveringRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.starts: dict[str, int] = {}
            self.polls: dict[str, int] = {}

        def start_insights_run(self, **kwargs) -> InsightRunCheckpoint:
            version = kwargs["foundry_version"]
            self.starts[version] = self.starts.get(version, 0) + 1
            return super().start_insights_run(**kwargs)

        def finish_insights_run(self, **kwargs) -> InsightRunEvidence:
            version = kwargs["foundry_version"]
            self.polls[version] = self.polls.get(version, 0) + 1
            if version == target and self.polls[version] == 1:
                raise ContractError("Synthetic Insight polling deadline")
            return super().finish_insights_run(**kwargs)

    runtime = RecoveringRuntime()
    execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=runtime,
        seed=1,
        checkpoint_store=VersionCheckpointStore(
            tmp_path / "stages",
            "sha256:" + "d" * 64,
        ),
    )
    assert runtime.starts[target] == 1
    assert runtime.polls[target] == 2


def test_ambiguous_insight_start_retries_only_after_clean_retraffic(
    tmp_path: Path,
) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = select_daily(date(2026, 8, 24), agents, issues, hashes["issues"])
    target = selected["weather-agent"][0]

    class RecoveringRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.starts: dict[str, int] = {}

        def start_insights_run(self, **kwargs) -> InsightRunCheckpoint:
            version = kwargs["foundry_version"]
            self.starts[version] = self.starts.get(version, 0) + 1
            if version == target and self.starts[version] == 1:
                raise ContractError(
                    "Remote operation failed before a response was received"
                )
            return super().start_insights_run(**kwargs)

    runtime = RecoveringRuntime()
    execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=runtime,
        seed=1,
        checkpoint_store=VersionCheckpointStore(
            tmp_path / "stages",
            "sha256:" + "d" * 64,
        ),
    )
    assert runtime.starts[target] == 2
    assert runtime.invoked.count(target) == 2
    assert runtime.clean_agents.count("weather-agent") == 2
    assert runtime.reset_agents.count("weather-agent") == 2


def test_pending_insight_start_from_crash_forces_clean_retraffic(
    tmp_path: Path,
) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = select_daily(date(2026, 8, 24), agents, issues, hashes["issues"])
    registry = _registry(agents, hashes)
    store = VersionCheckpointStore(
        tmp_path / "stages",
        "sha256:" + "d" * 64,
    )
    entry = registry["agents"]["weather-agent"]["versions"]["v0"]
    checkpoint_args = (
        "weather-agent",
        "v0",
        entry["foundry_version"],
        entry["content_digest"],
    )
    store.save_invocation(
        *checkpoint_args,
        InvocationEvidence(
            operation_ids=(),
            response_references=tuple(
                f"private-response-{index}" for index in range(5)
            ),
            started_at="2026-08-24T10:00:00+00:00",
            completed_at="2026-08-24T10:01:00+00:00",
            request_count=5,
            allow_window_correlation=False,
            response_count=5,
            usable_response_count=5,
            semantic_assertion_count=5,
            semantic_assertions_passed=5,
            request_summaries=tuple(
                RequestCompletionEvidence(
                    request_index=index,
                    response_count=1,
                    usable_response=True,
                    semantic_assertion_count=1,
                    semantic_assertions_passed=1,
                    assertion_results=(
                        SemanticAssertionEvidence("synthetic_contract", True),
                    ),
                    activation_gate=False,
                    direct_terminal_response_count=1,
                    function_call_count=0,
                )
                for index in range(5)
            ),
        ),
    )
    store.save_operation_ids(
        *checkpoint_args,
        tuple(f"{index + 1:032x}" for index in range(5)),
    )
    store.save_trace_verified(*checkpoint_args)
    store.mark_insight_start_pending(*checkpoint_args)

    class RecordingRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.invoked_pairs = []

        def invoke_version(self, **kwargs) -> InvocationEvidence:
            self.invoked_pairs.append(
                (kwargs["agent_name"], kwargs["foundry_version"])
            )
            return super().invoke_version(**kwargs)

    runtime = RecordingRuntime()
    execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=registry,
        runtime=runtime,
        seed=1,
        checkpoint_store=store,
    )
    assert ("weather-agent", "v0") in runtime.invoked_pairs
    assert runtime.clean_agents.count("weather-agent") == 1
    assert runtime.reset_agents.count("weather-agent") == 1


def test_resume_waits_before_first_version_without_checkpoint(
    tmp_path: Path,
) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = select_daily(date(2026, 8, 24), agents, issues, hashes["issues"])
    registry = _registry(agents, hashes)
    store = VersionCheckpointStore(
        tmp_path / "stages",
        "sha256:" + "d" * 64,
    )
    entry = registry["agents"]["weather-agent"]["versions"]["v0"]
    store.save_result(
        "weather-agent",
        "v0",
        entry["foundry_version"],
        entry["content_digest"],
        VersionResult(
            logical_version="v0",
            foundry_version=entry["foundry_version"],
            status="passed",
            endpoint_request_count=1,
            endpoint_response_count=1,
            endpoint_usable_response_count=1,
            trace_contract_verified=True,
        ),
    )
    runtime = FakeRuntime()
    execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=registry,
        runtime=runtime,
        seed=1,
        checkpoint_store=store,
    )
    assert runtime.clean_agents.count("weather-agent") == 1
    assert runtime.reset_agents.count("weather-agent") == 1


def test_baseline_assertion_failure_is_incomplete() -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    registry = _registry(agents, hashes)
    selected = {
        agent["name"]: [agent["issue_ids"][0]] for agent in agents["agents"]
    }

    class FailedBaselineAssertionRuntime(FakeRuntime):
        def invoke_version(self, **kwargs) -> InvocationEvidence:
            evidence = super().invoke_version(**kwargs)
            if kwargs["agent_name"] != "weather-agent" or kwargs[
                "traffic_path"
            ].parent.name != "v0":
                return evidence
            summaries = list(evidence.request_summaries)
            summaries[0] = replace(
                summaries[0],
                semantic_assertions_passed=0,
                assertion_results=(
                    SemanticAssertionEvidence("synthetic_contract", False),
                ),
            )
            return replace(
                evidence,
                semantic_assertions_passed=evidence.semantic_assertions_passed - 1,
                request_summaries=tuple(summaries),
            )

    result = execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=registry,
        runtime=FailedBaselineAssertionRuntime(),
        seed=1,
    )[0]
    assert result.baseline.status == "inconclusive"
    assert result.baseline.error_code == "baseline_assertion_failed"
    assert result.issues[0].status == "skipped_baseline"


def test_failed_prompt_issue_activation_is_incomplete() -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    registry = _registry(agents, hashes)
    selected = {
        agent["name"]: [agent["issue_ids"][0]] for agent in agents["agents"]
    }

    class FailedActivationRuntime(FakeRuntime):
        def invoke_version(self, **kwargs) -> InvocationEvidence:
            evidence = super().invoke_version(**kwargs)
            if kwargs["foundry_version"] != "issue-001":
                return evidence
            summaries = list(evidence.request_summaries)
            summaries[0] = replace(
                summaries[0],
                semantic_assertions_passed=0,
                assertion_results=(
                    SemanticAssertionEvidence("synthetic_contract", False),
                ),
            )
            return replace(
                evidence,
                semantic_assertions_passed=evidence.semantic_assertions_passed - 1,
                request_summaries=tuple(summaries),
            )

    result = execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=registry,
        runtime=FailedActivationRuntime(),
        seed=1,
    )[0]
    assert result.baseline.status == "passed"
    assert result.issues[0].status == "inconclusive"
    assert result.issues[0].error_code == "issue_activation_failed"


def test_failed_hosted_trace_activation_is_incomplete() -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    registry = _registry(agents, hashes)

    class FailedTraceActivationRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.starts = 0
            self.finishes = 0

        def start_insights_run(self, **kwargs):
            self.starts += 1
            return super().start_insights_run(**kwargs)

        def finish_insights_run(self, **kwargs):
            self.finishes += 1
            return super().finish_insights_run(**kwargs)

        def trace_assertion_evidence(self, **kwargs):
            evidence = list(super().trace_assertion_evidence(**kwargs))
            if kwargs["traffic_path"].parent.name == "issue-013":
                assertions = list(evidence[0])
                assertions[0] = TraceAssertionEvidence(
                    assertions[0].assertion,
                    False,
                )
                evidence[0] = tuple(assertions)
            return tuple(evidence)

    runtime = FailedTraceActivationRuntime()
    result = execute(
        agents=agents,
        issues=issues,
        selected={
            agent["name"]: [agent["issue_ids"][0]]
            for agent in agents["agents"]
        },
        registry=registry,
        runtime=runtime,
        seed=1,
    )
    issue = next(
        item for item in result if item.agent_name == "finance-agent"
    ).issues[0]
    assert issue.status == "inconclusive"
    assert issue.error_code == "issue_activation_failed"
    assert issue.trace_assertion_count == 10
    assert issue.trace_assertions_passed == 9
    assert issue.endpoint_request_summaries[0].trace_assertion_results[0] == (
        TraceAssertionEvidence("one_balance_call", False)
    )
    assert issue.insight_references == []
    assert issue.observed_insights == []
    assert runtime.starts > 0
    assert runtime.finishes > 0


def test_unhandled_baseline_error_fails_terminal_evidence() -> None:
    agents, _ = load_catalogs()
    support = next(
        item for item in agents["agents"] if item["name"] == "support-ticket-agent"
    )
    with pytest.raises(ContractError, match="unhandled error"):
        _validate_baseline_trace_evidence(
            agent=support,
            invocation=InvocationEvidence(
                operation_ids=(),
                response_references=("synthetic",),
                started_at="2026-08-24T10:00:00+00:00",
                completed_at="2026-08-24T10:00:01+00:00",
                request_count=1,
                allow_window_correlation=False,
            ),
            trace_evidence={
                "terminal_response_count": 1,
                "terminal_output_count": 1,
                "explicit_terminal_success_count": 1,
                "explicit_terminal_output_count": 1,
                "unhandled_error_count": 1,
            },
        )


def test_issue_failure_does_not_stop_later_versions() -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = select_daily(date(2026, 8, 24), agents, issues, hashes["issues"])
    failed = selected["weather-agent"][1]
    runtime = FakeRuntime(fail=failed)
    results = execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=runtime,
        seed=1,
    )
    weather = next(item for item in results if item.agent_name == "weather-agent")
    assert weather.issues[1].status == "inconclusive"
    assert weather.issues[1].error_code == "invocation_failed"
    assert weather.issues[-1].logical_version in runtime.invoked
    assert weather.issues[-1].status == "observed"


def test_baseline_noise_continues_issue_diagnostics() -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = select_daily(date(2026, 8, 24), agents, issues, hashes["issues"])
    runtime = FakeRuntime(baseline_noise="v0")
    results = execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=runtime,
        seed=1,
    )
    assert all(item.baseline.status == "not_at_bar" for item in results)
    assert all(value.status == "observed" for item in results for value in item.issues)


def test_foreign_operation_card_is_not_persisted(tmp_path: Path) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = select_daily(date(2026, 8, 24), agents, issues, hashes["issues"])
    selected_issue = selected["weather-agent"][0]

    class ForeignOperationCardRuntime(FakeRuntime):
        def finish_insights_run(self, **kwargs) -> InsightRunEvidence:
            result = super().finish_insights_run(**kwargs)
            if kwargs["foundry_version"] == "v0":
                return result
            card = result.insights[0]
            return replace(
                result,
                insights=(
                    replace(
                        card,
                        linked_operation_ids=(
                            *card.linked_operation_ids,
                            "f" * 32,
                        ),
                        trace_count=card.trace_count + 1,
                    ),
                ),
            )

    store = VersionCheckpointStore(
        tmp_path / "stages",
        "sha256:" + "d" * 64,
    )
    results = execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=ForeignOperationCardRuntime(),
        seed=1,
        checkpoint_store=store,
    )
    weather = next(item for item in results if item.agent_name == "weather-agent")
    issue = weather.issues[0]
    entry = _registry(agents, hashes)["agents"]["weather-agent"]["versions"][
        selected_issue
    ]
    persisted = store.result(
        "weather-agent",
        selected_issue,
        entry["foundry_version"],
        entry["content_digest"],
    )

    assert issue.status == "not_at_bar"
    assert issue.error_code == "expected_exactly_one_insight"
    assert issue.insight_references == []
    assert issue.observed_insights == []
    assert issue.observed_insight is None
    assert persisted == issue


def test_baseline_operational_failure_stops_only_one_agent() -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = select_daily(date(2026, 8, 24), agents, issues, hashes["issues"])
    results = execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=FakeRuntime(reset_failure_agent="weather-agent"),
        seed=1,
    )
    weather = next(item for item in results if item.agent_name == "weather-agent")
    assert weather.baseline.status == "inconclusive"
    assert all(value.status == "skipped_baseline" for value in weather.issues)
    assert all(
        item.baseline.status == "passed"
        for item in results
        if item.agent_name != "weather-agent"
    )


def test_clean_window_failure_has_actionable_error_code() -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = select_daily(date(2026, 8, 24), agents, issues, hashes["issues"])
    results = execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=FakeRuntime(clean_window_failure_agent="weather-agent"),
        seed=1,
    )
    weather = next(item for item in results if item.agent_name == "weather-agent")
    assert weather.baseline.error_code == "clean_window_not_empty"
