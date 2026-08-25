from __future__ import annotations

import threading
import time
from datetime import date
from pathlib import Path

from agent_insights_quality.catalogs import catalog_hashes, load_catalogs
from agent_insights_quality.models import (
    InsightEvidence,
    InsightRunEvidence,
    InvocationEvidence,
)
from agent_insights_quality.runner import execute
from agent_insights_quality.selection import select_daily


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

    def report_progress(self, message: str) -> None:
        self.progress.append(message)

    def reset_monitor(self, agent_name: str, monitor_id: str) -> None:
        assert agent_name in monitor_id
        if agent_name == self.reset_failure_agent:
            raise RuntimeError("synthetic reset failure")

    def assert_clean_window(self, agent_name: str, lookback_hours: int) -> None:
        assert agent_name.endswith("-agent")
        assert lookback_hours == 3
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
        del agent_type, traffic_path, seed
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
        return InvocationEvidence(
            (),
            (foundry_version,),
            "2026-08-24T10:00:00+00:00",
            "2026-08-24T10:01:00+00:00",
            1,
            False,
        )

    def wait_for_telemetry(
        self,
        *,
        agent_name: str,
        foundry_version: str,
        invocation: InvocationEvidence,
    ) -> tuple[str, ...]:
        del agent_name, invocation
        return ((foundry_version.replace("issue-", "") + "0" * 32)[:32],)

    def run_insights(
        self,
        *,
        agent_name: str,
        monitor_id: str,
        foundry_version: str,
        operation_ids: tuple[str, ...],
        lookback_hours: int,
    ) -> InsightRunEvidence:
        del agent_name, monitor_id
        assert lookback_hours == 3
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
    ) -> None:
        assert agent_name.endswith("-agent")
        assert foundry_version
        assert operation_ids
        assert "invoke_agent" in required_operations


def test_runner_executes_25_issues() -> None:
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
    assert sum(len(item.issues) for item in results) == 25
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
