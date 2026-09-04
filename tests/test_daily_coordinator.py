from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality import (
    daily_coordinator as coordinator,
    validation_approved,
    validation_manifest,
)
from agent_insights_quality.catalogs import catalog_hashes, load_catalogs
from agent_insights_quality.daily_lifecycle import (
    AGENT_ORDER,
    DailyLifecycle,
    DailyLock,
    DailyRecord,
    daily_runtime_root,
)
from agent_insights_quality.github_preview import preview_links
from agent_insights_quality.models import (
    AgentResult,
    InvocationEvidence,
    RequestCompletionEvidence,
    SemanticAssertionEvidence,
    TraceAssertionEvidence,
    VersionResult,
)
from agent_insights_quality.util import (
    ContractError,
    atomic_json,
    content_hash,
    file_hash,
    immutable_json,
)
from tests.test_daily_lifecycle import HASH, _initial


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


def test_daily_prepare_binds_snapshot_and_clean_checkout_without_staging_gate(
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
    checkout_commit = "2" * 40
    monkeypatch.setattr(coordinator, "current_clean_commit", lambda: checkout_commit)
    assert not hasattr(validation_approved, "fetch_approved_record_for_checkout")
    monkeypatch.setattr(
        validation_manifest,
        "current_validation_digest",
        lambda *_args, **_kwargs: pytest.fail(
            "Daily prepare must not compute a validation digest"
        ),
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
    assert active.value["bindings"]["checkout_commit_sha"] == checkout_commit
    assert "approval" not in active.value["bindings"]
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


def test_daily_prepare_requires_an_exact_clean_checkout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    private_root = tmp_path / ".aiq-runtime" / "agent-insights-quality"
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
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", str(private_root))
    monkeypatch.setattr(
        coordinator,
        "current_clean_commit",
        lambda: (_ for _ in ()).throw(ContractError("Checkout must be clean")),
    )

    with pytest.raises(ContractError, match="Checkout must be clean"):
        coordinator.prepare_daily(
            report_date=date(2026, 9, 1),
            work_items_path=work_items_path,
            base=private_root,
            now=lambda: datetime(2026, 9, 2, 4, tzinfo=UTC),
        )


def test_daily_agent_lane_is_resumable_and_writes_one_receipt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = {"test_region": "SwedenCentral", "synthetic": True}
    active = _prepared(tmp_path, registry)
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    calls = []

    monkeypatch.setattr(
        coordinator,
        "_assert_checkout_binding",
        lambda *_args, **_kwargs: None,
    )
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


def test_incomplete_baseline_keeps_lane_pending_with_resume_command(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = {"test_region": "SwedenCentral", "synthetic": True}
    active = _prepared(tmp_path, registry)
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    issue_ids = active.value["bindings"]["selection"]["finance-agent"]
    profile = SimpleNamespace(
        registry_path=tmp_path / "registry.json",
        assert_insights_connection=lambda: None,
        assert_test_agent_model=lambda _model: None,
    )
    monkeypatch.setattr(
        coordinator,
        "_assert_checkout_binding",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(coordinator, "load_catalogs", lambda: (agents, issues))
    monkeypatch.setattr(coordinator, "catalog_hashes", lambda *_args: hashes)
    monkeypatch.setattr(coordinator, "load_registry", lambda *_args, **_kwargs: registry)
    monkeypatch.setattr(
        coordinator,
        "execute_agent",
        lambda **_kwargs: AgentResult(
            "finance-agent",
            VersionResult(
                "v0",
                "v0",
                "inconclusive",
                error_code="baseline_evidence_incomplete",
            ),
            [
                VersionResult(issue_id, issue_id, "skipped_baseline")
                for issue_id in issue_ids
            ],
        ),
    )

    result = coordinator.run_daily_agent(
        "finance-agent",
        base=tmp_path,
        profile_factory=lambda _name: profile,
        runtime_factory=lambda _profile: object(),
    )
    guide = coordinator.daily_guide(base=tmp_path)
    finance = next(
        item for item in guide["agent_lanes"] if item["agent"] == "finance-agent"
    )

    assert result["status"] == "recovery_pending"
    assert result["remaining_recoveries"] == 3
    assert "finance-agent" in guide["pending_agent_lanes"]
    assert finance["command"].endswith("--agent finance-agent")
    assert finance["recovery"]["status"] == "recovery_pending"
    assert finance["recovery"]["remaining_recoveries"] == 3
    assert not coordinator._lane_receipt_path(
        active,
        tmp_path,
        "finance-agent",
    ).exists()


def test_exhausted_baseline_recovery_requires_daily_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = {"test_region": "SwedenCentral", "synthetic": True}
    active = _prepared(tmp_path, registry)
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    issue_ids = active.value["bindings"]["selection"]["finance-agent"]
    store = coordinator._checkpoint_store(active, tmp_path)
    for _ in range(3):
        assert store.claim_agent_recovery("finance-agent", 3)
    profile = SimpleNamespace(
        registry_path=tmp_path / "registry.json",
        assert_insights_connection=lambda: None,
        assert_test_agent_model=lambda _model: None,
    )
    monkeypatch.setattr(
        coordinator,
        "_assert_checkout_binding",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(coordinator, "load_catalogs", lambda: (agents, issues))
    monkeypatch.setattr(coordinator, "catalog_hashes", lambda *_args: hashes)
    monkeypatch.setattr(coordinator, "load_registry", lambda *_args, **_kwargs: registry)
    monkeypatch.setattr(
        coordinator,
        "execute_agent",
        lambda **_kwargs: AgentResult(
            "finance-agent",
            VersionResult(
                "v0",
                "v0",
                "inconclusive",
                error_code="baseline_evidence_incomplete",
            ),
            [
                VersionResult(issue_id, issue_id, "skipped_baseline")
                for issue_id in issue_ids
            ],
        ),
    )

    result = coordinator.run_daily_agent(
        "finance-agent",
        base=tmp_path,
        profile_factory=lambda _name: profile,
        runtime_factory=lambda _profile: object(),
    )
    guide = coordinator.daily_guide(base=tmp_path)

    assert result["status"] == "recovery_exhausted"
    assert result["remaining_recoveries"] == 0
    assert "daily-fail" in guide["next"]
    assert "finance-agent" in guide["pending_agent_lanes"]


def _reopen_finance_fixture(
    tmp_path: Path,
    active: DailyRecord,
    registry: dict,
    *,
    issue_traffic: bool = False,
    unhandled_errors: int = 0,
) -> tuple[dict, VersionResult]:
    summaries = [
        RequestCompletionEvidence(
            request_index=index,
            response_count=1,
            usable_response=True,
            semantic_assertion_count=1,
            semantic_assertions_passed=1,
            assertion_results=(
                SemanticAssertionEvidence("reviewed_semantic", True, True),
            ),
            activation_gate=True,
            direct_terminal_response_count=0,
            function_call_count=0,
            trace_assertion_count=int(index < 3),
            trace_assertions_passed=int(index < 3),
            trace_assertion_results=(
                (TraceAssertionEvidence("reviewed_trace", True, True),)
                if index < 3
                else ()
            ),
        )
        for index in range(10)
    ]
    operation_ids = [f"{index + 1:032x}" for index in range(10)]
    baseline = VersionResult(
        logical_version="v0",
        foundry_version="v0",
        status="inconclusive",
        operation_ids=operation_ids,
        error_code="baseline_evidence_failed",
        endpoint_request_count=10,
        endpoint_response_count=10,
        endpoint_usable_response_count=10,
        semantic_assertion_count=10,
        semantic_assertions_passed=10,
        trace_assertion_count=3,
        trace_assertions_passed=3,
        trace_contract_verified=True,
        trace_behavior_summary={
            "operation_count": 10,
            "terminal_response_count": 9,
            "terminal_success_count": 9,
            "terminal_output_count": 9,
            "explicit_terminal_success_count": 9,
            "explicit_terminal_output_count": 9,
            "unhandled_error_count": unhandled_errors,
        },
        endpoint_request_summaries=summaries,
    )
    issue_ids = active.value["bindings"]["selection"]["finance-agent"]
    issues = [
        VersionResult(
            issue_id,
            issue_id,
            "skipped_baseline",
            operation_ids=["f" * 32] if issue_traffic and index == 0 else [],
        )
        for index, issue_id in enumerate(issue_ids)
    ]
    receipt = coordinator._stamp_lane_receipt(
        active,
        "finance-agent",
        AgentResult("finance-agent", baseline, issues),
    )
    immutable_json(
        coordinator._lane_receipt_path(active, tmp_path, "finance-agent"),
        receipt,
    )
    store = coordinator._checkpoint_store(active, tmp_path)
    checkpoint_args = (
        "finance-agent",
        "v0",
        registry["agents"]["finance-agent"]["versions"]["v0"]["foundry_version"],
        registry["agents"]["finance-agent"]["versions"]["v0"]["content_digest"],
    )
    store.save_invocation(
        *checkpoint_args,
        InvocationEvidence(
            operation_ids=tuple(operation_ids),
            response_references=tuple(f"response-{index}" for index in range(10)),
            started_at="2026-09-03T18:00:00+00:00",
            completed_at="2026-09-03T18:01:00+00:00",
            request_count=10,
            allow_window_correlation=False,
            response_count=10,
            usable_response_count=10,
            semantic_assertion_count=10,
            semantic_assertions_passed=10,
            trace_assertion_count=3,
            trace_assertions_passed=3,
            request_summaries=tuple(summaries),
        ),
    )
    store.save_operation_ids(*checkpoint_args, tuple(operation_ids))
    store.save_trace_verified(*checkpoint_args)
    store.save_rejected_result(
        *checkpoint_args,
        baseline,
        drain_pending=False,
    )
    return receipt, baseline


def test_reopen_single_unknown_baseline_resumes_only_untouched_issues(
    monkeypatch,
    tmp_path: Path,
) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    registry = {
        "profile": "daily",
        "test_region": "SwedenCentral",
        "catalog_hashes": hashes,
        "agents": {
            agent["name"]: {
                "monitor_id": f"monitor-{agent['name']}",
                "versions": {
                    logical: {
                        "foundry_version": logical,
                        "content_digest": HASH,
                    }
                    for logical in ["v0", *agent["issue_ids"]]
                },
            }
            for agent in agents["agents"]
        },
    }
    active = _prepared(tmp_path, registry)
    original, _ = _reopen_finance_fixture(tmp_path, active, registry)
    profile = SimpleNamespace(
        registry_path=tmp_path / "registry.json",
        assert_insights_connection=lambda: None,
        assert_test_agent_model=lambda _model: None,
    )
    monkeypatch.setattr(coordinator, "current_clean_commit", lambda: "2" * 40)
    monkeypatch.setattr(coordinator, "load_catalogs", lambda: (agents, issues))
    monkeypatch.setattr(coordinator, "catalog_hashes", lambda *_args: hashes)
    monkeypatch.setattr(
        coordinator.RuntimeProfile,
        "from_env",
        lambda *_args: profile,
    )
    monkeypatch.setattr(coordinator, "load_registry", lambda *_args, **_kwargs: registry)

    reopened = coordinator.reopen_incomplete_daily_lane(
        "finance-agent",
        confirmed=True,
        base=tmp_path,
    )
    captured = {}

    def execute(**kwargs):
        captured.update(kwargs)
        baseline = kwargs["accepted_baseline"]
        issue_ids = active.value["bindings"]["selection"]["finance-agent"]
        return AgentResult(
            "finance-agent",
            baseline,
            [
                VersionResult(
                    issue_id,
                    issue_id,
                    "observed",
                    operation_ids=[f"{index + 20:032x}"],
                )
                for index, issue_id in enumerate(issue_ids)
            ],
        )

    monkeypatch.setattr(coordinator, "execute_agent", execute)
    completed = coordinator.run_daily_agent(
        "finance-agent",
        base=tmp_path,
        profile_factory=lambda _name: profile,
        runtime_factory=lambda _profile: object(),
    )
    path, effective = coordinator._effective_lane_receipt(
        active,
        tmp_path,
        "finance-agent",
    )

    assert reopened["status"] == "reopened"
    assert captured["accepted_baseline"].status == "passed"
    assert captured["accepted_baseline"].operation_ids == [
        f"{index + 1:032x}" for index in range(10)
    ]
    assert completed["status"] == "complete"
    assert path.name == "recovery-completion-receipt.json"
    assert effective["supersedes_receipt_digest"] == original["receipt_digest"]
    assert effective["result"]["baseline"]["trace_behavior_summary"][
        "terminal_response_count"
    ] == 9
    assert all(
        item["status"] == "observed" for item in effective["result"]["issues"]
    )
    claims = list(
        coordinator._lane_root(active, tmp_path, "finance-agent")
        .joinpath("worker-claims")
        .glob("*.json")
    )
    assert len(claims) == 1


@pytest.mark.parametrize(
    ("issue_traffic", "unhandled_errors", "catalog_mismatch", "message"),
    [
        (True, 0, False, "untouched incomplete-baseline lane"),
        (False, 1, False, "baseline evidence is not exact"),
        (False, 0, True, "Agent or traffic content changed"),
    ],
)
def test_reopen_rejects_changed_content_issue_traffic_or_definitive_failure(
    monkeypatch,
    tmp_path: Path,
    issue_traffic: bool,
    unhandled_errors: int,
    catalog_mismatch: bool,
    message: str,
) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    registry = {
        "profile": "daily",
        "test_region": "SwedenCentral",
        "catalog_hashes": hashes,
        "agents": {
            agent["name"]: {
                "monitor_id": f"monitor-{agent['name']}",
                "versions": {
                    logical: {
                        "foundry_version": logical,
                        "content_digest": HASH,
                    }
                    for logical in ["v0", *agent["issue_ids"]]
                },
            }
            for agent in agents["agents"]
        },
    }
    active = _prepared(tmp_path, registry)
    _reopen_finance_fixture(
        tmp_path,
        active,
        registry,
        issue_traffic=issue_traffic,
        unhandled_errors=unhandled_errors,
    )
    profile = SimpleNamespace(registry_path=tmp_path / "registry.json")
    monkeypatch.setattr(coordinator, "current_clean_commit", lambda: "2" * 40)
    monkeypatch.setattr(coordinator, "load_catalogs", lambda: (agents, issues))
    monkeypatch.setattr(
        coordinator,
        "catalog_hashes",
        lambda *_args: (
            {**hashes, "artifacts": "sha256:" + "b" * 64}
            if catalog_mismatch
            else hashes
        ),
    )
    monkeypatch.setattr(
        coordinator.RuntimeProfile,
        "from_env",
        lambda *_args: profile,
    )
    monkeypatch.setattr(coordinator, "load_registry", lambda *_args, **_kwargs: registry)

    with pytest.raises(ContractError, match=message):
        coordinator.reopen_incomplete_daily_lane(
            "finance-agent",
            confirmed=True,
            base=tmp_path,
        )


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


def test_daily_worker_rejects_stale_checkout_binding(tmp_path: Path) -> None:
    active = _prepared(tmp_path, {"test_region": "SwedenCentral"})
    stale_value = deepcopy(active.value)
    stale_value["bindings"]["checkout_commit_sha"] = "3" * 40
    stale = DailyRecord(active.path, stale_value, active.digest)

    with pytest.raises(ContractError, match="Stale Daily worker"):
        coordinator._daily_fence(
            stale,
            tmp_path,
            allowed_states={"PREPARED"},
        )


def test_daily_run_contract_binds_checkout_and_runtime_sources() -> None:
    registry = {"test_region": "SwedenCentral", "synthetic": True}
    exact_bindings = _initial()["bindings"]
    merged_bindings = deepcopy(exact_bindings)
    merged_bindings["checkout_commit_sha"] = "3" * 40

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
