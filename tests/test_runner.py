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
    linked_operations_match_scope,
)
from agent_insights_quality.runner import (
    _RecoveryBudget,
    _StartStagger,
    _baseline_recovery_is_safe,
    _execute_version,
    _execute_version_with_recovery,
    _required_trace_operations,
    _validate_baseline_trace_evidence,
    execute,
    execute_agent,
)
from agent_insights_quality.runtime_state import VersionCheckpointStore
from agent_insights_quality.selection import select_daily
from agent_insights_quality.util import ROOT, ContractError, InsightWindowExpiredError
from agent_insights_quality.validation_rules import execution_requests


def _registry(agents: dict, hashes: dict[str, str]) -> dict:
    return {
        "profile": "daily",
        "project_name": "agent-insights-quality",
        "test_region": "WestUS2",
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
        self.hosted_stabilizations: list[tuple[str, str, bool]] = []

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
        requests: list[dict] | None = None,
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
        requests = requests or execution_requests(traffic_path)
        request_count = len(requests)
        summaries = tuple(
            RequestCompletionEvidence(
                request_index=index,
                response_count=1,
                usable_response=True,
                semantic_assertion_count=1,
                semantic_assertions_passed=1,
                assertion_results=(
                    SemanticAssertionEvidence(
                        "synthetic_contract",
                        True,
                        True,
                    ),
                ),
                activation_gate=bool(
                    request.get("expected", {}).get("activation_gate")
                ),
                direct_terminal_response_count=int(agent_type == "prompt"),
                function_call_count=0,
            )
            for index, request in enumerate(requests)
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
        on_stable,
        on_maturity_proof=None,
        requests: list[dict] | None = None,
        minimum_passing_trace_observations: int | None = None,
    ) -> tuple[tuple[TraceAssertionEvidence, ...], ...]:
        del minimum_passing_trace_observations, on_maturity_proof
        requests = requests or execution_requests(traffic_path)
        assert agent_name.endswith("-agent")
        assert foundry_version
        assert len(operation_ids) == len(response_references)
        assert window_start < window_end
        assert stabilization_seconds == 180
        self.hosted_stabilizations.append(
            (
                agent_name,
                foundry_version,
                any(
                    request["expected"].get("trace_assertions")
                    for request in requests
                ),
            )
        )
        on_first_pass()
        on_stable(self.trace_behavior_evidence(operation_ids))
        return tuple(
            tuple(
                TraceAssertionEvidence(item["name"], True)
                for item in request["expected"].get("trace_assertions", [])
            )
            for request in requests
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
        intent_reference: str,
        persist,
    ) -> InsightRunCheckpoint:
        del agent_name, monitor_id, foundry_version, operation_ids
        assert lookback_hours == 0.1
        assert start_margin_seconds == 30
        assert intent_reference.startswith("sha256:")
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
        validate_window: bool = True,
    ) -> InsightRunEvidence:
        del agent_name, monitor_id
        del validate_window
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

    def discover_insights_run(self, **kwargs):
        del kwargs
        return "absent", None

    def verify_trace_contract(
        self,
        *,
        agent_name: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        required_operations_by_request: tuple[tuple[str, ...], ...],
        window_start: str,
        window_end: str,
    ) -> None:
        assert agent_name.endswith("-agent")
        assert foundry_version
        assert operation_ids
        assert len(required_operations_by_request) == len(operation_ids)
        assert all(
            "invoke_agent" in operations
            for operations in required_operations_by_request
        )
        assert window_start < window_end


def _timed_trace_row(
    tool_name: str,
    *,
    timestamp: str,
    operation_id: str | None = None,
    matched_reference: str = "issue-synthetic-0",
) -> dict:
    return {
        "operation_id": operation_id or f"{1:032x}",
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
        "matched_reference": matched_reference,
    }


class TimedTraceRuntime(FakeRuntime):
    def __init__(
        self,
        rows_at,
        *,
        agent_name: str = "finance-agent",
        foundry_version: str = "issue-synthetic",
        baseline: bool = False,
        finish_failures: int = 0,
    ) -> None:
        super().__init__()
        self.monotonic = 0.0
        self.wall = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
        self.rows_at = rows_at
        self.agent_name = agent_name
        self.foundry_version = foundry_version
        self.baseline = baseline
        self.starts: list[tuple[float, datetime]] = []
        self.finishes = 0
        self.finish_failures = finish_failures
        self.trace_behavior_times: list[float] = []
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
        assert operation_ids == tuple(
            f"{index:032x}" for index in range(1, len(operation_ids) + 1)
        )
        assert response_references == tuple(
            f"{self.foundry_version}-{index}"
            for index in range(len(response_references))
        )
        assert foundry_version == self.foundry_version
        assert agent_name == self.agent_name
        assert window_start == "2026-08-24T10:00:00+00:00"
        assert window_end == "2026-08-24T10:01:00+00:00"
        rows = self.rows_at(self.monotonic)
        anchors = {}
        selected = []
        for index, row in enumerate(rows, start=1):
            operation_id = row["operation_id"]
            reference = row["matched_reference"]
            key = (operation_id, reference)
            if key not in anchors:
                anchor_id = f"anchor-{len(anchors) + 1}"
                anchors[key] = anchor_id
                selected.append(
                    {
                        **row,
                        "span_id": anchor_id,
                        "parent_span_id": "",
                        "operation_name": "invoke_agent",
                        "tool_name": "",
                        "matched_reference": reference,
                        "agent_name": self.agent_name,
                        "agent_version": self.foundry_version,
                    }
                )
            selected.append(
                {
                    **row,
                    "span_id": f"tool-{index}",
                    "parent_span_id": anchors[key],
                    "matched_reference": "",
                }
            )
        return selected

    def trace_behavior_evidence(self, operation_ids: tuple[str, ...]) -> dict:
        self.trace_behavior_times.append(self.monotonic)
        return super().trace_behavior_evidence(operation_ids)

    def trace_assertion_evidence(self, **kwargs):
        on_stable = kwargs["on_stable"]
        kwargs["on_stable"] = lambda _evidence: on_stable(
            self.trace_behavior_evidence(kwargs["operation_ids"])
        )
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
        evidence = super().finish_insights_run(**kwargs)
        return replace(evidence, insights=()) if self.baseline else evidence


def _write_timed_trace_traffic(
    path: Path,
    *,
    baseline: bool,
    with_trace_assertions: bool = True,
    activation_gate: bool | None = None,
) -> None:
    attempt_count = 10 if baseline else 1
    expected = {"http_status": 200}
    if activation_gate is None:
        activation_gate = with_trace_assertions
    if activation_gate:
        expected["activation_gate"] = True
    if with_trace_assertions:
        expected["trace_assertions"] = [
            {
                "name": "one_lookup",
                "kind": "tool_call_count",
                "tool_name": "lookup",
                "count": 1,
            }
        ]
    requests = [
        {
            "id": f"request_A1b2C3d4_{index:02d}",
            "request": {"body": {"input": f"synthetic request {index}"}},
            "expected": expected,
        }
        for index in range(1, attempt_count + 1)
    ]
    path.write_text(
        json.dumps(
            {
                "requests": requests,
                "validation_rules": {
                    "schema_version": "1.0.0",
                    "scenarios": [
                        {
                            "id": "synthetic-scenario",
                            "validation_mode": (
                                "baseline" if baseline else "deterministic"
                            ),
                            "n": attempt_count,
                            "k": 6 if baseline else 1,
                            "fixtures": [],
                            "attempts": [
                                {
                                    "index": index,
                                    "conversation_group": (
                                        f"synthetic-attempt-{index:02d}"
                                    ),
                                    "parameters": {
                                        "case_id": f"case-{index:02d}"
                                    },
                                    "setup_steps": [],
                                    "probe_steps": [
                                        {
                                            **request,
                                            "id": (
                                                f"synthetic-probe-{index:02d}"
                                            ),
                                        }
                                    ],
                                }
                                for index, request in enumerate(
                                    requests,
                                    start=1,
                                )
                            ],
                            "healthy_predicate": (
                                {"kind": "all_probe_assertions_pass"}
                                if baseline
                                else None
                            ),
                            "defect_predicate": (
                                {"kind": "never"}
                                if baseline
                                else {
                                    "kind": "all_observation_steps_pass",
                                    "step_ids": ["synthetic-probe-01"],
                                    "required_surfaces": (
                                        ["trace"] if with_trace_assertions else []
                                    ),
                                }
                            ),
                            "v0_control_predicate": (
                                None
                                if baseline
                                else {"kind": "zero_defect_observations"}
                            ),
                            "execution_digest": "sha256:" + "b" * 64,
                        }
                    ],
                    "execution_digest": "sha256:" + "c" * 64,
                },
            }
        ),
        encoding="utf-8",
    )


def _timed_version_kwargs(
    tmp_path: Path,
    runtime: TimedTraceRuntime,
    checkpoint_store: VersionCheckpointStore,
    *,
    agent_name: str = "finance-agent",
    foundry_version: str = "issue-synthetic",
    baseline: bool = False,
    with_trace_assertions: bool = True,
    agent_type: str = "hosted_code",
    activation_gate: bool | None = None,
) -> dict:
    traffic_path = tmp_path / f"{agent_name}-{foundry_version}-traffic.json"
    _write_timed_trace_traffic(
        traffic_path,
        baseline=baseline,
        with_trace_assertions=with_trace_assertions,
        activation_gate=activation_gate,
    )
    return {
        "runtime": runtime,
        "agent": {
            "name": agent_name,
            "type": agent_type,
            "baseline_contract": {
                "request_count": 10 if baseline else 1,
                "terminal_response": (
                    "direct_prompt"
                    if agent_type == "prompt"
                    else "standard_assistant_message"
                ),
                "semantic_assertions": "required",
                "trace_operations": "uniform",
            },
        },
        "monitor_id": f"monitor-{agent_name}",
        "logical_version": "v0" if baseline else foundry_version,
        "registry_entry": {
            "foundry_version": foundry_version,
            "content_digest": "sha256:" + "a" * 64,
        },
        "traffic_path": traffic_path,
        "seed": 1,
        "expected": (
            None
            if baseline
            else {
                "trace_contract": {
                    "operations": ["invoke_agent"],
                    "minimum_traces": 1,
                }
            }
        ),
        "lookback_hours": 0.1,
        "trace_assertion_stabilization_seconds": 180,
        "insight_start_margin_seconds": 30,
        "checkpoint_store": checkpoint_store,
        "start_stagger": _StartStagger(0),
    }


def test_support_baseline_uses_request_bound_trace_operations() -> None:
    agents, _ = load_catalogs()
    support = next(
        agent
        for agent in agents["agents"]
        if agent["name"] == "support-ticket-agent"
    )

    operations = _required_trace_operations(
        agent=support,
        expected=None,
        traffic_path=ROOT / support["baseline_path"] / "traffic.json",
        request_count=len(
            execution_requests(ROOT / support["baseline_path"] / "traffic.json")
        ),
    )

    assert operations[::2] == (("invoke_agent", "chat"),) * 10
    assert operations[1:8:2] == (
        ("invoke_agent", "execute_tool", "chat"),
    ) * 4
    assert operations[9] == ("invoke_agent", "execute_tool")


def test_first_exact_hosted_mapping_starts_insights_before_stabilization(
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
    assert [start[0] for start in runtime.starts] == [0]
    assert runtime.starts[0][0] < 360
    assert runtime.monotonic == 315
    assert runtime.finishes == 1


def test_exact_hosted_mapping_starts_before_slow_trace_contract(
    tmp_path: Path,
) -> None:
    first = [_timed_trace_row("lookup", timestamp="2026-08-29T12:00:00Z")]

    class SlowTraceContractRuntime(TimedTraceRuntime):
        def verify_trace_contract(self, **kwargs) -> None:
            assert [start[0] for start in self.starts] == [0]
            self._advance(600)
            super().verify_trace_contract(**kwargs)

    runtime = SlowTraceContractRuntime(lambda _elapsed: first)
    store = VersionCheckpointStore(tmp_path / "stages", "sha256:" + "d" * 64)

    result = _execute_version(**_timed_version_kwargs(tmp_path, runtime, store))

    assert result.status == "observed"
    assert [start[0] for start in runtime.starts] == [0]
    assert runtime.monotonic == 780
    assert runtime.trace_behavior_times == [780]
    assert runtime.finishes == 1


def test_preverification_failure_quarantines_without_claiming_trace_proof(
    tmp_path: Path,
) -> None:
    class FailedTraceContractRuntime(TimedTraceRuntime):
        def verify_trace_contract(self, **kwargs) -> None:
            del kwargs
            raise ContractError("synthetic trace contract failure")

    runtime = FailedTraceContractRuntime(lambda _elapsed: [])
    store = VersionCheckpointStore(tmp_path / "stages", "sha256:" + "d" * 64)
    kwargs = _timed_version_kwargs(tmp_path, runtime, store)

    result = _execute_version_with_recovery(
        **kwargs,
        clean_window_poll_seconds=15,
        clean_window_ingestion_margin_seconds=30,
        clean_window_max_wait_seconds=1200,
        recovery_budget=_RecoveryBudget(0),
    )

    assert result.status == "inconclusive"
    assert result.error_code == "trace_contract_failed"
    assert result.trace_contract_verified is False
    assert [start[0] for start in runtime.starts] == [0]
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


def test_unstabilized_late_assertion_pass_is_incomplete_without_retraffic(
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
    assert [start[0] for start in runtime.starts] == [0]
    assert runtime.monotonic == 15 * 60
    assert runtime.finishes == 1
    assert runtime.invoked.count("issue-synthetic") == 1
    assert runtime.clean_agents == []
    assert runtime.reset_agents == []


_HOSTED_NO_ASSERTION_CASES = (
    ("finance-agent", "finance-baseline", True),
    ("travel-agent", "travel-baseline", True),
    ("support-ticket-agent", "support-baseline", True),
    ("travel-agent", "issue-021", False),
    ("support-ticket-agent", "issue-029", False),
)


@pytest.mark.parametrize(
    ("agent_name", "foundry_version", "baseline"),
    _HOSTED_NO_ASSERTION_CASES,
)
def test_hosted_no_assertion_mapping_stabilizes_before_cards(
    tmp_path: Path,
    agent_name: str,
    foundry_version: str,
    baseline: bool,
) -> None:
    attempt_count = 10 if baseline else 1
    first = [
        _timed_trace_row(
            "lookup",
            timestamp="2026-08-29T12:00:00Z",
            operation_id=f"{index:032x}",
            matched_reference=f"{foundry_version}-{index - 1}",
        )
        for index in range(1, attempt_count + 1)
    ]
    runtime = TimedTraceRuntime(
        lambda _elapsed: first,
        agent_name=agent_name,
        foundry_version=foundry_version,
        baseline=baseline,
    )
    store = VersionCheckpointStore(tmp_path / "stages", "sha256:" + "d" * 64)
    kwargs = _timed_version_kwargs(
        tmp_path,
        runtime,
        store,
        agent_name=agent_name,
        foundry_version=foundry_version,
        baseline=baseline,
        with_trace_assertions=False,
    )

    result = _execute_version(**kwargs)
    resumed = _execute_version(**kwargs)

    assert resumed == result
    assert result.status == ("passed" if baseline else "observed")
    assert result.trace_assertion_count == 0
    assert result.trace_assertions_passed == 0
    assert all(
        summary.trace_assertion_results == ()
        for summary in result.endpoint_request_summaries
    )
    assert [start[0] for start in runtime.starts] == [0]
    assert runtime.monotonic == 180
    assert runtime.finishes == 1
    assert runtime.trace_behavior_times == [180]


@pytest.mark.parametrize(
    ("agent_name", "foundry_version", "baseline"),
    _HOSTED_NO_ASSERTION_CASES,
)
def test_hosted_no_assertion_late_operation_is_drained_and_resume_is_idempotent(
    tmp_path: Path,
    agent_name: str,
    foundry_version: str,
    baseline: bool,
) -> None:
    attempt_count = 10 if baseline else 1
    first = [
        _timed_trace_row(
            "lookup",
            timestamp="2026-08-29T12:00:00Z",
            operation_id=f"{index:032x}",
            matched_reference=f"{foundry_version}-{index - 1}",
        )
        for index in range(1, attempt_count + 1)
    ]
    duplicate = _timed_trace_row(
        "lookup",
        timestamp="2026-08-29T12:02:15Z",
        operation_id=f"{attempt_count + 1:032x}",
        matched_reference=f"{foundry_version}-0",
    )
    runtime = TimedTraceRuntime(
        lambda elapsed: first if elapsed < 135 else [*first, duplicate],
        agent_name=agent_name,
        foundry_version=foundry_version,
        baseline=baseline,
    )
    store = VersionCheckpointStore(tmp_path / "stages", "sha256:" + "d" * 64)
    kwargs = _timed_version_kwargs(
        tmp_path,
        runtime,
        store,
        agent_name=agent_name,
        foundry_version=foundry_version,
        baseline=baseline,
        with_trace_assertions=False,
    )

    result = _execute_version(**kwargs)
    resumed = _execute_version(**kwargs)

    assert resumed == result
    assert result.status == "inconclusive"
    assert result.error_code == (
        "baseline_evidence_failed" if baseline else "issue_activation_failed"
    )
    assert result.insight_references == []
    assert result.observed_insights == []
    assert [start[0] for start in runtime.starts] == [0]
    assert runtime.monotonic == 135
    assert runtime.finishes == 1
    assert runtime.invoked.count(foundry_version) == 1


def test_prompt_path_does_not_run_hosted_stabilization(tmp_path: Path) -> None:
    class PromptRuntime(TimedTraceRuntime):
        def trace_assertion_evidence(self, **kwargs):
            del kwargs
            pytest.fail("Prompt traffic must not enter Hosted stabilization")

    runtime = PromptRuntime(
        lambda _elapsed: [],
        agent_name="weather-agent",
        foundry_version="issue-prompt-synthetic",
    )
    store = VersionCheckpointStore(tmp_path / "stages", "sha256:" + "d" * 64)

    result = _execute_version(
        **_timed_version_kwargs(
            tmp_path,
            runtime,
            store,
            agent_name="weather-agent",
            foundry_version="issue-prompt-synthetic",
            with_trace_assertions=False,
            agent_type="prompt",
            activation_gate=True,
        )
    )

    assert result.status == "observed"
    assert [start[0] for start in runtime.starts] == [0]
    assert runtime.monotonic == 0


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
    expected_hosted_stabilizations = {
        (agent["name"], logical_version)
        for agent in agents["agents"]
        if agent["type"] != "prompt"
        for logical_version in ("v0", *selected[agent["name"]])
    }
    assert {
        (agent_name, foundry_version)
        for agent_name, foundry_version, _ in runtime.hosted_stabilizations
    } == expected_hosted_stabilizations
    assert len(runtime.hosted_stabilizations) == 15
    assert ("finance-agent", "v0", True) in runtime.hosted_stabilizations
    assert ("travel-agent", "v0", False) in runtime.hosted_stabilizations
    assert ("support-ticket-agent", "v0", True) in runtime.hosted_stabilizations
    assert all(
        agent_name not in {"weather-agent", "healthcare-agent"}
        for agent_name, _, _ in runtime.hosted_stabilizations
    )
    assert runtime.invoked == [
        logical_version
        for agent in agents["agents"]
        for logical_version in ("v0", *selected[agent["name"]])
    ]


def test_runner_executes_agents_sequentially_without_internal_fanout() -> None:
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
    assert runtime.maximum_concurrent_agents == 1


@pytest.mark.parametrize(
    ("agent_name", "issue_id", "surface"),
    [
        ("weather-agent", "issue-004", "semantic"),
        ("healthcare-agent", "issue-010", "semantic"),
        ("finance-agent", "issue-019", "trace"),
    ],
)
def test_all_issue_modes_use_six_of_ten_reviewed_observations(
    agent_name: str,
    issue_id: str,
    surface: str,
) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)

    class ThresholdRuntime(FakeRuntime):
        def invoke_version(self, **kwargs) -> InvocationEvidence:
            evidence = super().invoke_version(**kwargs)
            if kwargs["foundry_version"] != issue_id or surface != "semantic":
                return evidence
            summaries = list(evidence.request_summaries)
            observation_indexes = [
                index
                for index, summary in enumerate(summaries)
                if summary.activation_gate
            ]
            for index in observation_indexes[-4:]:
                summaries[index] = replace(
                    summaries[index],
                    semantic_assertions_passed=0,
                    assertion_results=(
                        SemanticAssertionEvidence(
                            "synthetic_contract",
                            False,
                            True,
                        ),
                    ),
                )
            return replace(
                evidence,
                semantic_assertions_passed=evidence.semantic_assertions_passed - 4,
                request_summaries=tuple(summaries),
            )

        def trace_assertion_evidence(self, **kwargs):
            results = list(super().trace_assertion_evidence(**kwargs))
            if kwargs["foundry_version"] != issue_id or surface != "trace":
                return tuple(results)
            requests = execution_requests(kwargs["traffic_path"])
            observation_indexes = [
                index
                for index, request in enumerate(requests)
                if request["expected"]["activation_gate"]
            ]
            for index in observation_indexes[-4:]:
                assertions = list(results[index])
                assertions[0] = replace(assertions[0], passed=False)
                results[index] = tuple(assertions)
            return tuple(results)

    runtime = ThresholdRuntime()
    result = execute_agent(
        agent_name=agent_name,
        agents=agents,
        issues=issues,
        selected={agent_name: [issue_id]},
        registry=_registry(agents, hashes),
        runtime=runtime,
        seed=1,
    )

    baseline_observations = [
        summary
        for summary in result.baseline.endpoint_request_summaries
        if summary.activation_gate
    ]
    issue_observations = [
        summary
        for summary in result.issues[0].endpoint_request_summaries
        if summary.activation_gate
    ]
    observed = sum(
        (
            summary.semantic_assertions_passed
            == summary.semantic_assertion_count
            if surface == "semantic"
            else summary.trace_assertions_passed == summary.trace_assertion_count
        )
        for summary in issue_observations
    )
    assert result.baseline.status == "passed"
    assert len(baseline_observations) == 10
    assert result.issues[0].status == "inconclusive"
    assert len(issue_observations) == 10
    assert observed == 6
    assert runtime.invoked == ["v0", issue_id]


def test_issue_six_of_ten_does_not_resample_five_observations() -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    issue_id = "issue-004"

    class FiveOfTenRuntime(FakeRuntime):
        def invoke_version(self, **kwargs) -> InvocationEvidence:
            evidence = super().invoke_version(**kwargs)
            if kwargs["foundry_version"] != issue_id:
                return evidence
            summaries = list(evidence.request_summaries)
            observation_indexes = [
                index
                for index, summary in enumerate(summaries)
                if summary.activation_gate
            ]
            for index in observation_indexes[-5:]:
                summaries[index] = replace(
                    summaries[index],
                    semantic_assertions_passed=0,
                    assertion_results=(
                        SemanticAssertionEvidence(
                            "synthetic_contract",
                            False,
                            True,
                        ),
                    ),
                )
            return replace(
                evidence,
                semantic_assertions_passed=evidence.semantic_assertions_passed - 5,
                request_summaries=tuple(summaries),
            )

    runtime = FiveOfTenRuntime()
    result = execute_agent(
        agent_name="weather-agent",
        agents=agents,
        issues=issues,
        selected={"weather-agent": [issue_id]},
        registry=_registry(agents, hashes),
        runtime=runtime,
        seed=1,
    )

    issue = result.issues[0]
    observations = [
        summary
        for summary in issue.endpoint_request_summaries
        if summary.activation_gate
    ]
    assert issue.status == "inconclusive"
    assert issue.error_code == "issue_activation_failed"
    assert len(observations) == 10
    assert sum(
        summary.semantic_assertions_passed == summary.semantic_assertion_count
        for summary in observations
    ) == 5
    assert runtime.invoked == ["v0", issue_id]


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


def test_persistent_insight_poll_failure_exhausts_recovery_without_restart(
    tmp_path: Path,
) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = {
        agent["name"]: [agent["issue_ids"][0]] for agent in agents["agents"]
    }
    selected["weather-agent"].append(
        next(
            issue_id
            for issue_id in next(
                agent
                for agent in agents["agents"]
                if agent["name"] == "weather-agent"
            )["issue_ids"]
            if issue_id != selected["weather-agent"][0]
        )
    )
    target = selected["weather-agent"][0]
    blocked = selected["weather-agent"][1]

    class FailingRuntime(FakeRuntime):
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
            if version == target:
                raise ContractError("Synthetic Insight polling deadline")
            return super().finish_insights_run(**kwargs)

    runtime = FailingRuntime()
    store = VersionCheckpointStore(
        tmp_path / "stages",
        "sha256:" + "d" * 64,
    )
    results = execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=runtime,
        seed=1,
        max_recovery_versions=2,
        checkpoint_store=store,
    )
    weather = next(item for item in results if item.agent_name == "weather-agent")

    assert weather.issues[0].status == "inconclusive"
    assert weather.issues[0].error_code == "insight_run_poll_failed_timeout"
    assert weather.issues[1].status == "inconclusive"
    assert weather.issues[1].error_code == "previous_insight_run_unaccounted"
    assert runtime.starts[target] == 1
    assert runtime.polls[target] == 4
    assert blocked not in runtime.invoked

    class DrainingRuntime(FakeRuntime):
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
            return super().finish_insights_run(**kwargs)

    resumed = DrainingRuntime()
    resumed_results = execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=resumed,
        seed=1,
        max_recovery_versions=2,
        checkpoint_store=store,
    )
    resumed_weather = next(
        item for item in resumed_results if item.agent_name == "weather-agent"
    )

    assert resumed_weather.issues[0].status == "inconclusive"
    assert resumed_weather.issues[0].error_code == "insight_run_poll_failed_timeout"
    assert resumed_weather.issues[1].status == "observed"
    assert resumed.starts.get(target, 0) == 0
    assert resumed.polls[target] == 1
    assert target not in resumed.invoked
    assert blocked in resumed.invoked


def test_terminal_failed_insight_retries_only_with_clean_retraffic(
    tmp_path: Path,
) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = {
        agent["name"]: [agent["issue_ids"][0]] for agent in agents["agents"]
    }
    weather_agent = next(
        agent for agent in agents["agents"] if agent["name"] == "weather-agent"
    )
    selected["weather-agent"].append(weather_agent["issue_ids"][1])
    target, later = selected["weather-agent"]

    class TerminalRetryRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.starts: dict[str, int] = {}
            self.finishes: dict[str, int] = {}

        def start_insights_run(self, **kwargs) -> InsightRunCheckpoint:
            version = kwargs["foundry_version"]
            self.starts[version] = self.starts.get(version, 0) + 1
            return super().start_insights_run(**kwargs)

        def finish_insights_run(self, **kwargs) -> InsightRunEvidence:
            version = kwargs["foundry_version"]
            self.finishes[version] = self.finishes.get(version, 0) + 1
            evidence = super().finish_insights_run(**kwargs)
            if version == target and self.finishes[version] == 1:
                return replace(evidence, status="failed", insights=())
            return evidence

    runtime = TerminalRetryRuntime()
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
    weather = next(item for item in results if item.agent_name == "weather-agent")

    assert [item.status for item in weather.issues] == ["observed", "observed"]
    assert runtime.starts[target] == 2
    assert runtime.finishes[target] == 2
    assert runtime.invoked.count(target) == 2
    assert later in runtime.invoked
    assert runtime.clean_agents.count("weather-agent") == 2
    assert runtime.reset_agents.count("weather-agent") == 2


def test_ambiguous_insight_start_retries_only_after_stable_no_run_proof(
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
    first = execute(
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
    weather = next(item for item in first if item.agent_name == "weather-agent")
    assert weather.issues[0].error_code == "insight_run_start_unresolved"
    resumed = execute(
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
    resumed_weather = next(
        item for item in resumed if item.agent_name == "weather-agent"
    )
    assert resumed_weather.issues[0].status == "observed"
    assert runtime.starts[target] == 2
    assert runtime.invoked.count(target) == 1
    assert runtime.clean_agents.count("weather-agent") == 2
    assert runtime.reset_agents.count("weather-agent") == 1


def test_exhausted_ambiguous_start_blocks_until_resume_reconciliation(
    tmp_path: Path,
) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = {
        agent["name"]: [agent["issue_ids"][0]] for agent in agents["agents"]
    }
    weather_agent = next(
        agent for agent in agents["agents"] if agent["name"] == "weather-agent"
    )
    selected["weather-agent"].append(weather_agent["issue_ids"][1])
    target, blocked = selected["weather-agent"]

    class AmbiguousStartRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.starts: dict[str, int] = {}

        def start_insights_run(self, **kwargs) -> InsightRunCheckpoint:
            version = kwargs["foundry_version"]
            self.starts[version] = self.starts.get(version, 0) + 1
            if version == target:
                raise ContractError(
                    "Remote operation failed before a response was received"
                )
            return super().start_insights_run(**kwargs)

    store = VersionCheckpointStore(
        tmp_path / "stages",
        "sha256:" + "d" * 64,
    )
    runtime = AmbiguousStartRuntime()
    results = execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=runtime,
        seed=1,
        max_recovery_versions=2,
        checkpoint_store=store,
    )
    weather = next(item for item in results if item.agent_name == "weather-agent")

    assert weather.issues[0].error_code == "insight_run_start_unresolved"
    assert weather.issues[1].error_code == "previous_insight_run_unaccounted"
    assert runtime.starts[target] == 1
    assert runtime.invoked.count(target) == 1
    assert blocked not in runtime.invoked

    resumed = FakeRuntime()
    resumed_results = execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=resumed,
        seed=1,
        max_recovery_versions=2,
        checkpoint_store=store,
    )
    resumed_weather = next(
        item for item in resumed_results if item.agent_name == "weather-agent"
    )

    assert resumed_weather.issues[0].status == "observed"
    assert resumed_weather.issues[1].status == "observed"
    assert target not in resumed.invoked
    assert blocked in resumed.invoked
    assert resumed.clean_agents.count("weather-agent") == 1
    assert resumed.reset_agents.count("weather-agent") == 0


def test_ambiguous_start_remains_quarantined_when_clean_recovery_fails(
    tmp_path: Path,
) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = {
        agent["name"]: [agent["issue_ids"][0]] for agent in agents["agents"]
    }
    weather_agent = next(
        agent for agent in agents["agents"] if agent["name"] == "weather-agent"
    )
    selected["weather-agent"].append(weather_agent["issue_ids"][1])
    target, blocked = selected["weather-agent"]

    class FailedCleanRecoveryRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.weather_clean_attempts = 0

        def start_insights_run(self, **kwargs) -> InsightRunCheckpoint:
            if kwargs["foundry_version"] == target:
                raise ContractError(
                    "Remote operation failed before a response was received"
                )
            return super().start_insights_run(**kwargs)

        def wait_for_clean_window(
            self,
            agent_name: str,
            lookback_hours: float,
            **kwargs,
        ) -> None:
            if agent_name == "weather-agent":
                self.weather_clean_attempts += 1
                if self.weather_clean_attempts == 2:
                    raise RuntimeError("synthetic recovery clean-window failure")
            super().wait_for_clean_window(agent_name, lookback_hours, **kwargs)

    store = VersionCheckpointStore(
        tmp_path / "stages",
        "sha256:" + "d" * 64,
    )
    runtime = FailedCleanRecoveryRuntime()
    results = execute(
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=runtime,
        seed=1,
        checkpoint_store=store,
    )
    weather = next(item for item in results if item.agent_name == "weather-agent")
    entry = _registry(agents, hashes)["agents"]["weather-agent"]["versions"][target]

    assert weather.issues[0].status == "inconclusive"
    assert weather.issues[1].error_code == "previous_insight_run_unaccounted"
    assert blocked not in runtime.invoked
    assert store.insight_start_pending(
        "weather-agent",
        target,
        entry["foundry_version"],
        entry["content_digest"],
    )


def test_pending_insight_start_from_crash_reuses_endpoint_evidence(
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
    weather = next(
        agent for agent in agents["agents"] if agent["name"] == "weather-agent"
    )
    baseline_requests = execution_requests(
        ROOT / weather["baseline_path"] / "traffic.json"
    )
    request_count = len(baseline_requests)
    store.save_invocation(
        *checkpoint_args,
        InvocationEvidence(
            operation_ids=(),
            response_references=tuple(
                f"private-response-{index}" for index in range(request_count)
            ),
            started_at="2026-08-24T10:00:00+00:00",
            completed_at="2026-08-24T10:01:00+00:00",
            request_count=request_count,
            allow_window_correlation=False,
            response_count=request_count,
            usable_response_count=request_count,
            semantic_assertion_count=request_count,
            semantic_assertions_passed=request_count,
            request_summaries=tuple(
                RequestCompletionEvidence(
                    request_index=index,
                    response_count=1,
                    usable_response=True,
                    semantic_assertion_count=1,
                    semantic_assertions_passed=1,
                    assertion_results=(
                        SemanticAssertionEvidence(
                            "synthetic_contract",
                            True,
                            True,
                        ),
                    ),
                    activation_gate=bool(
                        request["expected"]["activation_gate"]
                    ),
                    direct_terminal_response_count=1,
                    function_call_count=0,
                )
                for index, request in enumerate(baseline_requests)
            ),
        ),
    )
    store.save_operation_ids(
        *checkpoint_args,
        tuple(f"{index + 1:032x}" for index in range(request_count)),
    )
    store.save_trace_verified(*checkpoint_args)
    store.mark_insight_start_pending(*checkpoint_args)
    store.save_result(
        *checkpoint_args,
        VersionResult(
            logical_version="v0",
            foundry_version=entry["foundry_version"],
            status="inconclusive",
            operation_ids=[
                f"{index + 1:032x}" for index in range(request_count)
            ],
            error_code="insight_run_start_unresolved",
        ),
    )

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
    assert ("weather-agent", "v0") not in runtime.invoked_pairs
    assert runtime.clean_agents.count("weather-agent") == 2
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
    cached_baseline = execute_agent(
        agent_name="weather-agent",
        agents=agents,
        issues=issues,
        selected={"weather-agent": [selected["weather-agent"][0]]},
        registry=registry,
        runtime=FakeRuntime(),
        seed=1,
    ).baseline
    store.save_result(
        "weather-agent",
        "v0",
        entry["foundry_version"],
        entry["content_digest"],
        cached_baseline,
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


def test_cached_five_attempt_evidence_is_not_rescaled_to_seven(
    tmp_path: Path,
) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    registry = _registry(agents, hashes)
    weather = next(
        agent for agent in agents["agents"] if agent["name"] == "weather-agent"
    )
    issue = next(item for item in issues["issues"] if item["id"] == "issue-004")
    entry = registry["agents"]["weather-agent"]["versions"]["issue-004"]
    store = VersionCheckpointStore(
        tmp_path / "stages",
        "sha256:" + "d" * 64,
    )
    store.save_result(
        "weather-agent",
        "issue-004",
        entry["foundry_version"],
        entry["content_digest"],
        VersionResult(
            logical_version="issue-004",
            foundry_version=entry["foundry_version"],
            status="observed",
            endpoint_request_count=5,
            endpoint_response_count=5,
            endpoint_usable_response_count=5,
            endpoint_request_summaries=[
                RequestCompletionEvidence(
                    request_index=index,
                    response_count=1,
                    usable_response=True,
                    semantic_assertion_count=1,
                    semantic_assertions_passed=1,
                    assertion_results=(
                        SemanticAssertionEvidence(
                            "synthetic_contract",
                            True,
                            True,
                        ),
                    ),
                    activation_gate=True,
                    direct_terminal_response_count=1,
                    function_call_count=0,
                )
                for index in range(5)
            ],
        ),
    )
    runtime = FakeRuntime()

    with pytest.raises(ContractError, match="current execution plan"):
        _execute_version(
            runtime=runtime,
            agent=weather,
            monitor_id="monitor-weather-agent",
            logical_version="issue-004",
            registry_entry=entry,
            traffic_path=ROOT / issue["implementation"] / "traffic.json",
            seed=1,
            expected=issue,
            lookback_hours=0.1,
            trace_assertion_stabilization_seconds=180,
            insight_start_margin_seconds=30,
            checkpoint_store=store,
            start_stagger=_StartStagger(0),
        )

    assert runtime.invoked == []


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
            observation_indexes = [
                index
                for index, summary in enumerate(summaries)
                if summary.activation_gate
            ]
            for observation_index in observation_indexes[-5:]:
                summaries[observation_index] = replace(
                    summaries[observation_index],
                    semantic_assertions_passed=0,
                    assertion_results=(
                        SemanticAssertionEvidence(
                            "synthetic_contract",
                            False,
                            True,
                        ),
                    ),
                )
            return replace(
                evidence,
                semantic_assertions_passed=evidence.semantic_assertions_passed - 5,
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
            observation_indexes = [
                index
                for index, summary in enumerate(summaries)
                if summary.activation_gate
            ]
            for observation_index in observation_indexes[-5:]:
                summaries[observation_index] = replace(
                    summaries[observation_index],
                    semantic_assertions_passed=0,
                    assertion_results=(
                        SemanticAssertionEvidence(
                            "synthetic_contract",
                            False,
                            True,
                        ),
                    ),
                )
            return replace(
                evidence,
                semantic_assertions_passed=evidence.semantic_assertions_passed - 5,
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


def test_failed_hosted_semantic_activation_does_not_start_insights() -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    registry = _registry(agents, hashes)
    target = "issue-016"

    class FailedSemanticActivationRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.starts: list[str] = []

        def invoke_version(self, **kwargs) -> InvocationEvidence:
            evidence = super().invoke_version(**kwargs)
            if kwargs["foundry_version"] != target:
                return evidence
            summaries = list(evidence.request_summaries)
            observation_indexes = [
                index
                for index, summary in enumerate(summaries)
                if summary.activation_gate
            ]
            for observation_index in observation_indexes[-5:]:
                summaries[observation_index] = replace(
                    summaries[observation_index],
                    semantic_assertions_passed=0,
                    assertion_results=(
                        SemanticAssertionEvidence(
                            "synthetic_contract",
                            False,
                            True,
                        ),
                    ),
                )
            return replace(
                evidence,
                semantic_assertions_passed=evidence.semantic_assertions_passed - 5,
                request_summaries=tuple(summaries),
            )

        def start_insights_run(self, **kwargs) -> InsightRunCheckpoint:
            self.starts.append(kwargs["foundry_version"])
            return super().start_insights_run(**kwargs)

    runtime = FailedSemanticActivationRuntime()
    results = execute(
        agents=agents,
        issues=issues,
        selected={
            agent["name"]: [
                target
                if agent["name"] == "finance-agent"
                else agent["issue_ids"][0]
            ]
            for agent in agents["agents"]
        },
        registry=registry,
        runtime=runtime,
        seed=1,
    )
    issue = next(
        item for item in results if item.agent_name == "finance-agent"
    ).issues[0]

    assert issue.status == "inconclusive"
    assert issue.error_code == "issue_activation_failed"
    assert target not in runtime.starts


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
                requests = execution_requests(kwargs["traffic_path"])
                observation_indexes = [
                    index
                    for index, request in enumerate(requests)
                    if request["expected"]["activation_gate"]
                ]
                for observation_index in observation_indexes[-5:]:
                    assertions = list(evidence[observation_index])
                    assertions[0] = TraceAssertionEvidence(
                        assertions[0].assertion,
                        False,
                    )
                    evidence[observation_index] = tuple(assertions)
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
    assert issue.trace_assertion_count == 20
    assert issue.trace_assertions_passed == 15
    failed_summary = next(
        summary
        for summary in issue.endpoint_request_summaries
        if summary.activation_gate and summary.trace_assertions_passed == 1
    )
    assert failed_summary.trace_assertion_results[0] == (
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


def test_aggregate_baseline_terminal_gap_is_not_a_strict_trace_unknown(
    tmp_path: Path,
) -> None:
    agents, issues = load_catalogs()
    finance = next(
        item for item in agents["agents"] if item["name"] == "finance-agent"
    )
    selected = {"finance-agent": finance["issue_ids"][:1]}
    hashes = catalog_hashes(agents, issues)
    store = VersionCheckpointStore(tmp_path / "stages", "sha256:" + "d" * 64)

    class SingleUnknownBaselineRuntime(FakeRuntime):
        def trace_behavior_evidence(
            self,
            operation_ids: tuple[str, ...],
        ) -> dict:
            evidence = super().trace_behavior_evidence(operation_ids)
            if len(operation_ids) == 20:
                evidence.update(
                    {
                        "terminal_response_count": 19,
                        "terminal_success_count": 19,
                        "terminal_output_count": 19,
                        "explicit_terminal_success_count": 19,
                        "explicit_terminal_output_count": 19,
                    }
                )
            return evidence

    runtime = SingleUnknownBaselineRuntime()
    result = execute_agent(
        agent_name="finance-agent",
        agents=agents,
        issues=issues,
        selected=selected,
        registry=_registry(agents, hashes),
        runtime=runtime,
        seed=1,
        checkpoint_store=store,
    )

    assert result.baseline.status == "inconclusive"
    assert result.baseline.error_code == "baseline_evidence_failed"
    assert result.baseline.trace_behavior_summary["terminal_response_count"] == 19
    assert result.issues[0].status == "skipped_baseline"
    assert store.agent_recovery_count("finance-agent", 3) == 0
    assert runtime.invoked.count("v0") == 1


def test_incomplete_aggregate_terminal_evidence_is_not_retrafficked(
    tmp_path: Path,
) -> None:
    agents, issues = load_catalogs()
    finance = next(
        item for item in agents["agents"] if item["name"] == "finance-agent"
    )
    selected = {"finance-agent": finance["issue_ids"][:4]}
    hashes = catalog_hashes(agents, issues)
    store = VersionCheckpointStore(tmp_path / "stages", "sha256:" + "d" * 64)

    class RecoveringBaselineRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.baseline_trace_attempts = 0

        def trace_behavior_evidence(
            self,
            operation_ids: tuple[str, ...],
        ) -> dict:
            evidence = super().trace_behavior_evidence(operation_ids)
            if len(operation_ids) == 20:
                self.baseline_trace_attempts += 1
                if self.baseline_trace_attempts == 1:
                    evidence.update(
                        {
                            "terminal_response_count": 18,
                            "terminal_success_count": 18,
                            "terminal_output_count": 18,
                            "explicit_terminal_success_count": 18,
                            "explicit_terminal_output_count": 18,
                        }
                    )
            return evidence

    runtime = RecoveringBaselineRuntime()
    kwargs = {
        "agent_name": "finance-agent",
        "agents": agents,
        "issues": issues,
        "selected": selected,
        "registry": _registry(agents, hashes),
        "runtime": runtime,
        "seed": 1,
        "checkpoint_store": store,
    }

    incomplete = execute_agent(**kwargs)
    recovered = execute_agent(**kwargs)

    assert incomplete.baseline.error_code == "baseline_evidence_failed"
    assert all(item.status == "skipped_baseline" for item in incomplete.issues)
    assert recovered.baseline.error_code == "baseline_evidence_failed"
    assert all(item.status == "skipped_baseline" for item in recovered.issues)
    assert store.agent_recovery_count("finance-agent", 3) == 0
    assert runtime.invoked.count("v0") == 1
    assert all(
        runtime.invoked.count(issue_id) == 0
        for issue_id in selected["finance-agent"]
    )
    assert not list((tmp_path / "stages" / "recovery-history").rglob("*.json"))


def test_ambiguous_baseline_delivery_is_not_recovery_safe() -> None:
    result = VersionResult(
        logical_version="v0",
        foundry_version="v0",
        status="inconclusive",
        operation_ids=["1" * 32],
        error_code="baseline_evidence_incomplete",
        endpoint_request_count=2,
        endpoint_response_count=1,
        endpoint_usable_response_count=1,
        trace_contract_verified=True,
        trace_behavior_summary={"unhandled_error_count": 0},
    )

    assert _baseline_recovery_is_safe(result, None) is False


def test_definitive_unhealthy_baseline_is_not_recovered(tmp_path: Path) -> None:
    agents, issues = load_catalogs()
    finance = next(
        item for item in agents["agents"] if item["name"] == "finance-agent"
    )
    selected = {"finance-agent": finance["issue_ids"][:1]}
    hashes = catalog_hashes(agents, issues)
    store = VersionCheckpointStore(tmp_path / "stages", "sha256:" + "d" * 64)

    class UnhealthyBaselineRuntime(FakeRuntime):
        def trace_behavior_evidence(
            self,
            operation_ids: tuple[str, ...],
        ) -> dict:
            evidence = super().trace_behavior_evidence(operation_ids)
            if len(operation_ids) == 20:
                evidence["unhandled_error_count"] = 1
            return evidence

    runtime = UnhealthyBaselineRuntime()
    kwargs = {
        "agent_name": "finance-agent",
        "agents": agents,
        "issues": issues,
        "selected": selected,
        "registry": _registry(agents, hashes),
        "runtime": runtime,
        "seed": 1,
        "checkpoint_store": store,
    }

    first = execute_agent(**kwargs)
    resumed = execute_agent(**kwargs)

    assert first.baseline.error_code == "baseline_evidence_failed"
    assert resumed.baseline == first.baseline
    assert runtime.invoked.count("v0") == 1
    assert store.agent_recovery_count("finance-agent", 3) == 0


def test_incomplete_aggregate_baseline_does_not_claim_recovery(tmp_path: Path) -> None:
    agents, issues = load_catalogs()
    finance = next(
        item for item in agents["agents"] if item["name"] == "finance-agent"
    )
    selected = {"finance-agent": finance["issue_ids"][:1]}
    hashes = catalog_hashes(agents, issues)
    store = VersionCheckpointStore(tmp_path / "stages", "sha256:" + "d" * 64)

    class IncompleteBaselineRuntime(FakeRuntime):
        def trace_behavior_evidence(
            self,
            operation_ids: tuple[str, ...],
        ) -> dict:
            evidence = super().trace_behavior_evidence(operation_ids)
            if len(operation_ids) == 20:
                evidence.update(
                    {
                        "terminal_response_count": 18,
                        "terminal_success_count": 18,
                        "terminal_output_count": 18,
                        "explicit_terminal_success_count": 18,
                        "explicit_terminal_output_count": 18,
                    }
                )
            return evidence

    runtime = IncompleteBaselineRuntime()
    kwargs = {
        "agent_name": "finance-agent",
        "agents": agents,
        "issues": issues,
        "selected": selected,
        "registry": _registry(agents, hashes),
        "runtime": runtime,
        "seed": 1,
        "checkpoint_store": store,
    }

    attempts = [execute_agent(**kwargs) for _ in range(5)]

    assert attempts[-1].baseline.error_code == "baseline_evidence_failed"
    assert all(
        item.status == "skipped_baseline" for item in attempts[-1].issues
    )
    assert store.agent_recovery_count("finance-agent", 3) == 0
    assert runtime.invoked.count("v0") == 1
    assert runtime.invoked.count("v0") == 1


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
    assert issue.error_code == "missing_insight"
    assert issue.insight_references == []
    assert issue.observed_insights == []
    assert issue.observed_insight is None
    assert persisted == issue


def test_card_linkage_does_not_require_catalog_minimum_trace_count() -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)

    class ThreeLinkCardRuntime(FakeRuntime):
        def finish_insights_run(self, **kwargs) -> InsightRunEvidence:
            result = super().finish_insights_run(**kwargs)
            if kwargs["foundry_version"] != "issue-009":
                return result
            card = result.insights[0]
            return replace(
                result,
                insights=(
                    replace(
                        card,
                        linked_operation_ids=card.linked_operation_ids[:3],
                        trace_count=3,
                    ),
                ),
            )

    result = execute_agent(
        agent_name="healthcare-agent",
        agents=agents,
        issues=issues,
        selected={"healthcare-agent": ["issue-009"]},
        registry=_registry(agents, hashes),
        runtime=ThreeLinkCardRuntime(),
        seed=1,
    )

    assert result.issues[0].status == "observed"
    assert result.issues[0].observed_insight is not None
    assert result.issues[0].observed_insight.trace_count == 3


@pytest.mark.parametrize(
    ("linked", "expected"),
    [
        (("a",), True),
        (("a", "b"), True),
        (("a", "outside"), False),
        (("a", "a"), False),
    ],
)
def test_every_card_link_must_be_unique_and_in_scope(
    linked: tuple[str, ...],
    expected: bool,
) -> None:
    assert linked_operations_match_scope(linked, {"a", "b"}) is expected


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
