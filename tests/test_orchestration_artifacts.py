from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from agent_insights_quality.runtime import artifacts
from agent_insights_quality.runtime import orchestrator as orchestrator_module
from agent_insights_quality.runtime.artifacts import AzureBlobArtifactStore, LocalArtifactStore
from agent_insights_quality.runtime.errors import RuntimeFailure
from agent_insights_quality.runtime.orchestrator import (
    AnalysisWindow,
    PlanInput,
    ProductionOrchestrator,
    RunState,
    VersionWork,
)
from agent_insights_quality.runtime.receipts import (
    ensure_public_safe,
    opaque_reference,
    read_receipt,
    write_receipt,
)


def work(agent: str, version: str, start: datetime) -> VersionWork:
    return VersionWork(
        agent_id=agent,
        agent_name=agent + "-name",
        version_reference=version,
        window=AnalysisWindow(start, start + timedelta(minutes=5)),
        assignments=({"scenario_id": "scenario"},),
    )


class Hooks:
    def __init__(self, *, fail_once: bool = False, results=None) -> None:
        self.calls: list[str] = []
        self.fail_once = fail_once
        self.finalized = 0
        self.finalized_failure: RuntimeFailure | None = None
        self.results: dict[str, dict[str, str]] = results if results is not None else {}

    def _value(self, name, key):
        self.calls.append(name + ":" + key)
        result = {"result_reference": "sha256:" + "a" * 64}
        self.results[key] = result
        return result

    def preflight(self, _plan, *, dry_run):
        return self._value("preflight", str(dry_run))

    def ensure_project(self, _plan, *, idempotency_key):
        return self._value("project", idempotency_key)

    def deploy(self, _work, *, idempotency_key):
        return self._value("deploy", idempotency_key)

    def invoke(self, work, _deployment, *, idempotency_key):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeFailure("temporary", "Temporary invocation failure.", transient=True)
        value = self._value("invoke", idempotency_key)
        if isinstance(work.window, AnalysisWindow):
            start = work.window.start
            end = work.window.end
        else:
            start = datetime(2026, 8, 21, tzinfo=UTC)
            end = start + timedelta(minutes=5)
        result = value | {
            "window_binding": {
                "planned_start": "window://test/healthy/start-inclusive",
                "planned_end": "window://test/healthy/end-exclusive",
                "realized_start": start.isoformat(),
                "realized_end": end.isoformat(),
            }
        }
        self.results[idempotency_key] = result
        return result

    def wait_ingestion(self, _work, _invocation, *, idempotency_key):
        return self._value("ingestion", idempotency_key)

    def run_insights(self, _work, _telemetry, *, idempotency_key):
        return self._value("insights", idempotency_key)

    def assemble_evidence(self, _work, _run, *, idempotency_key):
        return self._value("evidence", idempotency_key)

    def recover(self, key, _checkpoint):
        self.calls.append("recover:" + key)
        return self.results[key]

    def cancel(self, _work):
        self.calls.append("cancel")

    def finalize_failure(self, failure, _state):
        self.finalized += 1
        self.finalized_failure = failure


def test_orchestrator_retries_and_resumes_idempotently_with_public_receipt(tmp_path: Path) -> None:
    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput("aiq-20260821", "aiq-20260821", {"a": (work("a", "v1", start),)})
    hooks = Hooks(fail_once=True)
    receipt = tmp_path / "state.json"
    result = ProductionOrchestrator(hooks, receipt, sleep=lambda _seconds: None).run(plan)
    assert result.status == "succeeded"
    assert sum(call.startswith("invoke:") for call in hooks.calls) == 1
    persisted = read_receipt(receipt)
    ensure_public_safe(persisted)

    resumed_orchestrator = ProductionOrchestrator(
        hooks,
        receipt,
        sleep=lambda _seconds: None,
    )
    resumed = resumed_orchestrator.run(
        plan,
        resume=True,
    )
    assert resumed.status == "succeeded"
    resumed_orchestrator.cancel()
    assert hooks.calls.count("cancel") == 0
    changed = PlanInput(
        plan.plan_id,
        plan.project_name,
        {"a": (work("a", "v2", start + timedelta(minutes=5)),)},
    )
    mismatched = ProductionOrchestrator(hooks, receipt)
    with pytest.raises(RuntimeFailure, match="different plan"):
        mismatched.run(changed, resume=True)
    mismatched.cancel()
    assert hooks.calls.count("cancel") == 0


@pytest.mark.parametrize(
    ("retry_after", "expected_delay"),
    [
        (120, 120.0),
        (0.5, 0.5),
        (301, 1),
        (-1, 1),
        (True, 1),
        ("120", 1),
    ],
)
def test_orchestrator_retry_honors_only_bounded_numeric_retry_after(
    tmp_path: Path,
    retry_after,
    expected_delay: float,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeFailure(
                "transient",
                "Synthetic transient failure.",
                {"retry_after_seconds": retry_after},
                transient=True,
            )
        return {"result_reference": "sha256:" + ("a" * 64)}

    result = ProductionOrchestrator(
        Hooks(),
        tmp_path / "state.json",
        sleep=sleeps.append,
    )._retry(operation)

    assert result["result_reference"].startswith("sha256:")
    assert sleeps == [expected_delay]


def test_orchestrator_deadline_stops_before_retry_without_cleanup(
    tmp_path: Path,
) -> None:
    clock = [0.0]
    attempts = 0

    def operation():
        nonlocal attempts
        attempts += 1
        raise RuntimeFailure(
            "transient",
            "Synthetic transient failure.",
            transient=True,
        )

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    hooks = Hooks()
    orchestrator = ProductionOrchestrator(
        hooks,
        tmp_path / "state.json",
        run_timeout_seconds=1,
        sleep=sleep,
        monotonic=lambda: clock[0],
    )
    orchestrator._deadline = 1

    with pytest.raises(RuntimeFailure) as captured:
        orchestrator._retry(operation)

    assert captured.value.code == "run_deadline_exceeded"
    assert attempts == 1
    assert "cancel" not in hooks.calls


def test_orchestrator_deadline_stops_before_new_step_and_retains_checkpoint(
    tmp_path: Path,
) -> None:
    clock = [0.0]

    class DeadlineHooks(Hooks):
        def deploy(self, current, *, idempotency_key):
            value = super().deploy(current, idempotency_key=idempotency_key)
            clock[0] = 1.0
            return value

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {"a": (work("a", "v1", start),)},
    )
    receipt = tmp_path / "state.json"
    hooks = DeadlineHooks()

    with pytest.raises(RuntimeFailure) as captured:
        ProductionOrchestrator(
            hooks,
            receipt,
            run_timeout_seconds=1,
            monotonic=lambda: clock[0],
        ).run(plan)

    assert captured.value.details["failure_codes"] == ["run_deadline_exceeded"]
    assert sum(call.startswith("deploy:") for call in hooks.calls) == 1
    assert not any(call.startswith("invoke:") for call in hooks.calls)
    assert hooks.calls.count("cancel") == 0
    assert any(key.endswith(":deploy") for key in read_receipt(receipt)["checkpoints"])


def test_orchestrator_final_evidence_cannot_finish_after_deadline(
    tmp_path: Path,
) -> None:
    clock = [0.0]

    class DeadlineHooks(Hooks):
        def assemble_evidence(self, current, run, *, idempotency_key):
            value = super().assemble_evidence(
                current,
                run,
                idempotency_key=idempotency_key,
            )
            clock[0] = 1.0
            return value

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {"a": (work("a", "v1", start),)},
    )
    receipt = tmp_path / "state.json"

    with pytest.raises(RuntimeFailure) as captured:
        ProductionOrchestrator(
            DeadlineHooks(),
            receipt,
            run_timeout_seconds=1,
            monotonic=lambda: clock[0],
        ).run(plan)

    assert captured.value.details["failure_codes"] == ["run_deadline_exceeded"]
    state = read_receipt(receipt)
    assert state["status"] == "inconclusive"
    assert state["agent_failures"]["a"]["phase"] == "evidence"
    assert any(key.endswith(":evidence") for key in state["checkpoints"])


def test_run_state_loads_legacy_receipt_without_agent_failures() -> None:
    state = RunState("aiq-20260821", "sha256:" + ("a" * 64))
    payload = state.public_dict()
    payload.pop("agent_failures")

    loaded = RunState.from_receipt(
        payload,
        state.plan_id,
        state.plan_reference,
    )

    assert loaded.agent_failures == {}


def test_resume_recovers_completed_steps_without_replaying_side_effects(
    tmp_path: Path,
) -> None:
    class InterruptedHooks(Hooks):
        def wait_ingestion(self, _work, _invocation, *, idempotency_key):
            raise RuntimeFailure("interrupted", "Synthetic process interruption.")

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput("aiq-20260821", "aiq-20260821", {"a": (work("a", "v1", start),)})
    receipt = tmp_path / "state.json"
    shared: dict[str, dict[str, str]] = {}
    with pytest.raises(RuntimeFailure, match="independent agent sequences"):
        ProductionOrchestrator(
            InterruptedHooks(results=shared),
            receipt,
        ).run(plan)

    resumed_hooks = Hooks(results=shared)
    result = ProductionOrchestrator(resumed_hooks, receipt).run(plan, resume=True)
    assert result.status == "succeeded"
    assert not any(call.startswith(("deploy:", "invoke:")) for call in resumed_hooks.calls)
    assert any(call.startswith("recover:") and call.endswith(":deploy") for call in resumed_hooks.calls)
    assert any(call.startswith("recover:") and call.endswith(":invoke") for call in resumed_hooks.calls)


def test_orchestrator_finalizes_failure_without_success_shaped_state(tmp_path: Path) -> None:
    class FailingHooks(Hooks):
        def deploy(self, _work, *, idempotency_key):
            raise RuntimeFailure("deployment_failed", "Deployment failed.")

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput("aiq-20260821", "aiq-20260821", {"a": (work("a", "v1", start),)})
    hooks = FailingHooks()
    receipt = tmp_path / "state.json"
    with pytest.raises(RuntimeFailure, match="independent agent sequences"):
        ProductionOrchestrator(hooks, receipt).run(plan)
    state = read_receipt(receipt)
    assert state["status"] == "inconclusive"
    assert state["failed_phase"] == "agents"
    assert state["agent_failures"]["a"]["code"] == "deployment_failed"
    assert hooks.finalized == 1


def test_resume_clears_stale_failure_before_returning_to_running(
    tmp_path: Path,
) -> None:
    class FirstFailure(Hooks):
        def deploy(self, _work, *, idempotency_key):
            raise RuntimeFailure("deployment_failed", "First failure.")

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {"a": (work("a", "v1", start),)},
    )
    receipt = tmp_path / "state.json"
    shared: dict[str, dict[str, str]] = {}
    with pytest.raises(RuntimeFailure, match="independent agent sequences"):
        ProductionOrchestrator(
            FirstFailure(results=shared),
            receipt,
        ).run(plan)

    class ResumedHooks(Hooks):
        def invoke(self, current, deployment, *, idempotency_key):
            active = read_receipt(receipt)
            assert active["status"] == "running"
            assert active["failed_phase"] is None
            assert active["failure"] is None
            return super().invoke(
                current,
                deployment,
                idempotency_key=idempotency_key,
            )

    resumed = ProductionOrchestrator(
        ResumedHooks(results=shared),
        receipt,
    ).run(plan, resume=True)
    assert resumed.status == "succeeded"
    assert resumed.attempt == 2
    assert resumed.agent_failures == {}


def test_runtime_state_drops_private_failure_details_but_keeps_safe_diagnostics(
    tmp_path: Path,
) -> None:
    class FailingHooks(Hooks):
        def deploy(self, _work, *, idempotency_key):
            raise RuntimeFailure(
                "azure_cli_failed",
                "Azure CLI command failed.",
                {
                    "phase": "deploy",
                    "command": [
                        "az",
                        "resource",
                        "show",
                        "--ids",
                        "/subscript"
                        "ions/11111111-1111-1111-1111-111111111111/"
                        "resourceGroups/private-rg",
                    ],
                    "url": "https://private.example.invalid/resource",
                    "failure_count": 1,
                },
            )

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {"a": (work("a", "v1", start),)},
    )
    receipt = tmp_path / "state.json"
    with pytest.raises(RuntimeFailure, match="independent agent sequences"):
        ProductionOrchestrator(FailingHooks(), receipt).run(plan)

    state = read_receipt(receipt)
    assert state["agent_failures"]["a"]["details"] == {
        "failure_count": 1,
        "phase": "deploy",
    }
    assert state["failure"]["details"]["failure_codes"] == ["azure_cli_failed"]
    assert len(state["failure"]["details"]["work_references"]) == 1
    serialized = json.dumps(state)
    assert "/subscript" + "ions/" not in serialized
    assert "private.example.invalid" not in serialized


def test_unexpected_failure_keeps_only_opaque_private_diagnostics(
    tmp_path: Path,
) -> None:
    class UnexpectedHooks(Hooks):
        def deploy(self, _work, *, idempotency_key):
            raise ValueError("synthetic private diagnostic")

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {"a": (work("a", "v1", start),)},
    )
    receipt = tmp_path / "state.json"
    hooks = UnexpectedHooks()

    with pytest.raises(RuntimeFailure) as captured:
        ProductionOrchestrator(hooks, receipt).run(plan)

    assert captured.value.code == "daily_work_failures"
    assert hooks.finalized_failure is captured.value
    diagnostics = captured.value.details["unexpected_exceptions"]
    assert diagnostics[0]["exception_class"] == "ValueError"
    assert diagnostics[0]["exception_reference"].startswith("sha256:")
    serialized = json.dumps(read_receipt(receipt))
    assert "ValueError" not in serialized
    assert "synthetic private diagnostic" not in serialized


def test_orchestrator_continues_queued_agents_after_one_failure(tmp_path: Path) -> None:
    class FailingFirstHooks(Hooks):
        def deploy(self, current, *, idempotency_key):
            self.calls.append("attempt:" + current.agent_id)
            if current.agent_id == "a":
                raise RuntimeFailure("deployment_failed", "Deployment failed.")
            return super().deploy(current, idempotency_key=idempotency_key)

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {
            "a": (work("a", "v1", start),),
            "b": (work("b", "v1", start),),
        },
    )
    hooks = FailingFirstHooks()
    receipt = tmp_path / "state.json"
    with pytest.raises(RuntimeFailure, match="independent agent sequences"):
        ProductionOrchestrator(
            hooks,
            receipt,
            max_parallel_agents=1,
        ).run(plan)
    assert "attempt:b" in hooks.calls
    assert hooks.calls.count("cancel") == 0
    state = read_receipt(receipt)
    assert state["agent_failures"]["a"]["code"] == "deployment_failed"
    assert any(key.endswith(":evidence") for key in state["checkpoints"] if key.startswith("b:"))


def test_orchestrator_aggregates_multiple_failures_after_peers_finish(tmp_path: Path) -> None:
    class MultipleFailureHooks(Hooks):
        def deploy(self, current, *, idempotency_key):
            if current.agent_id == "a":
                raise RuntimeFailure("deployment_failed", "Deployment failed.")
            return super().deploy(current, idempotency_key=idempotency_key)

        def invoke(self, current, deployment, *, idempotency_key):
            if current.agent_id == "b":
                raise RuntimeFailure("invocation_failed", "Invocation failed.")
            return super().invoke(
                current,
                deployment,
                idempotency_key=idempotency_key,
            )

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {
            "a": (work("a", "v1", start),),
            "b": (work("b", "v1", start),),
            "c": (work("c", "v1", start),),
        },
    )
    hooks = MultipleFailureHooks()
    receipt = tmp_path / "state.json"
    with pytest.raises(RuntimeFailure) as captured:
        ProductionOrchestrator(
            hooks,
            receipt,
            max_parallel_agents=3,
        ).run(plan)
    assert captured.value.code == "daily_work_failures"
    assert captured.value.details["failure_codes"] == [
        "deployment_failed",
        "invocation_failed",
    ]
    assert captured.value.details["agent_references"] == sorted(
        [
            opaque_reference("a"),
            opaque_reference("b"),
        ]
    )
    assert captured.value.details["work_references"] == sorted(
        [
            opaque_reference(plan.agents["a"][0].key),
            opaque_reference(plan.agents["b"][0].key),
        ]
    )
    state = read_receipt(receipt)
    assert set(state["agent_failures"]) == {"a", "b"}
    assert any(key.endswith(":evidence") for key in state["checkpoints"] if key.startswith("c:"))
    assert hooks.calls.count("cancel") == 0
    assert hooks.finalized == 1


def test_aggregate_is_transient_only_when_every_agent_failure_is_transient() -> None:
    transient = RuntimeFailure("temporary", "Temporary failure.", transient=True)
    permanent = RuntimeFailure("permanent", "Permanent failure.")

    assert ProductionOrchestrator._aggregate_agent_failures(
        {"a": transient, "b": transient}
    ).transient
    assert not ProductionOrchestrator._aggregate_agent_failures(
        {"a": transient, "b": permanent}
    ).transient


def test_fourteen_of_seventeen_resume_runs_only_incomplete_evidence(tmp_path: Path) -> None:
    class PartialEvidenceHooks(Hooks):
        def assemble_evidence(self, current, run, *, idempotency_key):
            if int(current.agent_id.removeprefix("agent-")) >= 14:
                raise RuntimeFailure("evidence_failed", "Evidence assembly failed.")
            return super().assemble_evidence(
                current,
                run,
                idempotency_key=idempotency_key,
            )

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {
            f"agent-{index}": (work(f"agent-{index}", "v1", start),)
            for index in range(17)
        },
    )
    receipt = tmp_path / "state.json"
    shared: dict[str, dict[str, str]] = {}
    with pytest.raises(RuntimeFailure) as captured:
        ProductionOrchestrator(
            PartialEvidenceHooks(results=shared),
            receipt,
            max_parallel_agents=5,
        ).run(plan)
    assert captured.value.details["failure_count"] == 3
    first = read_receipt(receipt)
    assert sum(key.endswith(":evidence") for key in first["checkpoints"]) == 14

    resumed_hooks = Hooks(results=shared)
    resumed = ProductionOrchestrator(
        resumed_hooks,
        receipt,
        max_parallel_agents=5,
    ).run(plan, resume=True)
    assert resumed.status == "succeeded"
    assert sum(call.startswith("evidence:") for call in resumed_hooks.calls) == 3
    assert not any(
        call.startswith(("deploy:", "invoke:", "ingestion:", "insights:"))
        for call in resumed_hooks.calls
    )
    recovered = [call for call in resumed_hooks.calls if call.startswith("recover:")]
    assert len(recovered) == 13


def test_resume_recovers_complete_earlier_version_and_finishes_later_version(
    tmp_path: Path,
) -> None:
    class LaterVersionFailureHooks(Hooks):
        def assemble_evidence(self, current, run, *, idempotency_key):
            if current.version_reference == "v2":
                raise RuntimeFailure("evidence_failed", "Evidence assembly failed.")
            return super().assemble_evidence(
                current,
                run,
                idempotency_key=idempotency_key,
            )

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {
            "a": (
                work("a", "v1", start),
                work("a", "v2", start + timedelta(minutes=5)),
            ),
        },
    )
    receipt = tmp_path / "state.json"
    shared: dict[str, dict[str, str]] = {}
    with pytest.raises(RuntimeFailure):
        ProductionOrchestrator(
            LaterVersionFailureHooks(results=shared),
            receipt,
        ).run(plan)

    resumed_hooks = Hooks(results=shared)
    resumed = ProductionOrchestrator(resumed_hooks, receipt).run(
        plan,
        resume=True,
    )

    assert resumed.status == "succeeded"
    assert sum(call.startswith("evidence:") for call in resumed_hooks.calls) == 1
    assert not any(
        call.startswith(("deploy:", "invoke:", "ingestion:", "insights:"))
        for call in resumed_hooks.calls
    )
    recovered = [call for call in resumed_hooks.calls if call.startswith("recover:")]
    assert len(recovered) == 10
    assert any(":v1:" in call and call.endswith(":evidence") for call in recovered)
    assert any(":v2:" in call and call.endswith(":insights") for call in recovered)


def test_explicit_abort_performs_second_cleanup_sweep_for_late_deployment(
    tmp_path: Path,
) -> None:
    deploy_started = threading.Event()
    first_cancel_seen = threading.Event()

    class LateDeployHooks(Hooks):
        def __init__(self):
            super().__init__()
            self.active = False
            self.cancel_count = 0

        def deploy(self, current, *, idempotency_key):
            deploy_started.set()
            assert first_cancel_seen.wait(timeout=2)
            self.active = True
            return self._value("deploy", idempotency_key)

        def cancel(self, _current):
            self.cancel_count += 1
            if self.active:
                self.active = False
            else:
                first_cancel_seen.set()

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {"a": (work("a", "v1", start),)},
    )
    hooks = LateDeployHooks()
    receipt = tmp_path / "state.json"
    orchestrator = ProductionOrchestrator(hooks, receipt)
    failures: list[RuntimeFailure] = []

    def execute() -> None:
        try:
            orchestrator.run(plan)
        except RuntimeFailure as error:
            failures.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    assert deploy_started.wait(timeout=2)
    orchestrator.cancel()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert failures[0].code == "run_cancelled"
    assert hooks.active is False
    assert hooks.cancel_count == 2
    assert read_receipt(receipt)["failure"]["code"] == "run_cancelled"
    resume_hooks = Hooks(results=hooks.results)
    resume_orchestrator = ProductionOrchestrator(resume_hooks, receipt)
    with pytest.raises(RuntimeFailure) as resume_failure:
        resume_orchestrator.run(
            plan,
            resume=True,
        )
    assert resume_failure.value.code == "aborted_run_not_resumable"
    resume_orchestrator.cancel()
    assert resume_hooks.calls.count("cancel") == 0


def test_abort_during_resume_initialization_is_delivered_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {"a": (work("a", "v1", start),)},
    )
    receipt = tmp_path / "state.json"
    write_receipt(
        receipt,
        RunState(plan.plan_id, plan.reference).public_dict(),
    )
    read_started = threading.Event()
    allow_read = threading.Event()
    original_read = orchestrator_module.read_receipt

    def blocking_read(path: Path):
        read_started.set()
        assert allow_read.wait(timeout=2)
        return original_read(path)

    monkeypatch.setattr(orchestrator_module, "read_receipt", blocking_read)
    hooks = Hooks()
    orchestrator = ProductionOrchestrator(hooks, receipt)
    failures: list[RuntimeFailure] = []

    def execute() -> None:
        try:
            orchestrator.run(plan, resume=True)
        except RuntimeFailure as error:
            failures.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    assert read_started.wait(timeout=2)
    orchestrator.cancel()
    assert hooks.calls.count("cancel") == 0
    allow_read.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert failures[0].code == "run_cancelled"
    assert read_receipt(receipt)["failure"]["code"] == "run_cancelled"
    assert hooks.calls.count("cancel") == 2
    assert not any(
        call.startswith(("preflight:", "project:", "deploy:"))
        for call in hooks.calls
    )


def test_abort_during_invalid_resume_initialization_never_cleans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {"a": (work("a", "v1", start),)},
    )
    receipt = tmp_path / "state.json"
    write_receipt(
        receipt,
        RunState("different-plan", plan.reference).public_dict(),
    )
    read_started = threading.Event()
    allow_read = threading.Event()
    original_read = orchestrator_module.read_receipt

    def blocking_read(path: Path):
        read_started.set()
        assert allow_read.wait(timeout=2)
        return original_read(path)

    monkeypatch.setattr(orchestrator_module, "read_receipt", blocking_read)
    hooks = Hooks()
    orchestrator = ProductionOrchestrator(hooks, receipt)
    failures: list[RuntimeFailure] = []

    def execute() -> None:
        try:
            orchestrator.run(plan, resume=True)
        except RuntimeFailure as error:
            failures.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    assert read_started.wait(timeout=2)
    orchestrator.cancel()
    allow_read.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert failures[0].code == "resume_plan_mismatch"
    assert hooks.calls.count("cancel") == 0
    assert read_receipt(receipt).get("failure") is None


def test_abort_receipt_is_durable_before_first_cleanup_hook(
    tmp_path: Path,
) -> None:
    deploy_started = threading.Event()
    release_deploy = threading.Event()

    class OrderingHooks(Hooks):
        def __init__(self, receipt: Path):
            super().__init__()
            self.receipt = receipt
            self.cleanup_states: list[str] = []

        def deploy(self, _current, *, idempotency_key):
            deploy_started.set()
            assert release_deploy.wait(timeout=2)
            return self._value("deploy", idempotency_key)

        def cancel(self, _current):
            self.cleanup_states.append(
                str(read_receipt(self.receipt)["failure"]["code"])
            )
            release_deploy.set()
            super().cancel(_current)

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {"a": (work("a", "v1", start),)},
    )
    receipt = tmp_path / "state.json"
    hooks = OrderingHooks(receipt)
    orchestrator = ProductionOrchestrator(hooks, receipt)
    failures: list[RuntimeFailure] = []

    def execute() -> None:
        try:
            orchestrator.run(plan)
        except RuntimeFailure as error:
            failures.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    assert deploy_started.wait(timeout=2)
    orchestrator.cancel()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert failures[0].code == "run_cancelled"
    assert hooks.cleanup_states == ["run_cancelled", "run_cancelled"]
    assert read_receipt(receipt)["failure"]["code"] == "run_cancelled"
    with pytest.raises(RuntimeFailure) as resume_failure:
        ProductionOrchestrator(Hooks(), receipt).run(plan, resume=True)
    assert resume_failure.value.code == "aborted_run_not_resumable"


def test_abort_cannot_be_overwritten_by_running_transition(
    tmp_path: Path,
) -> None:
    project_complete = threading.Event()
    allow_running_transition = threading.Event()

    class BlockingProjectOrchestrator(ProductionOrchestrator):
        def _step(self, state, key, phase, operation, *, replay_existing=False):
            value = super()._step(
                state,
                key,
                phase,
                operation,
                replay_existing=replay_existing,
            )
            if phase == "project":
                project_complete.set()
                assert allow_running_transition.wait(timeout=2)
            return value

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {"a": (work("a", "v1", start),)},
    )
    receipt = tmp_path / "state.json"
    hooks = Hooks()
    orchestrator = BlockingProjectOrchestrator(hooks, receipt)
    failures: list[RuntimeFailure] = []

    def execute() -> None:
        try:
            orchestrator.run(plan)
        except RuntimeFailure as error:
            failures.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    assert project_complete.wait(timeout=2)
    orchestrator.cancel()
    allow_running_transition.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert failures[0].code == "run_cancelled"
    assert read_receipt(receipt)["failure"]["code"] == "run_cancelled"
    assert hooks.calls.count("cancel") == 2


def test_explicit_abort_returns_after_bounded_worker_drain(tmp_path: Path) -> None:
    deploy_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()

    class UnresponsiveHooks(Hooks):
        def deploy(self, _current, *, idempotency_key):
            deploy_started.set()
            release_worker.wait(timeout=2)
            worker_finished.set()
            return self._value("deploy", idempotency_key)

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {"a": (work("a", "v1", start),)},
    )
    hooks = UnresponsiveHooks()
    abort_phase = threading.Event()
    monotonic_calls_after_abort = 0

    def monotonic() -> float:
        nonlocal monotonic_calls_after_abort
        if abort_phase.is_set():
            monotonic_calls_after_abort += 1
        return time.monotonic()

    orchestrator = ProductionOrchestrator(
        hooks,
        tmp_path / "state.json",
        cancellation_wait_seconds=0.01,
        monotonic=monotonic,
    )
    failures: list[RuntimeFailure] = []

    def execute() -> None:
        try:
            orchestrator.run(plan)
        except RuntimeFailure as error:
            failures.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    assert deploy_started.wait(timeout=2)
    abort_phase.set()
    orchestrator.cancel()
    thread.join(timeout=1)
    release_worker.set()

    assert not thread.is_alive()
    assert failures[0].code == "run_cancelled"
    assert hooks.calls.count("cancel") == 2
    assert monotonic_calls_after_abort >= 2
    assert worker_finished.wait(timeout=2)


def test_cancel_cannot_cleanup_after_success_commit_starts(tmp_path: Path) -> None:
    success_save_started = threading.Event()
    allow_success_save = threading.Event()

    class BlockingSuccessOrchestrator(ProductionOrchestrator):
        def _save(self, state):
            if state.status == "succeeded":
                success_save_started.set()
                assert allow_success_save.wait(timeout=2)
            super()._save(state)

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {"a": (work("a", "v1", start),)},
    )
    hooks = Hooks()
    orchestrator = BlockingSuccessOrchestrator(
        hooks,
        tmp_path / "state.json",
    )
    results: list[RunState] = []
    run_thread = threading.Thread(target=lambda: results.append(orchestrator.run(plan)))
    run_thread.start()
    assert success_save_started.wait(timeout=2)

    cancel_started = threading.Event()

    def cancel() -> None:
        cancel_started.set()
        orchestrator.cancel()

    cancel_thread = threading.Thread(target=cancel)
    cancel_thread.start()
    assert cancel_started.wait(timeout=2)
    allow_success_save.set()
    run_thread.join(timeout=2)
    cancel_thread.join(timeout=2)

    assert results[0].status == "succeeded"
    assert hooks.calls.count("cancel") == 0


def test_abort_winning_terminal_failure_race_is_not_resumable(
    tmp_path: Path,
) -> None:
    aggregate_started = threading.Event()
    allow_aggregate = threading.Event()

    class FailingHooks(Hooks):
        def deploy(self, _current, *, idempotency_key):
            raise RuntimeFailure("deployment_failed", "Deployment failed.")

    class BlockingAggregateOrchestrator(ProductionOrchestrator):
        @staticmethod
        def _aggregate_agent_failures(failures):
            aggregate_started.set()
            assert allow_aggregate.wait(timeout=2)
            return ProductionOrchestrator._aggregate_agent_failures(failures)

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {"a": (work("a", "v1", start),)},
    )
    receipt = tmp_path / "state.json"
    hooks = FailingHooks()
    orchestrator = BlockingAggregateOrchestrator(hooks, receipt)
    failures: list[RuntimeFailure] = []

    def execute() -> None:
        try:
            orchestrator.run(plan)
        except RuntimeFailure as error:
            failures.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    assert aggregate_started.wait(timeout=2)
    orchestrator.cancel()
    allow_aggregate.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert failures[0].code == "run_cancelled"
    assert read_receipt(receipt)["failure"]["code"] == "run_cancelled"
    assert hooks.calls.count("cancel") == 2
    with pytest.raises(RuntimeFailure) as resume_failure:
        ProductionOrchestrator(Hooks(), receipt).run(plan, resume=True)
    assert resume_failure.value.code == "aborted_run_not_resumable"


def test_explicit_abort_retains_late_cleanup_failure_code(tmp_path: Path) -> None:
    deploy_started = threading.Event()
    first_cancel_seen = threading.Event()

    class LateCleanupFailureHooks(Hooks):
        def __init__(self):
            super().__init__()
            self.active = False
            self.cancel_count = 0

        def deploy(self, current, *, idempotency_key):
            deploy_started.set()
            assert first_cancel_seen.wait(timeout=2)
            self.active = True
            return self._value("deploy", idempotency_key)

        def cancel(self, _current):
            self.cancel_count += 1
            if self.cancel_count == 1:
                first_cancel_seen.set()
            elif self.active:
                raise RuntimeFailure(
                    "late_cleanup_failed",
                    "Synthetic late cleanup failure.",
                )

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {"a": (work("a", "v1", start),)},
    )
    receipt = tmp_path / "state.json"
    hooks = LateCleanupFailureHooks()
    orchestrator = ProductionOrchestrator(hooks, receipt)
    failures: list[RuntimeFailure] = []

    def execute() -> None:
        try:
            orchestrator.run(plan)
        except RuntimeFailure as error:
            failures.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    assert deploy_started.wait(timeout=2)
    orchestrator.cancel()
    thread.join(timeout=2)

    state = read_receipt(receipt)
    assert failures[0].code == "run_cancelled"
    assert "late_cleanup_failed" in state["failure"]["details"][
        "cancellation_failures"
    ]


def test_plan_rejects_non_symbolic_execution_windows() -> None:
    start = datetime(2026, 8, 21, tzinfo=UTC)
    payload = {
        "plan_id": "aiq-20260821",
        "project": {"name": "aiq-20260821"},
        "assignments": [
            {
                "agent_id": "a",
                "agent_name": "agent",
                "agent_type": "prompt",
                "agent_version_digest": "v1",
                "wave": 0,
                "window": {"start": start.isoformat(), "end": (start + timedelta(minutes=5)).isoformat()},
                "version_sequence": [
                    {
                        "phase": "faulted",
                        "version_key": "faulted",
                        "digest": "v1",
                        "window": {
                            "start": start.isoformat(),
                            "end": (start + timedelta(minutes=5)).isoformat(),
                        },
                    }
                ],
            },
        ],
    }
    with pytest.raises(RuntimeFailure, match="symbolic"):
        PlanInput.from_daily_plan(payload)


def test_local_artifacts_use_opaque_references_and_exact_owner_cleanup(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    first = store.put("run/evidence.json", b"synthetic", "owner-a")
    store.put("run/other.json", b"other", "owner-b")
    assert first.reference.startswith("sha256:")
    assert store.get("run/evidence.json") == b"synthetic"
    selected = store.cleanup_expired(
        "owner-a",
        now=datetime.now(UTC) + timedelta(days=91),
        dry_run=False,
    )
    assert selected == ["run/evidence.json"]
    assert not (tmp_path / "run" / "evidence.json").exists()
    assert (tmp_path / "run" / "other.json").exists()


def test_receipts_and_artifacts_reject_private_values_and_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(RuntimeFailure, match="public-safe"):
        ensure_public_safe({"private_url": "sha256:" + "a" * 64})
    with pytest.raises(RuntimeFailure, match="public-safe"):
        ensure_public_safe({"reference": "https://" + "private.example.invalid"})
    with pytest.raises(RuntimeFailure, match="safe relative path"):
        LocalArtifactStore(tmp_path).put("../escape", b"x", "owner")


def test_local_artifact_read_rejects_payload_manifest_mismatch(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    store.put("run/evidence.json", b"expected", "owner")
    (tmp_path / "run" / "evidence.json").write_bytes(b"tampered")
    with pytest.raises(RuntimeFailure, match="incomplete or mismatched"):
        store.get("run/evidence.json")


def test_pending_artifact_is_recoverable_by_owner_scoped_orphan_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = artifacts._write_json_atomic
    calls = 0

    def interrupt_commit(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic interruption")
        original(path, payload)

    monkeypatch.setattr(artifacts, "_write_json_atomic", interrupt_commit)
    store = LocalArtifactStore(tmp_path)
    with pytest.raises(RuntimeFailure, match="payload or committed manifest"):
        store.put("run/orphan.json", b"orphan", "owner-a")
    metadata = json.loads(
        (tmp_path / "run" / "orphan.json.metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["state"] == "pending"
    assert (tmp_path / "run" / "orphan.json").exists()
    assert store.cleanup_expired(
        "owner-b",
        now=datetime.now(UTC) + timedelta(days=91),
        dry_run=False,
    ) == []
    assert store.cleanup_expired(
        "owner-a",
        now=datetime.now(UTC) + timedelta(days=91),
        dry_run=False,
    ) == ["run/orphan.json"]
    assert not (tmp_path / "run" / "orphan.json").exists()


def test_blob_artifacts_validate_content_hash_and_exact_owner_cleanup() -> None:
    class Download:
        def __init__(self, content):
            self._content = content

        def readall(self):
            return self._content

    class Blob:
        def __init__(self, container, name):
            self._container = container
            self._name = name

        def get_blob_properties(self):
            return SimpleNamespace(metadata=self._container.values[self._name][1])

    class Container:
        def __init__(self):
            self.values = {}

        def upload_blob(self, name, content, *, overwrite, metadata):
            assert overwrite is False
            self.values[name] = (content, metadata)

        def download_blob(self, name):
            return Download(self.values[name][0])

        def get_blob_client(self, name):
            return Blob(self, name)

        def list_blobs(self, *, include):
            assert include == ["metadata"]
            return [
                SimpleNamespace(name=name, metadata=metadata)
                for name, (_content, metadata) in self.values.items()
            ]

        def delete_blob(self, name):
            del self.values[name]

    container = Container()
    store = AzureBlobArtifactStore(container)
    record = store.put("run/evidence.json", b"expected", "owner-a")
    ensure_public_safe(record.public_dict())
    assert store.get("run/evidence.json") == b"expected"
    container.values["run/evidence.json"] = (
        b"tampered",
        container.values["run/evidence.json"][1],
    )
    with pytest.raises(RuntimeFailure, match="incomplete or mismatched"):
        store.get("run/evidence.json")
    assert store.cleanup_expired(
        "owner-b",
        now=datetime.now(UTC) + timedelta(days=91),
        dry_run=False,
    ) == []
    assert store.cleanup_expired(
        "owner-a",
        now=datetime.now(UTC) + timedelta(days=91),
        dry_run=False,
    ) == ["run/evidence.json"]
