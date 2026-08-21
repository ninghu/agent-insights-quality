from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from agent_insights_quality.runtime import artifacts
from agent_insights_quality.runtime.artifacts import AzureBlobArtifactStore, LocalArtifactStore
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
    def __init__(self, *, fail_once: bool = False, results=None) -> None:
        self.calls: list[str] = []
        self.fail_once = fail_once
        self.finalized = 0
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
    with pytest.raises(RuntimeFailure, match="interruption"):
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
    with pytest.raises(RuntimeFailure, match="Deployment failed"):
        ProductionOrchestrator(hooks, receipt).run(plan)
    state = read_receipt(receipt)
    assert state["status"] == "inconclusive"
    assert state["failed_phase"] == "deploy"
    assert hooks.finalized == 1


def test_orchestrator_cancels_queued_agents_after_first_failure(tmp_path: Path) -> None:
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
    with pytest.raises(RuntimeFailure, match="Deployment failed"):
        ProductionOrchestrator(
            hooks,
            tmp_path / "state.json",
            max_parallel_agents=1,
        ).run(plan)
    assert "attempt:b" not in hooks.calls
    assert hooks.calls.count("cancel") == 2


def test_orchestrator_sends_peer_cancellation_before_waiting(tmp_path: Path) -> None:
    peer_started = threading.Event()
    peer_released = threading.Event()

    class ParallelHooks(Hooks):
        def deploy(self, current, *, idempotency_key):
            if current.agent_id == "b":
                peer_started.set()
                if not peer_released.wait(timeout=2):
                    raise RuntimeFailure("peer_not_cancelled", "Peer was not cancelled promptly.")
                return self._value("deploy", idempotency_key)
            assert peer_started.wait(timeout=2)
            raise RuntimeFailure("deployment_failed", "Deployment failed.")

        def cancel(self, current):
            super().cancel(current)
            if current.agent_id == "b":
                peer_released.set()

    start = datetime(2026, 8, 21, tzinfo=UTC)
    plan = PlanInput(
        "aiq-20260821",
        "aiq-20260821",
        {
            "b": (work("b", "v1", start),),
            "a": (work("a", "v1", start),),
        },
    )
    hooks = ParallelHooks()
    with pytest.raises(RuntimeFailure, match="Deployment failed"):
        ProductionOrchestrator(
            hooks,
            tmp_path / "state.json",
            max_parallel_agents=2,
            cancellation_wait_seconds=2,
        ).run(plan)
    assert peer_released.is_set()


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
