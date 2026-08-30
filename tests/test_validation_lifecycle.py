from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_blob import BlobRecord
from agent_insights_quality.validation_cleanup import (
    CleanupEngine,
    CleanupInventory,
    CleanupResult,
)
from agent_insights_quality.validation_cycle import ValidationCycleController
from agent_insights_quality.validation_lifecycle import (
    ACTIVE_BLOB,
    ACTIVE_CONTAINER,
    LifecycleJournal,
    stamp_lifecycle_digest,
    validate_lifecycle,
)
from agent_insights_quality.validation_policy import load_validation_policy
from agent_insights_quality.validation_reconciler import ValidationReconciler

HASH = "sha256:" + ("a" * 64)
HEAD = "b" * 40
START = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


class MemoryStore:
    def __init__(self, active: dict[str, Any] | None) -> None:
        self.values: dict[tuple[str, str], BlobRecord] = {}
        self.counter = 1
        self.lease_id = active["lease"]["lease_id"] if active else ""
        self.broken = False
        self.released = False
        if active is not None:
            self.values[(ACTIVE_CONTAINER, ACTIVE_BLOB)] = self._record(
                ACTIVE_CONTAINER,
                ACTIVE_BLOB,
                active,
            )

    def _record(
        self,
        container: str,
        name: str,
        value: dict[str, Any],
    ) -> BlobRecord:
        record = BlobRecord(
            container=container,
            name=name,
            value=deepcopy(value),
            etag=f"etag-{self.counter}",
            version_id=f"version-{self.counter}",
        )
        self.counter += 1
        return record

    def read(
        self,
        container: str,
        name: str,
        *,
        lease_id: str | None = None,
        version_id: str | None = None,
    ) -> BlobRecord:
        del version_id
        if lease_id is not None and lease_id != self.lease_id:
            raise ContractError("wrong synthetic lease")
        return deepcopy(self.values[(container, name)])

    def read_optional(
        self,
        container: str,
        name: str,
        *,
        lease_id: str | None = None,
        version_id: str | None = None,
    ) -> BlobRecord | None:
        if (container, name) not in self.values:
            return None
        return self.read(
            container,
            name,
            lease_id=lease_id,
            version_id=version_id,
        )

    def create_once(
        self,
        container: str,
        name: str,
        value: dict[str, Any],
    ) -> BlobRecord:
        key = (container, name)
        if key in self.values:
            existing = self.values[key]
            if existing.value != value:
                raise ContractError("different immutable synthetic content")
            return deepcopy(existing)
        record = self._record(container, name, value)
        self.values[key] = record
        return deepcopy(record)

    def compare_and_swap(
        self,
        container: str,
        name: str,
        value: dict[str, Any],
        *,
        lease_id: str,
        etag: str,
    ) -> BlobRecord:
        current = self.values[(container, name)]
        if lease_id != self.lease_id or current.etag != etag:
            raise ContractError("synthetic lease or ETag ownership was lost")
        record = self._record(container, name, value)
        self.values[(container, name)] = record
        return deepcopy(record)

    def acquire_infinite_lease(
        self,
        container: str,
        name: str,
        *,
        proposed_lease_id: str | None = None,
    ) -> str:
        del container, name
        self.lease_id = proposed_lease_id or f"fresh-lease-{self.counter}"
        return self.lease_id

    def break_lease(self, container: str, name: str) -> None:
        del container, name
        self.broken = True

    def release_lease(
        self,
        container: str,
        name: str,
        *,
        lease_id: str,
    ) -> None:
        del container, name
        assert lease_id == self.lease_id
        self.released = True


class CleanupBackend:
    def delete(self, item) -> None:
        del item

    def absent(self, item) -> bool:
        del item
        return True

    def manifest_is_shared(self, provider_id: str) -> bool:
        del provider_id
        return False

    def inventory(
        self,
        *,
        cycle_id: str,
        ownership_nonce: str,
    ) -> CleanupInventory:
        del cycle_id, ownership_nonce
        return CleanupInventory(
            project_exists=False,
            nonce_owned_ids=(),
            session_response_ids=(),
            cycle_acr_tag_ids=(),
            incomplete_cascade_ids=(),
        )


def _lifecycle(*, heartbeat: datetime = START, agents: int = 0) -> dict:
    canonical_agents = [
        "weather-agent",
        "healthcare-agent",
        "finance-agent",
        "travel-agent",
        "support-ticket-agent",
    ]
    topology_agents = []
    for index in range(agents):
        issue_number = index - 4
        canonical = (
            canonical_agents[index]
            if index < 5
            else "weather-agent"
            if issue_number <= 6
            else "healthcare-agent"
            if issue_number <= 12
            else "finance-agent"
            if issue_number <= 20
            else "travel-agent"
            if issue_number <= 28
            else "support-ticket-agent"
        )
        runtime_kind, framework = {
            "weather-agent": ("prompt", "foundry_prompt"),
            "healthcare-agent": ("prompt", "foundry_prompt"),
            "finance-agent": ("hosted_code", "microsoft_agent_framework"),
            "travel-agent": ("hosted_code", "langgraph"),
            "support-ticket-agent": (
                "hosted_custom_container",
                "custom_responses",
            ),
        }[canonical]
        hosted = runtime_kind != "prompt"
        topology_agents.append(
            {
                "authority_id": (
                    f"{canonical}/v0"
                    if index < 5
                    else f"issue-{issue_number:03d}"
                ),
                "canonical_agent": canonical,
                "logical_version": (
                    "v0" if index < 5 else f"issue-{issue_number:03d}"
                ),
                "runtime_kind": runtime_kind,
                "framework": framework,
                "runtime_agent_name": f"synthetic-agent-{index}",
                "runtime_agent_version": "1",
                "provider_agent_id": f"provider-agent-{index}",
                "provider_agent_version_id": f"provider-version-{index}",
                "hosted_identity_id": (
                    f"hosted-identity-{index}" if hosted else None
                ),
                "hosted_blueprint_id": (
                    f"hosted-blueprint-{index}" if hosted else None
                ),
                "hosted_deployment_id": (
                    f"hosted-deployment-{index}" if hosted else None
                ),
                "foundry_agent_name": f"synthetic-agent-{index}",
                "foundry_agent_version": "1",
                "runtime_principal_id": (
                    f"runtime-principal-{index}" if hosted else None
                ),
                "telemetry_identity_id": f"provider-version-{index}",
                "connection_ids": [],
            }
        )
    value = {
        "schema_version": "1.0.0",
        "kind": "test-agent-validation-lifecycle",
        "snapshot_type": "active",
        "cycle_id": "validation-cycle-0001",
        "epoch": 1,
        "revision": 1,
        "state": "LEASED",
        "repository": "ninghu/agent-insights-quality",
        "pr_number": 999,
        "git": {
            "initial_head_sha": HEAD,
            "initial_tree_sha": "c" * 40,
            "current_head_sha": HEAD,
            "current_tree_sha": "c" * 40,
            "frozen_head_sha": None,
            "frozen_tree_sha": None,
            "final_head_sha": None,
            "final_tree_sha": None,
        },
        "digests": {
            "artifact_manifest_hash": HASH,
            "source_tree_digest": HASH,
            "validation_contract_digest": HASH,
            "execution_matrix_digest": HASH,
            "runtime_topology_digest": None,
            "quota_plan_digest": HASH,
            "evidence_digest": None,
        },
        "policy_manifest": {
            "repository": "ninghu/agent-insights-quality",
            "path": "config/test-agent-validation-policy.yaml",
            "ref": "refs/heads/main",
            "commit_sha": HEAD,
            "content_digest": HASH,
        },
        "lease": {
            "epoch": 1,
            "lease_id": "synthetic-lease-1",
            "ownership_nonce": "nonce-0001",
            "holder_workflow_reference": "workflow-1",
            "holder_app_reference": "app-1",
            "holder_run_reference": "run-1",
            "acquired_at": START.isoformat(),
            "heartbeat_at": heartbeat.isoformat(),
            "state": "held",
        },
        "capacity": {
            "measured_rpm": 100,
            "measured_tpm": 100000,
            "measured_at": START.isoformat(),
            "reserved_percent": 25,
            "reserved_rpm": 25,
            "reserved_tpm": 25000,
            "available_rpm": 75,
            "available_tpm": 75000,
            "outer_request_envelope": 500,
            "worst_case_inner_model_calls": 2,
            "worst_case_inner_tokens": 4096,
            "endpoint_concurrency": 8,
            "provisioning_concurrency": 8,
            "telemetry_query_concurrency": 4,
            "runtime_attempt_concurrency": 1,
            "inner_model_call_limit": 4,
            "plan_digest": HASH,
        },
        "project": {
            "name": None,
            "provider_id": None,
            "endpoint_reference": None,
            "state": "absent",
            "create_intent_at": None,
            "create_observed_at": None,
            "delete_intent_at": None,
            "delete_observed_at": None,
        },
        "runtime_topology": {
            "account_reference": "account-reference",
            "project_reference": None,
            "telemetry_resource_set": "g29",
            "test_agent_model": {
                "deployment_name": "gpt-5.4-mini",
                "model_id": "gpt-5.4-mini",
                "model_version": "2026-03-17",
            },
            "name_policy": {
                "project_maximum_length": 64,
                "agent_maximum_length": 63,
                "pattern": "^[a-z][a-z0-9-]*[a-z0-9]$",
            },
            "connection_ids": [],
            "runtime_principal_ids": [],
            "telemetry_identity_ids": [],
            "agents": topology_agents,
        },
        "resources": [],
        "scope_freeze": None,
        "review": None,
        "cleanup": {
            "status": "not_started",
            "plan_hash": None,
            "exact_clean": False,
            "verified_absent_ids": [],
            "retained_shared_manifest_ids": [],
            "residue_ids": [],
            "verification_at": None,
        },
        "event_snapshot": {
            "path": "test-agent-validation-snapshots/events/initial.json",
            "version_id": "version-initial",
            "etag": "etag-initial",
            "digest": HASH,
        },
        "clean_snapshot": None,
        "evidence_reference": None,
        "receipt_reference": None,
        "last_activity_at": START.isoformat(),
        "absolute_expires_at": (START + timedelta(hours=72)).isoformat(),
        "failure": None,
        "previous_etag": None,
        "journal_digest": HASH,
    }
    if agents:
        resources = [
            {
                "kind": "project",
                "parent_id": None,
                "authority_id": None,
                "deterministic_name": "synthetic-project",
                "provider_id": "project-id",
                "ownership_nonce": "nonce-0001",
                "create_intent_at": START.isoformat(),
                "create_observed_at": START.isoformat(),
                "delete_intent_at": None,
                "delete_observed_at": None,
                "state": "created",
                "cleanup_method": "explicit",
            }
        ]

        def add_resource(
            kind: str,
            provider_id: str | None,
            authority_id: str,
        ) -> None:
            if provider_id is None or any(
                item["provider_id"] == provider_id for item in resources
            ):
                return
            resources.append(
                {
                    "kind": kind,
                    "parent_id": "project-id",
                    "authority_id": authority_id,
                    "deterministic_name": provider_id,
                    "provider_id": provider_id,
                    "ownership_nonce": "nonce-0001",
                    "create_intent_at": START.isoformat(),
                    "create_observed_at": START.isoformat(),
                    "delete_intent_at": None,
                    "delete_observed_at": None,
                    "state": "created",
                    "cleanup_method": (
                        "documented_project_cascade"
                        if kind == "runtime_principal"
                        else "explicit"
                    ),
                }
            )

        for agent in topology_agents:
            for kind, field in (
                ("provider_agent", "provider_agent_id"),
                ("provider_agent_version", "provider_agent_version_id"),
                ("hosted_identity", "hosted_identity_id"),
                ("hosted_blueprint", "hosted_blueprint_id"),
                ("hosted_deployment", "hosted_deployment_id"),
                ("runtime_principal", "runtime_principal_id"),
            ):
                add_resource(kind, agent[field], agent["authority_id"])
        value["project"].update(
            {
                "name": "synthetic-project",
                "provider_id": "project-id",
                "state": "created",
                "create_intent_at": START.isoformat(),
                "create_observed_at": START.isoformat(),
            }
        )
        value["runtime_topology"].update(
            {
                "project_reference": "project-id",
                "runtime_principal_ids": sorted(
                    {
                        item["runtime_principal_id"]
                        for item in topology_agents
                        if item["runtime_principal_id"]
                    }
                ),
                "telemetry_identity_ids": sorted(
                    {item["telemetry_identity_id"] for item in topology_agents}
                ),
            }
        )
        value["resources"] = resources
    return stamp_lifecycle_digest(value)


def test_journal_writes_immutable_event_before_etag_guarded_active_update() -> None:
    value = _lifecycle()
    store = MemoryStore(value)
    journal = LifecycleJournal(store, load_validation_policy())
    current = store.read(ACTIVE_CONTAINER, ACTIVE_BLOB)
    committed = journal.commit(
        current,
        lease_id="synthetic-lease-1",
        next_state="PREFLIGHT",
        now=START + timedelta(seconds=30),
    )
    assert committed.event.value["snapshot_type"] == "event"
    assert committed.active.value["snapshot_type"] == "active"
    assert committed.active.value["state"] == "PREFLIGHT"
    assert committed.active.value["previous_etag"] == current.etag
    assert (
        committed.active.value["event_snapshot"]["digest"]
        == committed.event.value["journal_digest"]
    )


def test_first_cycle_creates_anchor_then_acquires_proposed_infinite_lease() -> None:
    initial = _lifecycle()
    initial["snapshot_type"] = "event"
    initial["event_snapshot"] = None
    initial["lease"]["lease_id"] = "proposed-lease"
    initial = stamp_lifecycle_digest(initial)
    store = MemoryStore(None)
    lease_id, active = LifecycleJournal(
        store,
        load_validation_policy(),
    ).begin_cycle(
        initial,
        proposed_lease_id="proposed-lease",
    )
    assert lease_id == "proposed-lease"
    assert active.value["snapshot_type"] == "active"
    assert active.value["event_snapshot"]["digest"].startswith("sha256:")


def test_journal_rejects_lost_etag_and_project_replacement() -> None:
    value = _lifecycle()
    store = MemoryStore(value)
    journal = LifecycleJournal(store, load_validation_policy())
    current = store.read(ACTIVE_CONTAINER, ACTIVE_BLOB)
    stale = BlobRecord(
        container=current.container,
        name=current.name,
        value=current.value,
        etag="stale-etag",
        version_id=current.version_id,
    )
    with pytest.raises(ContractError, match="ETag ownership"):
        journal.commit(
            stale,
            lease_id="synthetic-lease-1",
            next_state="PREFLIGHT",
            now=START + timedelta(seconds=1),
        )

    current.value["project"]["name"] = "original-project"
    current.value["project"]["provider_id"] = "project-id-1"
    current = BlobRecord(
        container=current.container,
        name=current.name,
        value=stamp_lifecycle_digest(current.value),
        etag=current.etag,
        version_id=current.version_id,
    )
    store.values[(ACTIVE_CONTAINER, ACTIVE_BLOB)] = current
    with pytest.raises(ContractError, match="cannot be replaced"):
        journal.commit(
            current,
            lease_id="synthetic-lease-1",
            next_state="PREFLIGHT",
            updates={
                "project": {
                    "name": "replacement-project",
                    "provider_id": "project-id-2",
                }
            },
            now=START + timedelta(seconds=1),
        )


def test_reconciler_takeover_requires_stale_lease_and_fresh_epoch() -> None:
    fresh_store = MemoryStore(_lifecycle())
    fresh = LifecycleJournal(fresh_store, load_validation_policy())
    with pytest.raises(ContractError, match="not stale"):
        fresh.takeover_for_cleanup(
            ownership_nonce="nonce-0002",
            holder_workflow_reference="workflow-2",
            holder_app_reference="app-2",
            holder_run_reference="run-2",
            now=START + timedelta(seconds=30),
        )

    stale_store = MemoryStore(_lifecycle(heartbeat=START))
    stale = LifecycleJournal(stale_store, load_validation_policy())
    lease_id, takeover = stale.takeover_for_cleanup(
        ownership_nonce="nonce-0002",
        holder_workflow_reference="workflow-2",
        holder_app_reference="app-2",
        holder_run_reference="run-2",
        now=START + timedelta(seconds=61),
    )
    assert stale_store.broken is True
    assert lease_id.startswith("fresh-lease-")
    assert takeover.active.value["epoch"] == 2
    assert takeover.active.value["lease"]["ownership_nonce"] == "nonce-0002"
    assert takeover.active.value["state"] == "CLEANING"


def test_reconciler_can_take_over_after_absolute_ttl() -> None:
    value = _lifecycle(heartbeat=START)
    value["state"] = "PREFLIGHT"
    value = stamp_lifecycle_digest(value)
    store = MemoryStore(value)
    lease_id, takeover = LifecycleJournal(
        store,
        load_validation_policy(),
    ).takeover_for_cleanup(
        ownership_nonce="nonce-0002",
        holder_workflow_reference="workflow-2",
        holder_app_reference="app-2",
        holder_run_reference="run-2",
        now=START + timedelta(hours=73),
    )
    assert lease_id.startswith("fresh-lease-")
    assert takeover.active.value["state"] == "CLEANING"
    assert takeover.active.value["last_activity_at"] > value["absolute_expires_at"]


def test_event_snapshot_retry_uses_content_addressed_name() -> None:
    class FailOnceStore(MemoryStore):
        fail = True

        def compare_and_swap(self, *args, **kwargs):
            if self.fail:
                self.fail = False
                raise ContractError("synthetic CAS interruption")
            return super().compare_and_swap(*args, **kwargs)

    store = FailOnceStore(_lifecycle())
    journal = LifecycleJournal(store, load_validation_policy())
    current = store.read(ACTIVE_CONTAINER, ACTIVE_BLOB)
    with pytest.raises(ContractError, match="CAS interruption"):
        journal.commit(
            current,
            lease_id="synthetic-lease-1",
            next_state="PREFLIGHT",
            now=START + timedelta(seconds=1),
        )
    committed = journal.commit(
        current,
        lease_id="synthetic-lease-1",
        next_state="PREFLIGHT",
        now=START + timedelta(seconds=2),
    )
    event_names = [
        name
        for container, name in store.values
        if container == "test-agent-validation-snapshots"
    ]
    assert len(event_names) == 2
    assert len(set(event_names)) == 2
    assert committed.active.value["state"] == "PREFLIGHT"


def test_normal_mutation_cannot_extend_ttl_or_continue_after_stale_heartbeat() -> None:
    value = _lifecycle()
    store = MemoryStore(value)
    journal = LifecycleJournal(store, load_validation_policy())
    current = store.read(ACTIVE_CONTAINER, ACTIVE_BLOB)
    with pytest.raises(ContractError, match="heartbeat is stale"):
        journal.commit(
            current,
            lease_id="synthetic-lease-1",
            next_state="PREFLIGHT",
            now=START + timedelta(seconds=61),
        )
    with pytest.raises(ContractError, match="absolute TTL is immutable"):
        journal.commit(
            current,
            lease_id="synthetic-lease-1",
            next_state="PREFLIGHT",
            updates={
                "absolute_expires_at": (
                    START + timedelta(hours=73)
                ).isoformat()
            },
            now=START + timedelta(seconds=1),
        )


def test_terminal_clean_snapshot_requires_exact_exhaustive_cleanup() -> None:
    value = _lifecycle(agents=41)
    value["state"] = "CLEANING"
    value["evidence_reference"] = {
        "path": "test-agent-validation-snapshots/evidence/evidence.json",
        "version_id": "evidence-version",
        "etag": "evidence-etag",
        "digest": HASH,
    }
    value = stamp_lifecycle_digest(value)
    store = MemoryStore(value)
    journal = LifecycleJournal(store, load_validation_policy())
    current = store.read(ACTIVE_CONTAINER, ACTIVE_BLOB)
    absent_ids = [item["provider_id"] for item in value["resources"]]
    cleaned_resources = deepcopy(value["resources"])
    for resource in cleaned_resources:
        resource["state"] = "absence_verified"
        resource["delete_observed_at"] = (
            START + timedelta(minutes=1)
        ).isoformat()
    committed = journal.commit(
        current,
        lease_id="synthetic-lease-1",
        next_state="CLEAN",
        updates={
            "cleanup": {
                "status": "exact_clean",
                "plan_hash": HASH,
                "exact_clean": True,
                "verified_absent_ids": absent_ids,
                "retained_shared_manifest_ids": [],
                "residue_ids": [],
                "verification_at": (START + timedelta(minutes=1)).isoformat(),
            },
            "resources": cleaned_resources,
        },
        now=START + timedelta(minutes=1),
    )
    assert committed.clean is not None
    assert committed.clean.value["snapshot_type"] == "clean"
    assert committed.active.value["clean_snapshot"]["digest"] == (
        committed.clean.value["journal_digest"]
    )
    validate_lifecycle(committed.active.value)


def test_terminal_clean_rejects_unjournaled_topology_identity() -> None:
    value = _lifecycle(agents=41)
    value["state"] = "CLEAN"
    value["evidence_reference"] = {
        "path": "test-agent-validation-snapshots/evidence/evidence.json",
        "version_id": "evidence-version",
        "etag": "evidence-etag",
        "digest": HASH,
    }
    value["resources"] = value["resources"][:-1]
    for resource in value["resources"]:
        resource["state"] = "absence_verified"
    value["cleanup"].update(
        {
            "status": "exact_clean",
            "plan_hash": HASH,
            "exact_clean": True,
            "verified_absent_ids": [
                item["provider_id"] for item in value["resources"]
            ],
            "verification_at": START.isoformat(),
        }
    )
    value["clean_snapshot"] = {
        "path": "test-agent-validation-snapshots/clean/clean.json",
        "version_id": "clean-version",
        "etag": "clean-etag",
        "digest": HASH,
    }
    with pytest.raises(ContractError, match="unjournaled provider identities"):
        validate_lifecycle(stamp_lifecycle_digest(value))


def test_cleanup_blocked_keeps_account_unavailable() -> None:
    value = _lifecycle(agents=41)
    value["state"] = "CLEANING"
    value = stamp_lifecycle_digest(value)
    store = MemoryStore(value)
    journal = LifecycleJournal(store, load_validation_policy())
    committed = journal.commit(
        store.read(ACTIVE_CONTAINER, ACTIVE_BLOB),
        lease_id="synthetic-lease-1",
        next_state="CLEANUP_BLOCKED",
        updates={
            "cleanup": {
                "status": "ambiguous",
                "exact_clean": False,
                "residue_ids": ["synthetic-residue"],
            }
        },
        now=START + timedelta(minutes=1),
    )
    assert committed.active.value["state"] == "CLEANUP_BLOCKED"
    assert committed.clean is None


def test_receipt_transition_releases_lease_only_after_clean_proof() -> None:
    value = _lifecycle(agents=41)
    value["state"] = "CLEAN"
    value["git"]["final_head_sha"] = value["git"]["current_head_sha"]
    value["git"]["final_tree_sha"] = value["git"]["current_tree_sha"]
    value["evidence_reference"] = {
        "path": "test-agent-validation-snapshots/evidence/evidence.json",
        "version_id": "evidence-version",
        "etag": "evidence-etag",
        "digest": HASH,
    }
    value["cleanup"] = {
        "status": "exact_clean",
        "plan_hash": HASH,
        "exact_clean": True,
        "verified_absent_ids": [
            item["provider_id"] for item in value["resources"]
        ],
        "retained_shared_manifest_ids": [],
        "residue_ids": [],
        "verification_at": START.isoformat(),
    }
    for resource in value["resources"]:
        resource["state"] = "absence_verified"
        resource["delete_observed_at"] = START.isoformat()
    value["clean_snapshot"] = {
        "path": "test-agent-validation-snapshots/clean/clean.json",
        "version_id": "clean-version",
        "etag": "clean-etag",
        "digest": HASH,
    }
    value = stamp_lifecycle_digest(value)
    store = MemoryStore(value)
    current = store.read(ACTIVE_CONTAINER, ACTIVE_BLOB)
    controller = ValidationCycleController(
        LifecycleJournal(store, load_validation_policy()),
        lease_id="synthetic-lease-1",
        active=current,
    )
    receipt = BlobRecord(
        "test-agent-validation-shadow-receipts",
        "shadow-receipts/receipt.json",
        {
            "receipt_digest": HASH,
            "cycle_id": value["cycle_id"],
            "epoch": value["epoch"],
        },
        "receipt-etag",
        "receipt-version",
    )
    active = controller.receipt_issued(
        receipt,
        now=START + timedelta(seconds=1),
    )
    assert active.value["state"] == "RECEIPT_ISSUED"
    assert active.value["lease"]["state"] == "released"
    assert active.value["receipt_reference"]["digest"] == HASH
    assert store.released is True
    retried = ValidationCycleController(
        LifecycleJournal(store, load_validation_policy()),
        lease_id="synthetic-lease-1",
        active=store.read(ACTIVE_CONTAINER, ACTIVE_BLOB),
    ).receipt_issued(
        receipt,
        now=START + timedelta(seconds=2),
    )
    assert retried.value["state"] == "RECEIPT_ISSUED"
    assert retried.value["lease"]["state"] == "released"


def test_failed_clean_releases_lease_and_reconciler_retry_is_idempotent() -> None:
    value = _lifecycle()
    value["state"] = "CLEANING"
    value = stamp_lifecycle_digest(value)
    store = MemoryStore(value)
    journal = LifecycleJournal(store, load_validation_policy())
    controller = ValidationCycleController(
        journal,
        lease_id="synthetic-lease-1",
        active=store.read(ACTIVE_CONTAINER, ACTIVE_BLOB),
    )
    controller.complete_cleanup(
        CleanupResult(
            plan_hash=HASH,
            exact_clean=True,
            verified_absent_ids=(),
            retained_shared_manifest_ids=(),
            residue_ids=(),
        ),
        failed_cycle=True,
        now=START + timedelta(seconds=1),
    )
    assert controller.active.value["state"] == "FAILED_CLEAN"
    assert controller.active.value["lease"]["state"] == "released"
    assert store.released is True
    state = ValidationReconciler(
        journal=journal,
        cleanup=CleanupEngine(CleanupBackend()),
        policy=load_validation_policy(),
    ).reconcile(
        ownership_nonce="nonce-0002",
        holder_workflow_reference="workflow-2",
        holder_app_reference="app-2",
        holder_run_reference="run-2",
        alert=lambda message: None,
        now=START + timedelta(seconds=2),
    )
    assert state == "FAILED_CLEAN"


def test_reconciler_preserves_successful_final_checks_as_clean() -> None:
    value = _lifecycle(heartbeat=START, agents=41)
    value["state"] = "FINAL_CHECKS"
    value["git"]["final_head_sha"] = value["git"]["current_head_sha"]
    value["git"]["final_tree_sha"] = value["git"]["current_tree_sha"]
    value["evidence_reference"] = {
        "path": "test-agent-validation-snapshots/evidence/evidence.json",
        "version_id": "evidence-version",
        "etag": "evidence-etag",
        "digest": HASH,
    }
    value = stamp_lifecycle_digest(value)
    store = MemoryStore(value)
    state = ValidationReconciler(
        journal=LifecycleJournal(store, load_validation_policy()),
        cleanup=CleanupEngine(CleanupBackend()),
        policy=load_validation_policy(),
    ).reconcile(
        ownership_nonce="nonce-0002",
        holder_workflow_reference="workflow-2",
        holder_app_reference="app-2",
        holder_run_reference="run-2",
        alert=lambda message: None,
        now=START + timedelta(seconds=61),
    )
    assert state == "CLEAN"
    active = store.read(ACTIVE_CONTAINER, ACTIVE_BLOB)
    assert active.value["lease"]["state"] == "held"
    assert active.value["cleanup"]["exact_clean"] is True


def test_expired_unreceipted_clean_is_failed_clean_and_released() -> None:
    value = _lifecycle(agents=41)
    value["state"] = "CLEAN"
    value["evidence_reference"] = {
        "path": "test-agent-validation-snapshots/evidence/evidence.json",
        "version_id": "evidence-version",
        "etag": "evidence-etag",
        "digest": HASH,
    }
    for resource in value["resources"]:
        resource["state"] = "absence_verified"
        resource["delete_observed_at"] = START.isoformat()
    value["cleanup"].update(
        {
            "status": "exact_clean",
            "plan_hash": HASH,
            "exact_clean": True,
            "verified_absent_ids": [
                item["provider_id"] for item in value["resources"]
            ],
            "verification_at": START.isoformat(),
        }
    )
    value["clean_snapshot"] = {
        "path": "test-agent-validation-snapshots/clean/clean.json",
        "version_id": "clean-version",
        "etag": "clean-etag",
        "digest": HASH,
    }
    value = stamp_lifecycle_digest(value)
    store = MemoryStore(value)
    state = ValidationReconciler(
        journal=LifecycleJournal(store, load_validation_policy()),
        cleanup=CleanupEngine(CleanupBackend()),
        policy=load_validation_policy(),
    ).reconcile(
        ownership_nonce="nonce-0002",
        holder_workflow_reference="workflow-2",
        holder_app_reference="app-2",
        holder_run_reference="run-2",
        alert=lambda message: None,
        now=START + timedelta(hours=73),
    )
    assert state == "FAILED_CLEAN"
    assert store.released is True
