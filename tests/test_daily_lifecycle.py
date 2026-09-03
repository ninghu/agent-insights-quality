from __future__ import annotations

import copy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from agent_insights_quality.automation_policy import load_automation_policy
from agent_insights_quality.catalogs import catalog_hashes, load_catalogs
from agent_insights_quality.daily_coordinator import _policy_binding
from agent_insights_quality.daily_lifecycle import (
    AGENT_ORDER,
    DailyLifecycle,
    DailyLock,
    daily_runtime_root,
)
from agent_insights_quality.selection import select_daily
from agent_insights_quality.util import (
    ContractError,
    atomic_json,
    content_hash,
    file_hash,
)

HASH = "sha256:" + ("a" * 64)


def _approval_binding(
    *,
    checkout_commit_sha: str = "1" * 40,
    approved_commit_sha: str = "1" * 40,
) -> dict:
    value = {
        "checkout_commit_sha": checkout_commit_sha,
        "approved_commit_sha": approved_commit_sha,
        "approved_pr_number": 65,
        "validation_digest": HASH,
        "evidence_digest": HASH,
        "approved_record_digest": HASH,
        "binding_digest": "",
    }
    value["binding_digest"] = content_hash(
        {key: item for key, item in value.items() if key != "binding_digest"}
    )
    return value


def _initial(report_date: date = date(2026, 8, 31)) -> dict:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selection = select_daily(report_date, agents, issues, hashes["issues"])
    moment = datetime(2026, 8, 31, 15, tzinfo=UTC).isoformat()
    return {
        "schema_version": "3.0.0",
        "kind": "daily-qualification-lifecycle",
        "snapshot_type": "event",
        "state": "LOCKED",
        "execution_id": "1" * 32,
        "event_sequence": 0,
        "started_at": moment,
        "last_activity_at": moment,
        "previous_lifecycle_digest": None,
        "event_reference": None,
        "superseded_format_digest": None,
        "bindings": {
            "repository": "ninghu/agent-insights-quality",
            "public_run_id": f"aiq-{report_date:%Y%m%d}",
            "report_date": report_date.isoformat(),
            "delivery_mode": "official",
            "publish_preview": False,
            "work_items": {
                "path": "work-items/snapshot.json",
                "content_digest": HASH,
                "closed_business_date": (report_date - timedelta(days=1)).isoformat(),
            },
            "approval": _approval_binding(),
            "catalog_hashes": hashes,
            "selection": selection,
            "policy": _policy_binding(load_automation_policy()),
            "registry": None,
            "run_contract_digest": None,
        },
        "artifacts": {
            "lane_receipts": {agent_name: None for agent_name in AGENT_ORDER},
            "manifest": None,
            "assessment_index": None,
            "improvement_input": None,
            "improvement_analysis": None,
            "final_report": None,
            "adx_publication_status": None,
            "email_request": None,
            "preview_publication": None,
            "send_claim": None,
            "email_receipt": None,
            "publication": None,
            "failure": None,
        },
        "lifecycle_digest": "",
    }


def _prepared(tmp_path: Path):
    lock = DailyLock(daily_runtime_root(tmp_path) / "coordinator.lock")
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=tmp_path)
        active = lifecycle.begin(_initial())
        return lifecycle.transition(
            active,
            next_state="PREPARED",
            binding_updates={
                "registry": {
                    "content_digest": HASH,
                    "project_name": "aiq-daily-swedencentral",
                    "test_region": "SwedenCentral",
                    "test_region_registry": "SwedenCentral",
                },
                "run_contract_digest": HASH,
            },
        )


def test_daily_lifecycle_is_quiescent_and_content_addressed(tmp_path: Path) -> None:
    active = _prepared(tmp_path)
    assert active.value["state"] == "PREPARED"
    assert active.value["execution_id"] == "1" * 32
    assert active.value["event_reference"]["digest"].startswith("sha256:")

    lock = DailyLock(daily_runtime_root(tmp_path) / "coordinator.lock")
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=tmp_path)
        conflicting = _initial(date(2026, 9, 1))
        with pytest.raises(ContractError, match="Another Daily lifecycle"):
            lifecycle.begin(conflicting)


def test_unreadable_daily_lifecycle_is_archived_once_and_tombstoned(
    tmp_path: Path,
) -> None:
    lock = DailyLock(daily_runtime_root(tmp_path) / "coordinator.lock")
    lifecycle = DailyLifecycle(lock=lock, base=tmp_path)
    lifecycle.active_path.parent.mkdir(parents=True)
    prior = b'{"schema_version":"1.0.0","state":"FAILED"}\r\n'
    lifecycle.active_path.write_bytes(prior)

    with lock:
        active = lifecycle.begin(_initial())
        repeated = lifecycle.begin(_initial())

    archives = list((lifecycle.root / "superseded-formats").glob("*.json"))
    assert len(archives) == 1
    assert archives[0].read_bytes() == prior
    assert file_hash(archives[0]) == active.value["superseded_format_digest"]
    assert archives[0].name == (
        f"{active.value['superseded_format_digest'].removeprefix('sha256:')}.json"
    )
    assert active.value["schema_version"] == "3.0.0"
    assert active.value["state"] == "LOCKED"
    assert repeated.digest == active.digest
    assert archives[0].is_file()
    assert not list((lifecycle.root / "superseded-formats").glob(".*.tmp"))


def test_valid_current_lifecycle_without_tombstone_field_still_blocks(
    tmp_path: Path,
) -> None:
    active = _prepared(tmp_path)
    legacy_current = copy.deepcopy(active.value)
    legacy_current.pop("superseded_format_digest")
    legacy_current["lifecycle_digest"] = content_hash(
        {
            key: item
            for key, item in legacy_current.items()
            if key != "lifecycle_digest"
        }
    )
    atomic_json(active.path, legacy_current)
    next_run = _initial(date(2026, 9, 1))
    next_run["execution_id"] = "2" * 32

    lock = DailyLock(daily_runtime_root(tmp_path) / "coordinator.lock")
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=tmp_path)
        assert lifecycle.read_active().value["state"] == "PREPARED"
        with pytest.raises(ContractError, match="Another Daily lifecycle"):
            lifecycle.begin(next_run)


def test_daily_lifecycle_rejects_noncanonical_transition(tmp_path: Path) -> None:
    active = _prepared(tmp_path)
    lock = DailyLock(daily_runtime_root(tmp_path) / "coordinator.lock")
    with lock:
        lifecycle = DailyLifecycle(lock=lock, base=tmp_path)
        with pytest.raises(ContractError, match="cannot transition"):
            lifecycle.transition(active, next_state="FINALIZED")


def test_daily_lifecycle_rejects_stale_approval_binding(tmp_path: Path) -> None:
    value = _initial()
    value["bindings"]["approval"]["approved_record_digest"] = (
        "sha256:" + ("b" * 64)
    )
    value["lifecycle_digest"] = content_hash(
        {key: item for key, item in value.items() if key != "lifecycle_digest"}
    )
    lock = DailyLock(daily_runtime_root(tmp_path) / "coordinator.lock")

    with lock, pytest.raises(ContractError, match="approval binding digest is stale"):
        DailyLifecycle(lock=lock, base=tmp_path).begin(value)
