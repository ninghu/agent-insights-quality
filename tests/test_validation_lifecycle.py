from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import threading

import pytest

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.util import ROOT, ContractError, content_hash
from agent_insights_quality.validation_cycle import (
    ValidationCycleController,
    initial_lifecycle,
)
from agent_insights_quality.validation_lifecycle import (
    LifecycleJournal,
    LocalValidationLock,
    validate_lifecycle,
    validate_topology_resource_bindings,
    validation_runtime_root,
)
from agent_insights_quality.validation_manifest import prepare_validation_plan
from agent_insights_quality.validation_policy import load_validation_policy

START = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _plan(run: str = "synthetic-run") -> dict:
    agents, issues = load_catalogs()
    policy = load_validation_policy()
    return prepare_validation_plan(
        agents=agents,
        issues=issues,
        policy=policy,
        repository=policy.repository,
        pr_number=999,
        commit_sha="a" * 40,
        local_run_id=run,
    )


def _initial(run: str = "synthetic-run") -> dict:
    policy = load_validation_policy()
    return initial_lifecycle(
        _plan(run),
        policy=policy,
        ownership_nonce="nonce-0001",
        holder_session_reference=content_hash("session"),
        holder_operator_reference=content_hash("operator"),
        holder_run_reference=content_hash("run"),
        substrate={
            "tenant_id": "synthetic-tenant",
            "subscription_id": "synthetic-subscription",
            "account_name": "synthetic-account",
            "account_resource_id": "/subscriptions/synthetic/account",
            "registry_name": "synthetic-registry",
            "storage_account_name": "synthetic-storage",
            "telemetry_resource_id": "/subscriptions/synthetic/telemetry",
        },
        now=START,
    )


def _authority_ids() -> list[str]:
    return [item["authority_id"] for item in _plan()["authorities"]]


def test_initial_run_binds_opaque_identity_and_has_no_cleanup_contract() -> None:
    value = _initial()
    validate_lifecycle(value)
    assert value["state"] == "LOCKED"
    assert value["run_id"].startswith("validation-")
    assert value["validation_authority_ids"] == _authority_ids()
    assert value["absolute_expires_at"] == "2026-09-01T12:00:00+00:00"
    assert "cleanup" not in value
    assert "clean_reference" not in value


def test_new_run_supersedes_incomplete_run_without_deleting_history(
    tmp_path: Path,
) -> None:
    lock = LocalValidationLock(tmp_path / "validation.lock")
    journal = LifecycleJournal(lock=lock, root=tmp_path / "lifecycle")
    with lock:
        first, forced = journal.begin_run(
            _initial("first"),
            all_authority_ids=_authority_ids(),
            now=START,
        )
        assert forced == []
        first = journal.commit(
            first,
            next_state="PREFLIGHT",
            now=START + timedelta(seconds=1),
        )
        second, forced = journal.begin_run(
            _initial("second"),
            all_authority_ids=_authority_ids(),
            now=START + timedelta(seconds=2),
        )
    assert forced == _authority_ids()
    assert second.value["run_id"] != first.value["run_id"]
    assert second.value["supersedes"].startswith("sha256:")
    history = [path.read_text(encoding="utf-8") for path in journal.root.rglob("*.json")]
    assert any('"SUPERSEDED"' in item for item in history)
    assert not list(journal.root.rglob(".*.tmp"))


def test_legacy_active_is_archived_byte_for_byte_then_tombstoned(
    tmp_path: Path,
) -> None:
    lock = LocalValidationLock(tmp_path / "validation.lock")
    journal = LifecycleJournal(lock=lock, root=tmp_path / "lifecycle")
    journal.active_path.parent.mkdir(parents=True)
    legacy = b'{"schema_version":"1.0.0","state":"VALIDATING"}\r\n'
    journal.active_path.write_bytes(legacy)
    with lock:
        active, forced = journal.begin_run(
            _initial(),
            all_authority_ids=_authority_ids(),
            now=START,
        )
    archives = list((journal.root / "superseded-formats").glob("*.json"))
    assert len(archives) == 1
    assert archives[0].read_bytes() == legacy
    assert forced == _authority_ids()
    assert active.value["supersedes"].startswith("sha256:")


def test_journal_requires_lock_and_atomically_replaces_active(tmp_path: Path) -> None:
    lock = LocalValidationLock(tmp_path / "validation.lock")
    journal = LifecycleJournal(lock=lock, root=tmp_path / "lifecycle")
    with pytest.raises(ContractError, match="process lock"):
        journal.begin_run(_initial(), all_authority_ids=_authority_ids())
    with lock:
        active, _ = journal.begin_run(
            _initial(),
            all_authority_ids=_authority_ids(),
        )
        updated = journal.commit(active, next_state="PREFLIGHT", now=START)
    assert updated.value["previous_journal_digest"] == active.value["journal_digest"]
    assert journal.read_active().digest == updated.digest
    assert not list(journal.root.rglob(".*.tmp"))


def test_execution_ttl_blocks_work_but_allows_supersession(tmp_path: Path) -> None:
    lock = LocalValidationLock(tmp_path / "validation.lock")
    journal = LifecycleJournal(lock=lock, root=tmp_path / "lifecycle")
    with lock:
        active, _ = journal.begin_run(
            _initial(),
            all_authority_ids=_authority_ids(),
        )
        after_ttl = START + timedelta(hours=73)
        with pytest.raises(ContractError, match="absolute TTL"):
            journal.commit(active, next_state="PREFLIGHT", now=after_ttl)
        superseded = journal.commit(
            active,
            next_state="SUPERSEDED",
            now=after_ttl,
        )
    assert superseded.value["state"] == "SUPERSEDED"


def test_dynamic_resources_are_retained_and_idempotently_observed(
    tmp_path: Path,
) -> None:
    lock = LocalValidationLock(tmp_path / "validation.lock")
    journal = LifecycleJournal(lock=lock, root=tmp_path / "lifecycle")
    event = {
        "state": "create_intent",
        "kind": "stored_response",
        "intent_reference": content_hash("response-intent"),
        "deterministic_name": "synthetic-response",
        "runtime_kind": "prompt",
        "discovery_key": "synthetic-response-intent",
        "authority_id": "weather-agent/v0",
        "parent_id": "synthetic-agent",
    }
    with lock:
        active, _ = journal.begin_run(
            _initial(),
            all_authority_ids=_authority_ids(),
        )
        controller = ValidationCycleController(journal, active=active)
        controller.dynamic_resource_event(event, now=START)
        controller.dynamic_resource_event(event, now=START)
        controller.dynamic_resource_event(
            {
                **event,
                "state": "created",
                "provider_id": "synthetic-response-id",
            },
            now=START,
        )
    resource = controller.active.value["resources"][0]
    assert resource["state"] == "created"
    assert resource["retention"] == "retained"
    assert "delete_intent_at" not in resource


def test_topology_bindings_require_every_retained_provider_identity() -> None:
    topology = {
        "runtime_principal_ids": ["principal"],
        "telemetry_identity_ids": ["version"],
        "agents": [
            {
                "provider_agent_id": "agent",
                "provider_agent_version_id": "version",
                "connection_ids": ["connection"],
            }
        ],
    }
    resources = [
        {"provider_id": value}
        for value in ("principal", "version", "agent", "connection")
    ]
    validate_topology_resource_bindings(topology, resources)
    with pytest.raises(ContractError, match="unjournaled"):
        validate_topology_resource_bindings(topology, resources[:-1])


def test_shared_process_lock_excludes_a_second_worktree(tmp_path: Path) -> None:
    path = tmp_path / "shared-runtime" / "validation.lock"
    first = LocalValidationLock(path)
    second = LocalValidationLock(path)
    first.acquire()
    try:
        with pytest.raises(ContractError, match="holds the shared lock"):
            second.acquire()
    finally:
        first.release()


def test_concurrent_prepare_cannot_publish_two_active_generations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "shared-runtime" / "validation.lock"
    acquired = threading.Event()
    release = threading.Event()
    result: list[str] = []

    def first() -> None:
        with LocalValidationLock(path):
            acquired.set()
            release.wait(timeout=5)
            result.append("first")

    thread = threading.Thread(target=first)
    thread.start()
    assert acquired.wait(timeout=5)
    try:
        with pytest.raises(ContractError, match="holds the shared lock"):
            LocalValidationLock(path).acquire()
    finally:
        release.set()
        thread.join(timeout=5)
    assert result == ["first"]


def test_validation_runtime_root_rejects_every_override(monkeypatch) -> None:
    monkeypatch.setenv(
        "AIQ_RUNTIME_ROOT",
        str(ROOT / ".aiq-runtime" / "agent-insights-quality"),
    )
    with pytest.raises(ContractError, match="does not permit"):
        validation_runtime_root()
