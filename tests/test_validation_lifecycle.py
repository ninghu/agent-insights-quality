from __future__ import annotations

from datetime import UTC, datetime, timedelta
from dataclasses import replace

import pytest

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.provisioning import RemoteHttpError
from agent_insights_quality.util import ROOT, ContractError, content_hash
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
    validation_runtime_root,
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


def _support_images() -> dict[str, str]:
    return {
        ("v0" if index == 0 else f"issue-{index + 28:03d}"): (
            "synthetic.azurecr.io/agent-insights-quality-support@"
            f"sha256:{index:064x}"
        )
        for index in range(9)
    }


def _prompt_runtime() -> dict:
    return {
        "authority_id": "weather-agent/v0",
        "canonical_agent": "weather-agent",
        "logical_version": "v0",
        "runtime_kind": "prompt",
        "framework": "foundry_prompt",
        "runtime_agent_name": "weather-agent-baseline-synthetic",
        "runtime_agent_version": "1",
        "provider_agent_id": "synthetic-agent",
        "provider_agent_version_id": "synthetic-version",
        "hosted_identity_id": None,
        "hosted_blueprint_id": None,
        "hosted_deployment_id": None,
        "foundry_agent_name": "weather-agent-baseline-synthetic",
        "foundry_agent_version": "1",
        "runtime_principal_id": None,
        "telemetry_identity_id": "synthetic-version",
        "connection_ids": [],
    }


def _hosted_runtime() -> dict:
    return {
        "authority_id": "finance-agent/v0",
        "canonical_agent": "finance-agent",
        "logical_version": "v0",
        "runtime_kind": "hosted_code",
        "framework": "microsoft_agent_framework",
        "runtime_agent_name": "finance-agent-baseline-synthetic",
        "runtime_agent_version": "1",
        "provider_agent_id": "synthetic-finance-agent",
        "provider_agent_version_id": "synthetic-finance-version",
        "hosted_identity_id": "synthetic-finance-identity",
        "hosted_blueprint_id": "synthetic-finance-blueprint",
        "hosted_deployment_id": "synthetic-finance-deployment",
        "foundry_agent_name": "finance-agent-baseline-synthetic",
        "foundry_agent_version": "1",
        "runtime_principal_id": "synthetic-finance-principal",
        "telemetry_identity_id": "synthetic-finance-version",
        "connection_ids": [],
    }


def test_partial_deployment_progress_persists_recovery_and_ready_state(
    tmp_path,
) -> None:
    lock = LocalValidationLock(tmp_path / "validation.lock")
    journal = LifecycleJournal(lock=lock, root=tmp_path / "lifecycle")
    with lock:
        active = journal.begin_cycle(_initial())
        controller = ValidationCycleController(journal, active=active)
        controller.support_images_ready(_support_images(), now=START)
        controller.authority_recovery(
            authority_id="weather-agent/v0",
            canonical_agent="weather-agent",
            state="ambiguous",
            retry_count=1,
            error_code="interrupted_deployment",
            now=START + timedelta(seconds=1),
        )
        controller.authority_ready(
            _prompt_runtime(),
            now=START + timedelta(seconds=2),
        )
        deployment = controller.active.value["deployment"]
        assert deployment["traffic_started"] is False
        assert deployment["recoveries"] == [
            {
                "authority_id": "weather-agent/v0",
                "canonical_agent": "weather-agent",
                "state": "ready",
                "retry_count": 1,
                "error_code": "interrupted_deployment",
            }
        ]
        assert controller.active.value["runtime_topology"]["agents"] == [
            _prompt_runtime()
        ]
        with pytest.raises(ContractError, match="41 deployed"):
            controller.begin_validation(
                [_prompt_runtime()],
                now=START + timedelta(seconds=3),
            )


def test_resumed_resource_intent_does_not_duplicate_or_downgrade_ready(
    tmp_path,
) -> None:
    lock = LocalValidationLock(tmp_path / "validation.lock")
    journal = LifecycleJournal(lock=lock, root=tmp_path / "lifecycle")
    event = {
        "state": "create_intent",
        "kind": "provider_agent",
        "intent_reference": content_hash("agent-intent"),
        "deterministic_name": "synthetic-agent",
        "runtime_kind": "prompt",
        "discovery_key": "synthetic-agent|v0|provider_agent",
        "authority_id": "weather-agent/v0",
        "parent_id": None,
        "cleanup_method": "explicit",
    }
    with lock:
        controller = ValidationCycleController(
            journal,
            active=journal.begin_cycle(_initial()),
        )
        controller.dynamic_resource_event(event, now=START)
        revision = controller.active.value["revision"]
        controller.dynamic_resource_event(event, now=START)
        assert controller.active.value["revision"] == revision
        controller.dynamic_resource_event(
            {
                **event,
                "state": "created",
                "provider_id": "synthetic-provider-agent",
            },
            now=START + timedelta(seconds=1),
        )
        controller.dynamic_resource_event(
            {**event, "state": "ambiguous_create"},
            now=START + timedelta(seconds=2),
        )
        resource = controller.active.value["resources"][0]
        assert resource["state"] == "created"
        assert resource["provider_id"] == "synthetic-provider-agent"


def test_phase_two_requires_both_phase_one_baselines_without_failures(
    tmp_path,
) -> None:
    lock = LocalValidationLock(tmp_path / "validation.lock")
    journal = LifecycleJournal(lock=lock, root=tmp_path / "lifecycle")
    with lock:
        controller = ValidationCycleController(
            journal,
            active=journal.begin_cycle(_initial()),
        )
        with pytest.raises(ContractError, match="phase 1 did not authorize"):
            controller.begin_phase_two_deployment(now=START)
        controller.authority_ready(_prompt_runtime(), now=START)
        controller.authority_ready(
            _hosted_runtime(),
            now=START + timedelta(seconds=1),
        )
        controller.begin_phase_one_traffic(
            {"weather-agent/v0", "finance-agent/v0"},
            now=START + timedelta(seconds=2),
        )
        controller.begin_phase_two_deployment(
            now=START + timedelta(seconds=3),
        )
        assert (
            controller.active.value["deployment"]["phase"]
            == "phase_2_deployment"
        )
        assert controller.active.value["deployment"]["traffic_started"] is True


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


def test_validation_runtime_root_rejects_every_override(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "AIQ_RUNTIME_ROOT",
        str(ROOT / ".aiq-runtime" / "agent-insights-quality"),
    )
    with pytest.raises(ContractError, match="does not permit"):
        validation_runtime_root()


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
        def resolve_intent(self, item):
            return replace(item, resolved_provider_id=item.provider_id)

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


def test_recovery_resolves_ambiguous_response_and_session_exactly(tmp_path) -> None:
    class Backend:
        def __init__(self) -> None:
            self.deleted = []

        def resolve_intent(self, item):
            return replace(
                item,
                resolved_provider_id=f"resolved-{item.kind}",
            )

        def delete(self, item) -> None:
            self.deleted.append(item.resolved_provider_id)

        def absent(self, item) -> bool:
            return item.resolved_provider_id in self.deleted

        def manifest_is_shared(self, _provider_id: str) -> bool:
            return False

        def inventory(self, **_kwargs) -> CleanupInventory:
            return CleanupInventory(False, (), (), (), ())

    lock = LocalValidationLock(tmp_path / "validation.lock")
    journal = LifecycleJournal(lock=lock, root=tmp_path / "lifecycle")
    backend = Backend()
    with lock:
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
        for index, (kind, runtime_kind) in enumerate(
            (("stored_response", "prompt"), ("session", "hosted_code")),
            start=3,
        ):
            intent_reference = content_hash({"kind": kind})
            event = {
                "state": "create_intent",
                "kind": kind,
                "intent_reference": intent_reference,
                "deterministic_name": f"synthetic-{kind}",
                "authority_id": "weather-agent/v0",
                "parent_id": "synthetic-project-id",
                "runtime_kind": runtime_kind,
                "discovery_key": f"synthetic-agent|{intent_reference}",
            }
            controller.dynamic_resource_event(
                event,
                now=START + timedelta(seconds=index),
            )
            controller.dynamic_resource_event(
                {**event, "state": "ambiguous_create"},
                now=START + timedelta(seconds=index, milliseconds=500),
            )

        state = ValidationReconciler(
            journal=journal,
            cleanup=CleanupEngine(backend),
            policy=load_validation_policy(),
        ).reconcile(alert=lambda _: None, now=START + timedelta(hours=80))
        assert state == "FAILED_CLEAN"
        assert backend.deleted == [
            "resolved-stored_response",
            "resolved-session",
            "resolved-project",
        ]
        recovered = journal.read_active()
        assert recovered.value["cleanup"]["exact_clean"] is True
        assert recovered.value["cleanup"]["failure"] is None
        assert all(
            item["state"] == "absence_verified"
            for item in recovered.value["resources"]
        )


def test_recovery_persists_public_safe_cleanup_failure_before_blocking(
    tmp_path,
) -> None:
    class Backend:
        def resolve_intent(self, _item):
            raise RemoteHttpError(
                400,
                "BadRequest",
                "Synthetic payload must not be persisted",
                "GET private-route",
            )

        def delete(self, _item) -> None:
            pytest.fail("unresolved resource must not be deleted")

        def absent(self, _item) -> bool:
            return False

        def manifest_is_shared(self, _provider_id: str) -> bool:
            return False

        def inventory(self, **_kwargs) -> CleanupInventory:
            return CleanupInventory(False, (), (), (), ())

    lock = LocalValidationLock(tmp_path / "validation.lock")
    journal = LifecycleJournal(lock=lock, root=tmp_path / "lifecycle")
    with lock:
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
        intent_reference = content_hash({"kind": "stored_response"})
        response_event = {
            "state": "create_intent",
            "kind": "stored_response",
            "intent_reference": intent_reference,
            "deterministic_name": "synthetic-response",
            "authority_id": "weather-agent/v0",
            "parent_id": "synthetic-project-id",
            "runtime_kind": "prompt",
            "discovery_key": f"synthetic-agent|{intent_reference}",
        }
        controller.dynamic_resource_event(
            response_event,
            now=START + timedelta(seconds=3),
        )
        controller.dynamic_resource_event(
            {**response_event, "state": "ambiguous_create"},
            now=START + timedelta(seconds=4),
        )
        state = ValidationReconciler(
            journal=journal,
            cleanup=CleanupEngine(Backend()),
            policy=load_validation_policy(),
        ).reconcile(alert=lambda _: None, now=START + timedelta(hours=80))
        assert state == "CLEANUP_BLOCKED"
        blocked = journal.read_active()
        assert blocked.value["cleanup"]["failure"] == {
            "operation": "resolve_intent",
            "resource_kind": "stored_response",
            "http_status": 400,
            "provider_code": "bad_request",
            "error_class": "RemoteHttpError",
        }
        serialized = str(blocked.value["cleanup"]["failure"])
        assert "private-route" not in serialized
        assert "Synthetic payload" not in serialized
