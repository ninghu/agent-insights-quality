from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.util import ContractError, content_hash
from agent_insights_quality.validation_cleanup import (
    CleanupEngine,
    CleanupInventory,
)
from agent_insights_quality.validation_cycle import (
    ValidationCycleController,
    initial_lifecycle,
)
from agent_insights_quality.validation_lifecycle import (
    LifecycleJournal,
    LocalValidationLock,
)
from agent_insights_quality.validation_manifest import prepare_validation_plan
from agent_insights_quality.validation_policy import load_validation_policy
from agent_insights_quality.validation_reconciler import ValidationReconciler

START = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _initial() -> dict:
    agents, issues = load_catalogs()
    policy = load_validation_policy()
    plan = prepare_validation_plan(
        agents=agents,
        issues=issues,
        policy=policy,
        repository=policy.repository,
        pr_number=999,
        commit_sha="a" * 40,
        local_run_id="synthetic-run",
    )
    return initial_lifecycle(
        plan,
        policy=policy,
        ownership_nonce="nonce-0001",
        holder_session_reference=content_hash("session"),
        holder_operator_reference=content_hash("operator"),
        holder_run_reference=content_hash("run"),
        account_reference=content_hash("account"),
        now=START,
    )


def test_shared_process_lock_excludes_a_second_worktree(tmp_path) -> None:
    path = tmp_path / "shared-runtime" / "validation.lock"
    first = LocalValidationLock(path)
    second = LocalValidationLock(path)
    first.acquire()
    try:
        with pytest.raises(ContractError, match="holds the shared lock"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_atomic_journal_requires_lock_and_writes_content_addressed_history(
    tmp_path,
) -> None:
    lock = LocalValidationLock(tmp_path / "validation.lock")
    journal = LifecycleJournal(lock=lock, root=tmp_path / "lifecycle")
    with pytest.raises(ContractError, match="process lock"):
        journal.begin_cycle(_initial())
    with lock:
        active = journal.begin_cycle(_initial())
        assert active.value["state"] == "LOCKED"
        history = sorted((journal.root / "history").rglob("*.json"))
        assert len(history) == 1
        committed = journal.commit(
            active,
            next_state="PREFLIGHT",
            now=START + timedelta(seconds=30),
        )
        assert committed.value["state"] == "PREFLIGHT"
        assert committed.value["previous_journal_digest"] == (
            active.value["journal_digest"]
        )
        assert len(list((journal.root / "history").rglob("*.json"))) == 2
        assert not list(journal.root.rglob(".*.tmp"))


def test_execution_ttl_blocks_work_but_not_cleanup(tmp_path) -> None:
    lock = LocalValidationLock(tmp_path / "validation.lock")
    journal = LifecycleJournal(lock=lock, root=tmp_path / "lifecycle")
    with lock:
        active = journal.begin_cycle(_initial())
        after_ttl = START + timedelta(hours=73)
        with pytest.raises(ContractError, match="absolute TTL"):
            journal.commit(active, next_state="PREFLIGHT", now=after_ttl)
        cleaning = journal.commit(
            active,
            next_state="CLEANING",
            now=after_ttl,
        )
        assert cleaning.value["state"] == "CLEANING"


def test_next_invocation_recovers_incomplete_journal_before_new_cycle(
    tmp_path,
) -> None:
    class Backend:
        def delete(self, _item) -> None:
            return None

        def absent(self, _item) -> bool:
            return True

        def manifest_is_shared(self, _provider_id: str) -> bool:
            return False

        def inventory(self, **_kwargs) -> CleanupInventory:
            return CleanupInventory(False, (), (), (), ())

    lock_path = tmp_path / "validation.lock"
    root = tmp_path / "lifecycle"
    first_lock = LocalValidationLock(lock_path)
    with first_lock:
        journal = LifecycleJournal(lock=first_lock, root=root)
        active = journal.begin_cycle(_initial())
        active = journal.commit(
            active,
            next_state="PREFLIGHT",
            now=START + timedelta(seconds=1),
        )
        controller = ValidationCycleController(journal, active=active)
        controller.project_create_intent(
            name="aiq-validation-synthetic",
            provider_id="synthetic-project-id",
            now=START + timedelta(seconds=2),
        )

    recovery_lock = LocalValidationLock(lock_path)
    with recovery_lock:
        journal = LifecycleJournal(lock=recovery_lock, root=root)
        state = ValidationReconciler(
            journal=journal,
            cleanup=CleanupEngine(Backend()),
            policy=load_validation_policy(),
        ).reconcile(alert=lambda _: None, now=START + timedelta(hours=80))
        assert state == "FAILED_CLEAN"
        recovered = journal.read_active()
        assert recovered.value["cleanup"]["exact_clean"] is True
        assert recovered.value["clean_reference"]["digest"].startswith("sha256:")
