from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_insights_quality.runtime.artifacts import LocalArtifactStore
from agent_insights_quality.runtime.errors import RuntimeFailure
from agent_insights_quality.runtime.orchestrator import (
    AnalysisWindow,
    PlanInput,
    ProductionOrchestrator,
    VersionWork,
)
from agent_insights_quality.runtime.receipts import ensure_public_safe, read_receipt


def work(agent: str, version: str, start: datetime) -> VersionWork:
    return VersionWork(
        agent_id=agent,
        agent_name=agent + "-name",
        version_reference=version,
        window=AnalysisWindow(start, start + timedelta(minutes=5)),
        assignments=({"scenario_id": "scenario"},),
    )


class Hooks:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_once = fail_once
        self.finalized = 0

    def _value(self, name, key):
        self.calls.append(name + ":" + key)
        return {"result_reference": "sha256:" + "a" * 64}

    def preflight(self, _plan, *, dry_run):
        return self._value("preflight", str(dry_run))

    def ensure_project(self, _plan, *, idempotency_key):
        return self._value("project", idempotency_key)

    def deploy(self, _work, *, idempotency_key):
        return self._value("deploy", idempotency_key)

    def invoke(self, _work, _deployment, *, idempotency_key):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeFailure("temporary", "Temporary invocation failure.", transient=True)
        return self._value("invoke", idempotency_key)

    def wait_ingestion(self, _work, _invocation, *, idempotency_key):
        return self._value("ingestion", idempotency_key)

    def run_insights(self, _work, _telemetry, *, idempotency_key):
        return self._value("insights", idempotency_key)

    def assemble_evidence(self, _work, _run, *, idempotency_key):
        return self._value("evidence", idempotency_key)

    def cancel(self, _work):
        self.calls.append("cancel")

    def finalize_failure(self, _failure, _state):
        self.finalized += 1


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

    resumed = ProductionOrchestrator(hooks, receipt, sleep=lambda _seconds: None).run(
        plan,
        resume=True,
    )
    assert resumed.status == "succeeded"
    changed = PlanInput(
        plan.plan_id,
        plan.project_name,
        {"a": (work("a", "v2", start + timedelta(minutes=5)),)},
    )
    with pytest.raises(RuntimeFailure, match="different plan"):
        ProductionOrchestrator(hooks, receipt).run(changed, resume=True)


def test_orchestrator_finalizes_failure_without_success_shaped_state(tmp_path: Path) -> None:
    class FailingHooks(Hooks):
        def deploy(self, _work, *, idempotency_key):
            raise RuntimeFailure("deployment_failed", "Deployment failed.")

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput("aiq-20260821", "aiq-20260821", {"a": (work("a", "v1", start),)})
    hooks = FailingHooks()
    receipt = tmp_path / "state.json"
    with pytest.raises(RuntimeFailure, match="Deployment failed"):
        ProductionOrchestrator(hooks, receipt).run(plan)
    state = read_receipt(receipt)
    assert state["status"] == "inconclusive"
    assert state["failed_phase"] == "deploy"
    assert hooks.finalized == 1


def test_plan_rejects_overlapping_sequential_versions() -> None:
    start = datetime(2026, 8, 21, tzinfo=UTC)
    payload = {
        "plan_id": "aiq-20260821",
        "project": {"name": "aiq-20260821"},
        "assignments": [
            {
                "agent_id": "a",
                "agent_name": "agent",
                "agent_version_digest": "v1",
                "window": {"start": start.isoformat(), "end": (start + timedelta(minutes=5)).isoformat()},
            },
            {
                "agent_id": "a",
                "agent_name": "agent",
                "agent_version_digest": "v2",
                "window": {
                    "start": (start + timedelta(minutes=4)).isoformat(),
                    "end": (start + timedelta(minutes=8)).isoformat(),
                },
            },
        ],
    }
    with pytest.raises(RuntimeFailure, match="overlapping"):
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
