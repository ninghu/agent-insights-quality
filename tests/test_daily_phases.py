from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality import daily_phases
from agent_insights_quality.catalogs import catalog_hashes, load_catalogs
from agent_insights_quality.daily_lifecycle import DailyLifecycle, DailyLock, daily_runtime_root
from agent_insights_quality.models import (
    InsightEvidence,
    InsightRunCheckpoint,
    InsightRunEvidence,
    VersionResult,
)
from agent_insights_quality.util import ContractError, content_hash, immutable_json
from tests.test_daily_coordinator import _prepared
from tests.test_daily_lifecycle import HASH
from tests.test_runner import FakeRuntime, _registry


def _context(monkeypatch, tmp_path: Path):
    agents, _ = load_catalogs()
    hashes = catalog_hashes(*load_catalogs())
    registry = _registry(agents, hashes)
    active = _prepared(tmp_path, registry)
    profile = SimpleNamespace(
        registry_path=tmp_path / "registry.json",
        assert_insights_connection=lambda: None,
        assert_test_agent_model=lambda _model: None,
    )
    monkeypatch.setattr(daily_phases, "load_registry", lambda *_args, **_kwargs: registry)
    monkeypatch.setattr(daily_phases, "_registry", lambda _active: registry)
    monkeypatch.setattr(
        daily_phases,
        "current_clean_commit",
        lambda: active.value["bindings"]["checkout_commit_sha"],
    )
    return active, profile, registry


def test_traffic_lane_is_issue_side_only_and_paces_versions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _, profile, _ = _context(monkeypatch, tmp_path)
    runtime = FakeRuntime()
    sleeps = []

    result = daily_phases.run_daily_traffic_agent(
        "weather-agent",
        base=tmp_path,
        profile_factory=lambda _name: profile,
        runtime_factory=lambda _profile: runtime,
        now=lambda: datetime(2026, 8, 24, 10, 1, tzinfo=UTC),
        sleeper=sleeps.append,
    )

    assert result["status"] == "traffic_complete"
    assert result["telemetry_queries"] == 0
    assert result["insight_runs"] == 0
    assert len(runtime.invoked) == 5
    assert runtime.invoked[0] == "v0"
    assert runtime.invoked.count("v0") == 1
    assert sleeps == [60.0] * 4
    receipts = list(
        (daily_runtime_root(tmp_path) / "runs").glob(
            "*/traffic/receipts/weather-agent/*.json"
        )
    )
    assert len(receipts) == 5
    assert all(
        len(
            daily_phases._invocation(
                daily_phases.read_json(path)["invocation"]
            ).response_references
        )
        == 20
        for path in receipts
    )


def test_traffic_barrier_opens_verification_only_after_25_receipts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    active, profile, _ = _context(monkeypatch, tmp_path)
    runtime = FakeRuntime()
    targets = daily_phases._targets(active)
    for target in targets:
        if target.agent_name == "weather-agent":
            continue
        requests = daily_phases.daily_issue_side_requests(target.traffic_path)
        invocation = runtime.invoke_version(
            agent_name=target.agent_name,
            agent_type=target.agent_type,
            foundry_version=target.foundry_version,
            traffic_path=target.traffic_path,
            seed=1,
            requests=requests,
        )
        immutable_json(
            daily_phases._traffic_receipt_path(active, tmp_path, target),
            daily_phases._traffic_receipt(
                active,
                target,
                invocation,
                requests=requests,
            ),
        )

    daily_phases.run_daily_traffic_agent(
        "weather-agent",
        base=tmp_path,
        profile_factory=lambda _name: profile,
        runtime_factory=lambda _profile: runtime,
        now=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
        sleeper=lambda _seconds: None,
    )

    lifecycle = DailyLifecycle(
        lock=DailyLock(daily_runtime_root(tmp_path) / "coordinator.lock"),
        base=tmp_path,
    ).read_active()
    assert lifecycle.value["state"] == "VERIFICATION"
    assert lifecycle.value["artifacts"]["traffic_manifest"] is not None


def test_lookback_rounds_up_and_includes_start_margin() -> None:
    value = daily_phases.compute_insight_lookback(
        traffic_started_at="2026-09-01T10:00:30+00:00",
        insight_started_at=datetime(2026, 9, 1, 11, 0, tzinfo=UTC),
        start_margin_seconds=30,
        precision_minutes=1,
        minimum_hours=0.1,
        maximum_hours=24,
    )

    assert value["lookback_hours"] == 1.0
    assert value["calculation_digest"] == content_hash(
        {
            key: item
            for key, item in value.items()
            if key != "calculation_digest"
        }
    )


def test_verification_claim_release_is_claimant_bound(
    monkeypatch,
    tmp_path: Path,
) -> None:
    active, _, _ = _context(monkeypatch, tmp_path)
    lock = DailyLock(daily_runtime_root(tmp_path) / "coordinator.lock")
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=tmp_path)
        traffic = lifecycle.transition(active, next_state="TRAFFIC")
        active = lifecycle.transition(
            traffic,
            next_state="VERIFICATION",
            artifact_updates={
                "traffic_manifest": {"path": "traffic.json", "digest": HASH}
            },
        )
    claim = daily_phases._claim_verification_target(
        active,
        tmp_path,
        "sha256:" + "1" * 64,
        now=lambda: datetime(2026, 9, 1, 10, tzinfo=UTC),
    )
    assert claim is not None

    assert (
        daily_phases._release_claim(
            active,
            tmp_path,
            "sha256:" + "2" * 64,
        )
        is None
    )
    released = daily_phases._release_claim(
        active,
        tmp_path,
        "sha256:" + "1" * 64,
        expected_claim=claim,
    )
    assert released is not None
    assert released["claim_digest"] == claim["claim_digest"]


def test_phase_status_does_not_mutate_expired_claim(
    monkeypatch,
    tmp_path: Path,
) -> None:
    active, _, _ = _context(monkeypatch, tmp_path)
    lock = DailyLock(daily_runtime_root(tmp_path) / "coordinator.lock")
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=tmp_path)
        traffic = lifecycle.transition(active, next_state="TRAFFIC")
        active = lifecycle.transition(
            traffic,
            next_state="VERIFICATION",
            artifact_updates={
                "traffic_manifest": {"path": "traffic.json", "digest": HASH}
            },
        )
    claim = daily_phases._claim_verification_target(
        active,
        tmp_path,
        "sha256:" + "9" * 64,
        now=lambda: datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert claim is not None
    target = daily_phases._identity_targets(active)[0]
    path = daily_phases._claim_path(active, tmp_path, target)

    progress = daily_phases.phase_progress(active, tmp_path)

    assert progress["verification_active_claim_count"] == 0
    assert path.is_file()


def test_completed_verification_result_cannot_be_released(
    monkeypatch,
    tmp_path: Path,
) -> None:
    active, _, _ = _context(monkeypatch, tmp_path)
    lock = DailyLock(daily_runtime_root(tmp_path) / "coordinator.lock")
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=tmp_path)
        traffic = lifecycle.transition(active, next_state="TRAFFIC")
        active = lifecycle.transition(
            traffic,
            next_state="VERIFICATION",
            artifact_updates={
                "traffic_manifest": {"path": "traffic.json", "digest": HASH}
            },
        )
    claimant = "sha256:" + "3" * 64
    claim = daily_phases._claim_verification_target(
        active,
        tmp_path,
        claimant,
        now=lambda: datetime(2026, 9, 1, 10, tzinfo=UTC),
    )
    assert claim is not None
    target = daily_phases._target_by_identity(
        active,
        claim["agent_name"],
        claim["logical_version"],
    )
    immutable_json(
        daily_phases._verification_result_path(active, tmp_path, target),
        {"synthetic": True},
    )

    with pytest.raises(ContractError, match="cannot be released"):
        daily_phases._release_claim(active, tmp_path, claimant)


def test_below_threshold_verification_fails_before_insights(
    monkeypatch,
    tmp_path: Path,
) -> None:
    active, profile, _ = _context(monkeypatch, tmp_path)
    lock = DailyLock(daily_runtime_root(tmp_path) / "coordinator.lock")
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=tmp_path)
        traffic_state = lifecycle.transition(active, next_state="TRAFFIC")
        active = lifecycle.transition(
            traffic_state,
            next_state="VERIFICATION",
            artifact_updates={
                "traffic_manifest": {"path": "traffic.json", "digest": HASH}
            },
        )
    target = daily_phases._targets(active)[0]
    requests = daily_phases.daily_issue_side_requests(target.traffic_path)
    invocation = FakeRuntime().invoke_version(
        agent_name=target.agent_name,
        agent_type=target.agent_type,
        foundry_version=target.foundry_version,
        traffic_path=target.traffic_path,
        seed=1,
        requests=requests,
    )
    immutable_json(
        daily_phases._traffic_receipt_path(active, tmp_path, target),
        daily_phases._traffic_receipt(
            active,
            target,
            invocation,
            requests=requests,
        ),
    )
    monkeypatch.setattr(
        daily_phases,
        "_verify_target",
        lambda *_args, **_kwargs: VersionResult(
            logical_version=target.logical_version,
            foundry_version=target.foundry_version,
            status="failed",
            error_code="baseline_evidence_failed",
        ),
    )

    result = daily_phases.verify_next_daily_target(
        base=tmp_path,
        profile_factory=lambda _name: profile,
        runtime_factory=lambda _profile: SimpleNamespace(),
        claimant_reference="sha256:" + "4" * 64,
    )

    assert result["status"] == "verification_failed"
    assert result["insight_runs"] == 0
    assert (
        DailyLifecycle(
            lock=DailyLock(daily_runtime_root(tmp_path) / "coordinator.lock"),
            base=tmp_path,
        ).read_active().value["state"]
        == "FAILED"
    )


def test_no_claimable_target_reconciles_completed_verification_barrier(
    monkeypatch,
    tmp_path: Path,
) -> None:
    active, _, _ = _context(monkeypatch, tmp_path)
    lock = DailyLock(daily_runtime_root(tmp_path) / "coordinator.lock")
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=tmp_path)
        traffic = lifecycle.transition(active, next_state="TRAFFIC")
        lifecycle.transition(
            traffic,
            next_state="VERIFICATION",
            artifact_updates={
                "traffic_manifest": {"path": "traffic.json", "digest": HASH}
            },
        )
    monkeypatch.setattr(
        daily_phases,
        "_claim_verification_target",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        daily_phases,
        "_reconcile_verification_barrier",
        lambda *_args, **_kwargs: SimpleNamespace(value={"state": "INSIGHTS"}),
    )

    result = daily_phases.verify_next_daily_target(
        base=tmp_path,
        claimant_reference="sha256:" + "8" * 64,
    )

    assert result == {
        "status": "verification_complete",
        "retryable": False,
        "state": "INSIGHTS",
    }


def test_monitor_reset_is_exactly_once_per_agent_and_resume_is_idempotent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    active, _, _ = _context(monkeypatch, tmp_path)
    lock = DailyLock(daily_runtime_root(tmp_path) / "coordinator.lock")
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=tmp_path)
        traffic = lifecycle.transition(active, next_state="TRAFFIC")
        verification = lifecycle.transition(
            traffic,
            next_state="VERIFICATION",
            artifact_updates={
                "traffic_manifest": {"path": "traffic.json", "digest": HASH}
            },
        )
        active = lifecycle.transition(
            verification,
            next_state="INSIGHTS",
            artifact_updates={
                "verification_manifest": {
                    "path": "verification.json",
                    "digest": HASH,
                }
            },
        )
    resets = []
    runtime = SimpleNamespace(
        reset_monitor=lambda agent_name, _monitor_id: resets.append(agent_name)
    )
    def clock() -> datetime:
        return datetime(2026, 9, 1, 12, tzinfo=UTC)

    for agent_name in daily_phases.AGENT_ORDER:
        daily_phases._ensure_agent_monitor_reset(
            active,
            tmp_path,
            agent_name,
            runtime,
            monitor_id=f"monitor-{agent_name}",
            now=clock,
        )
        daily_phases._ensure_agent_monitor_reset(
            active,
            tmp_path,
            agent_name,
            runtime,
            monitor_id=f"monitor-{agent_name}",
            now=clock,
        )

    assert resets == list(daily_phases.AGENT_ORDER)


def test_insight_agent_autonomously_processes_five_versions_and_resumes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    active, profile, _ = _context(monkeypatch, tmp_path)
    lock = DailyLock(daily_runtime_root(tmp_path) / "coordinator.lock")
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=tmp_path)
        traffic_state = lifecycle.transition(active, next_state="TRAFFIC")
        verification_state = lifecycle.transition(
            traffic_state,
            next_state="VERIFICATION",
            artifact_updates={
                "traffic_manifest": {"path": "traffic.json", "digest": HASH}
            },
        )
        active = lifecycle.transition(
            verification_state,
            next_state="INSIGHTS",
            artifact_updates={
                "verification_manifest": {
                    "path": "verification.json",
                    "digest": HASH,
                }
            },
        )
    targets = daily_phases._targets(active, "weather-agent")
    traffic_runtime = FakeRuntime()
    for target_index, target in enumerate(targets):
        requests = daily_phases.daily_issue_side_requests(target.traffic_path)
        invocation = traffic_runtime.invoke_version(
            agent_name=target.agent_name,
            agent_type=target.agent_type,
            foundry_version=target.foundry_version,
            traffic_path=target.traffic_path,
            seed=1,
            requests=requests,
        )
        traffic = daily_phases._traffic_receipt(
            active,
            target,
            invocation,
            requests=requests,
        )
        immutable_json(
            daily_phases._traffic_receipt_path(active, tmp_path, target),
            traffic,
        )
        result = VersionResult(
            logical_version=target.logical_version,
            foundry_version=target.foundry_version,
            status="passed" if target.logical_version == "v0" else "observed",
            operation_ids=[f"{target_index + 1:032x}"],
            endpoint_request_count=len(requests),
            endpoint_response_count=len(requests),
            endpoint_usable_response_count=len(requests),
            trace_contract_verified=True,
        )
        verification = daily_phases._verification_result(
            active,
            target,
            traffic,
            {"claim_digest": content_hash(target.logical_version)},
            result,
        )
        immutable_json(
            daily_phases._verification_result_path(active, tmp_path, target),
            verification,
        )

    class InsightRuntime:
        def __init__(self):
            self.resets = []
            self.starts = []
            self.finishes = []

        def reset_monitor(self, agent_name, _monitor_id):
            self.resets.append(agent_name)

        def start_insights_run(self, *, foundry_version, persist, **_kwargs):
            self.starts.append(foundry_version)
            checkpoint = InsightRunCheckpoint(
                f"run-{foundry_version}",
                {},
            )
            persist(checkpoint)
            return checkpoint

        def finish_insights_run(
            self,
            *,
            foundry_version,
            operation_ids,
            checkpoint,
            **_kwargs,
        ):
            self.finishes.append(foundry_version)
            cards = ()
            if foundry_version != "v0":
                cards = tuple(
                    InsightEvidence(
                        reference=content_hash(f"{checkpoint.run_id}-{index}"),
                        agent_version=foundry_version,
                        title="Synthetic card",
                        description="Synthetic defect.",
                        category="reliability_errors",
                        severity="medium",
                        proposed_fix="Use the healthy implementation.",
                        linked_operation_ids=operation_ids,
                        trace_count=len(operation_ids),
                        updated_at="2026-08-24T10:30:00+00:00",
                    )
                    for index in range(2)
                )
            return InsightRunEvidence(
                run_reference=content_hash(checkpoint.run_id),
                window_start="2026-08-24T09:00:00+00:00",
                window_end="2026-08-24T10:30:00+00:00",
                status="succeeded",
                insights=cards,
            )

        def trace_behavior_evidence(self, operation_ids):
            return {"operation_count": len(operation_ids)}

    runtime = InsightRuntime()
    def clock() -> datetime:
        return datetime(2026, 8, 24, 10, 30, tzinfo=UTC)
    first = daily_phases.run_daily_insights_agent(
        "weather-agent",
        base=tmp_path,
        profile_factory=lambda _name: profile,
        runtime_factory=lambda _profile: runtime,
        now=clock,
    )
    second = daily_phases.run_daily_insights_agent(
        "weather-agent",
        base=tmp_path,
        profile_factory=lambda _name: profile,
        runtime_factory=lambda _profile: runtime,
        now=clock,
    )

    assert first["completed_target_count"] == 5
    assert second["completed_target_count"] == 5
    assert runtime.resets == ["weather-agent"]
    assert runtime.starts == [target.foundry_version for target in targets]
    assert runtime.finishes == [target.foundry_version for target in targets]
    issue_receipt = daily_phases.read_json(
        daily_phases._insight_receipt_path(
            active,
            tmp_path,
            targets[1],
        )
    )
    assert issue_receipt["result"]["status"] == "observed"
    assert len(issue_receipt["result"]["observed_insights"]) == 2
