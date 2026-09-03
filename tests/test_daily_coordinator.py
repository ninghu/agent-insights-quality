from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality import daily_coordinator as coordinator
from agent_insights_quality.catalogs import catalog_hashes, load_catalogs
from agent_insights_quality.daily_lifecycle import (
    AGENT_ORDER,
    DailyLifecycle,
    DailyLock,
    DailyRecord,
    daily_runtime_root,
)
from agent_insights_quality.github_preview import preview_links
from agent_insights_quality.models import AgentResult, VersionResult
from agent_insights_quality.util import (
    ContractError,
    atomic_json,
    content_hash,
    file_hash,
)
from tests.test_daily_lifecycle import HASH, _approval_binding, _initial


def _prepared(tmp_path: Path, registry: dict):
    initial = _initial()
    initial["bindings"]["registry"] = None
    lock = DailyLock(daily_runtime_root(tmp_path) / "coordinator.lock")
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=tmp_path)
        active = lifecycle.begin(initial)
        return lifecycle.transition(
            active,
            next_state="PREPARED",
            binding_updates={
                "registry": {
                    "content_digest": content_hash(registry),
                    "project_name": "aiq-daily-swedencentral",
                    "test_region": "SwedenCentral",
                    "test_region_registry": "SwedenCentral",
                },
                "run_contract_digest": HASH,
            },
        )


def test_daily_guide_returns_visible_whole_agent_lanes(tmp_path: Path) -> None:
    _prepared(tmp_path, {"test_region": "SwedenCentral"})
    guide = coordinator.daily_guide(base=tmp_path)

    assert guide["internal_fanout"] is False
    assert guide["max_parallel_agents"] == 5
    assert [item["agent"] for item in guide["agent_lanes"]] == list(AGENT_ORDER)
    assert all(len(item["issues"]) == 4 for item in guide["agent_lanes"])
    assert all("daily-run-agent" in item["command"] for item in guide["agent_lanes"])


def test_daily_prepare_binds_pacific_date_snapshot_and_approved_record(
    monkeypatch,
    tmp_path: Path,
) -> None:
    private_root = tmp_path / ".aiq-runtime" / "agent-insights-quality"
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", str(private_root))
    work_items_path = private_root / "work-items" / "snapshot.json"
    atomic_json(
        work_items_path,
        {
            "schema_version": "2.0.0",
            "query_reference": HASH,
            "closed_business_date": "2026-08-31",
            "active_items": [],
            "closed_yesterday_items": [],
        },
    )
    monkeypatch.setattr(coordinator, "current_clean_commit", lambda: "1" * 40)
    monkeypatch.setattr(coordinator, "current_validation_digest", lambda *_args: HASH)
    approval = _approval_binding(
        approved_commit_sha="2" * 40,
    )
    monkeypatch.setattr(
        coordinator,
        "_fetch_approved_record",
        lambda: approval,
    )

    status = coordinator.prepare_daily(
        report_date=date(2026, 9, 1),
        work_items_path=work_items_path,
        base=private_root,
        now=lambda: datetime(2026, 9, 2, 4, tzinfo=UTC),
    )

    assert status["state"] == "LOCKED"
    assert status["report_date"] == "2026-09-01"
    assert "execution_id" not in status
    active = coordinator._read_active(private_root, allowed_states={"LOCKED"})
    assert active.value["bindings"]["work_items"]["content_digest"].startswith(
        "sha256:"
    )
    assert active.value["bindings"]["approval"] == approval
    assert active.value["bindings"]["approval"]["checkout_commit_sha"] == "1" * 40
    assert active.value["bindings"]["approval"]["approved_commit_sha"] == "2" * 40
    assert active.value["bindings"]["approval"]["approved_pr_number"] == 65
    assert active.value["bindings"]["approval"]["evidence_digest"] == HASH
    assert status["publish_preview"] is False


def test_daily_prepare_gates_preview_to_nonzero_email_test() -> None:
    with pytest.raises(ContractError, match="requires --test-run"):
        coordinator.prepare_daily(
            report_date=date(2026, 9, 1),
            work_items_path=Path("unused.json"),
            publish_preview=True,
            now=lambda: datetime(2026, 9, 1, 15, tzinfo=UTC),
        )
    with pytest.raises(ContractError, match="nonzero --rerun"):
        coordinator.prepare_daily(
            report_date=date(2026, 9, 1),
            work_items_path=Path("unused.json"),
            test_run=True,
            publish_preview=True,
            now=lambda: datetime(2026, 9, 1, 15, tzinfo=UTC),
        )


def test_daily_finalization_binds_preview_and_whole_email_request(
    monkeypatch,
    tmp_path: Path,
) -> None:
    run_id = "aiq-20260901-r01"
    run_root = daily_runtime_root(tmp_path) / "executions" / "synthetic"
    report_path = run_root / "final-report" / "report.json"
    improvement_path = run_root / "improvement.json"
    preview_path = run_root / "github-preview-publication.json"
    request_path = run_root / "email-send-request.json"
    links = preview_links(run_id)
    publication = {
        "schema_version": "1.0.0",
        "kind": "daily-email-test-preview-publication",
        **links,
        "created_at": "2026-09-01T16:00:00+00:00",
        "commit_sha": "1" * 40,
        "content_digest": "sha256:" + "2" * 64,
        "manifest_digest": "sha256:" + "3" * 64,
    }
    atomic_json(report_path, {"synthetic": True})
    atomic_json(improvement_path, {"synthetic": True})
    atomic_json(preview_path, publication)
    atomic_json(
        request_path,
        {
            "content_digest": "sha256:" + "4" * 64,
            "preview": publication,
        },
    )
    active = SimpleNamespace(
        value={
            "bindings": {
                "publish_preview": True,
                "public_run_id": run_id,
            }
        }
    )
    updates = []
    monkeypatch.setattr(
        coordinator,
        "_transition_exact",
        lambda *_args, **_kwargs: updates.append(_args[3]),
    )

    coordinator.record_daily_finalization(
        active,
        report_path=report_path,
        email_request_path=request_path,
        improvement_analysis_path=improvement_path,
        adx_publication_status="skipped_test",
        preview_publication_path=preview_path,
        base=tmp_path,
    )

    assert updates[0]["preview_publication"]["digest"] == file_hash(preview_path)
    assert updates[0]["email_request"]["digest"] == file_hash(request_path)


def test_daily_status_safely_requires_unreadable_format_supersession(
    tmp_path: Path,
) -> None:
    active_path = daily_runtime_root(tmp_path) / "active.json"
    active_path.parent.mkdir(parents=True)
    active_path.write_bytes(
        b'{"schema_version":"1.0.0","private_path":"do-not-expose"}\n'
    )

    assert coordinator.daily_status(base=tmp_path) == {
        "state": "FORMAT_REQUIRES_SUPERSESSION",
        "next": "daily-prepare",
    }


def test_daily_prepare_rejects_non_pacific_business_date(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="Pacific business date"):
        coordinator.prepare_daily(
            report_date=date(2026, 8, 31),
            work_items_path=tmp_path / "not-read.json",
            base=tmp_path,
            now=lambda: datetime(2026, 9, 2, 4, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "staging"},
        {"environment_id": "westus2-g29"},
        {"location": "westus2"},
        {"telemetry_resource_set": "g29"},
    ],
)
def test_daily_approval_fetch_rejects_non_g30_profile(
    monkeypatch,
    overrides: dict,
) -> None:
    values = {
        "name": "daily",
        "environment_id": "swedencentral-g30",
        "location": "swedencentral",
        "telemetry_resource_set": "g30",
        "registry_storage_account_name": "aiqsweartsynthetic",
    }
    values.update(overrides)
    monkeypatch.setattr(
        coordinator.RuntimeProfile,
        "from_env",
        lambda *_args: SimpleNamespace(**values),
    )

    with pytest.raises(ContractError, match="reviewed Sweden g30"):
        coordinator._fetch_approved_record()


def test_daily_agent_lane_is_resumable_and_writes_one_receipt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = {"test_region": "SwedenCentral", "synthetic": True}
    active = _prepared(tmp_path, registry)
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    calls = []

    monkeypatch.setattr(coordinator, "_assert_checkout_binding", lambda _active: None)
    monkeypatch.setattr(coordinator, "load_catalogs", lambda: (agents, issues))
    monkeypatch.setattr(coordinator, "catalog_hashes", lambda *_args: hashes)
    monkeypatch.setattr(coordinator, "load_registry", lambda *_args, **_kwargs: registry)
    profile = SimpleNamespace(
        registry_path=tmp_path / "registry.json",
        assert_insights_connection=lambda: None,
        assert_test_agent_model=lambda _model: None,
    )

    def execute(**kwargs):
        calls.append(kwargs)
        issue_ids = active.value["bindings"]["selection"]["weather-agent"]
        return AgentResult(
            "weather-agent",
            VersionResult("v0", "v0", "passed", operation_ids=["1" * 32]),
            [
                VersionResult(
                    issue_id,
                    issue_id,
                    "inconclusive",
                    operation_ids=[f"{index + 2:032x}"],
                )
                for index, issue_id in enumerate(issue_ids)
            ],
        )

    monkeypatch.setattr(coordinator, "execute_agent", execute)
    first = coordinator.run_daily_agent(
        "weather-agent",
        base=tmp_path,
        profile_factory=lambda _name: profile,
        runtime_factory=lambda _profile: object(),
    )
    second = coordinator.run_daily_agent(
        "weather-agent",
        base=tmp_path,
        profile_factory=lambda _name: profile,
        runtime_factory=lambda _profile: object(),
    )

    assert first["status"] == "complete"
    assert second["status"] == "already_complete"
    assert len(calls) == 1
    assert calls[0]["agent_name"] == "weather-agent"
    assert calls[0]["start_delay_seconds"] == 0
    assert coordinator.daily_status(base=tmp_path)["state"] == "TRAFFIC"


def test_daily_lane_receipt_rejects_reused_operation_identity(tmp_path: Path) -> None:
    active = _prepared(tmp_path, {"test_region": "SwedenCentral"})
    issue_ids = active.value["bindings"]["selection"]["weather-agent"]
    result = AgentResult(
        "weather-agent",
        VersionResult("v0", "v0", "passed", operation_ids=["1" * 32]),
        [
            VersionResult(
                issue_id,
                issue_id,
                "inconclusive",
                operation_ids=["1" * 32],
            )
            for issue_id in issue_ids
        ],
    )
    receipt = coordinator._stamp_lane_receipt(active, "weather-agent", result)

    with pytest.raises(ContractError, match="reused an operation"):
        coordinator._validate_lane_receipt(receipt, active, "weather-agent")


def test_daily_runtime_sources_have_no_internal_thread_fanout() -> None:
    for relative in (
        "src/agent_insights_quality/runner.py",
        "src/agent_insights_quality/live.py",
        "src/agent_insights_quality/daily_coordinator.py",
    ):
        text = Path(relative).read_text(encoding="utf-8")
        assert "ThreadPoolExecutor" not in text
        assert "as_completed" not in text


def test_explicit_daily_failure_releases_next_business_date(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path, {"test_region": "SwedenCentral"})
    with pytest.raises(ContractError, match="explicit --confirm"):
        coordinator.fail_daily(
            reason_code="operator_stopped",
            confirmed=False,
            base=tmp_path,
        )

    status = coordinator.fail_daily(
        reason_code="operator_stopped",
        confirmed=True,
        base=tmp_path,
        now=lambda: datetime(2026, 8, 31, 16, tzinfo=UTC),
    )
    assert status["state"] == "FAILED"
    with pytest.raises(ContractError, match="Stale Daily worker"):
        coordinator._daily_fence(
            prepared,
            tmp_path,
            allowed_states={"PREPARED"},
        )

    next_run = _initial(date(2026, 9, 1))
    next_run["execution_id"] = "2" * 32
    lock = DailyLock(daily_runtime_root(tmp_path) / "coordinator.lock")
    with lock:
        active = DailyLifecycle(lock=lock, base=tmp_path).begin(next_run)
    assert active.value["state"] == "LOCKED"
    assert active.value["bindings"]["report_date"] == "2026-09-01"


def test_daily_capacity_enforces_configured_parallel_limit(tmp_path: Path) -> None:
    active = _prepared(tmp_path, {"test_region": "SwedenCentral"})
    locks = [
        coordinator._acquire_lane_capacity(active, tmp_path)
        for _ in range(5)
    ]
    try:
        with pytest.raises(ContractError, match="parallel Agent limit"):
            coordinator._acquire_lane_capacity(active, tmp_path)
    finally:
        for lock in locks:
            lock.release()


def test_daily_worker_rejects_stale_source_approval_binding(tmp_path: Path) -> None:
    active = _prepared(tmp_path, {"test_region": "SwedenCentral"})
    stale_value = deepcopy(active.value)
    stale_value["bindings"]["approval"] = _approval_binding(
        approved_commit_sha="3" * 40,
    )
    stale = DailyRecord(active.path, stale_value, active.digest)

    with pytest.raises(ContractError, match="Stale Daily worker"):
        coordinator._daily_fence(
            stale,
            tmp_path,
            allowed_states={"PREPARED"},
        )


def test_daily_run_contract_binds_checkout_and_source_approval() -> None:
    registry = {"test_region": "SwedenCentral", "synthetic": True}
    exact_bindings = _initial()["bindings"]
    merged_bindings = deepcopy(exact_bindings)
    merged_bindings["approval"] = _approval_binding(
        approved_commit_sha="3" * 40,
    )

    exact_digest = coordinator._daily_contract_digest(
        bindings=exact_bindings,
        registry=registry,
        test_region="SwedenCentral",
    )
    merged_digest = coordinator._daily_contract_digest(
        bindings=merged_bindings,
        registry=registry,
        test_region="SwedenCentral",
    )
    superseded_digest = coordinator._daily_contract_digest(
        bindings=exact_bindings,
        registry=registry,
        test_region="SwedenCentral",
        superseded_format_digest=HASH,
    )

    assert exact_digest != merged_digest
    assert exact_digest != superseded_digest
