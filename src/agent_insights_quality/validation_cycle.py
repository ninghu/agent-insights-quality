from __future__ import annotations

import copy
import threading
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_insights_quality.util import ContractError, content_hash
from agent_insights_quality.validation_blob import BlobRecord
from agent_insights_quality.validation_evidence import validate_evidence
from agent_insights_quality.validation_lifecycle import (
    LifecycleJournal,
    stamp_lifecycle_digest,
    validate_topology_resource_bindings,
)
from agent_insights_quality.validation_policy import ValidationPolicy
from agent_insights_quality.validation_quota import CapacityPlan
from agent_insights_quality.validation_cleanup import CleanupResult


def initial_lifecycle(
    candidate: Mapping[str, Any],
    *,
    policy: ValidationPolicy,
    policy_manifest: Mapping[str, Any],
    policy_manifest_digest: str,
    policy_commit_sha: str,
    policy_ref: str,
    lease_id: str,
    ownership_nonce: str,
    holder_workflow_reference: str,
    holder_app_reference: str,
    holder_run_reference: str,
    account_reference: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    if candidate.get("kind") != "test-agent-validation-candidate":
        raise ContractError("Validation candidate manifest kind is invalid")
    if candidate.get("telemetry_resource_set") != "g29":
        raise ContractError("Validation candidate must use g29")
    if policy_manifest.get("repository") != policy.repository:
        raise ContractError("Validation policy manifest repository is invalid")
    value = {
        "schema_version": "1.0.0",
        "kind": "test-agent-validation-lifecycle",
        "snapshot_type": "event",
        "cycle_id": candidate["cycle_id"],
        "epoch": 1,
        "revision": 1,
        "state": "LEASED",
        "repository": candidate["repository"],
        "pr_number": candidate["pr_number"],
        "git": {
            "initial_head_sha": candidate["candidate_head_sha"],
            "initial_tree_sha": candidate["candidate_tree_sha"],
            "current_head_sha": candidate["candidate_head_sha"],
            "current_tree_sha": candidate["candidate_tree_sha"],
            "frozen_head_sha": None,
            "frozen_tree_sha": None,
            "final_head_sha": None,
            "final_tree_sha": None,
        },
        "digests": {
            "artifact_manifest_hash": candidate["artifact_manifest_hash"],
            "source_tree_digest": candidate["source_tree_digest"],
            "validation_contract_digest": candidate[
                "validation_contract_digest"
            ],
            "execution_matrix_digest": candidate["execution_matrix_digest"],
            "runtime_topology_digest": candidate["runtime_topology_digest"],
            "quota_plan_digest": None,
            "evidence_digest": None,
        },
        "policy_manifest": {
            "repository": policy.repository,
            "path": policy.policy_manifest_path,
            "ref": policy_ref,
            "commit_sha": policy_commit_sha,
            "content_digest": policy_manifest_digest,
        },
        "lease": {
            "epoch": 1,
            "lease_id": lease_id,
            "ownership_nonce": ownership_nonce,
            "holder_workflow_reference": holder_workflow_reference,
            "holder_app_reference": holder_app_reference,
            "holder_run_reference": holder_run_reference,
            "acquired_at": moment.isoformat(),
            "heartbeat_at": moment.isoformat(),
            "state": "held",
        },
        "capacity": None,
        "project": {
            "name": candidate["project_name"],
            "provider_id": None,
            "endpoint_reference": None,
            "state": "absent",
            "create_intent_at": None,
            "create_observed_at": None,
            "delete_intent_at": None,
            "delete_observed_at": None,
        },
        "runtime_topology": {
            "account_reference": account_reference,
            "project_reference": None,
            "telemetry_resource_set": "g29",
            "test_agent_model": policy.test_agent_model,
            "name_policy": {
                "project_maximum_length": policy.project_name_policy.maximum_length,
                "agent_maximum_length": policy.agent_name_policy.maximum_length,
                "pattern": policy.agent_name_policy.pattern,
            },
            "connection_ids": [],
            "runtime_principal_ids": [],
            "telemetry_identity_ids": [],
            "agents": [],
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
        "event_snapshot": None,
        "clean_snapshot": None,
        "evidence_reference": None,
        "receipt_reference": None,
        "last_activity_at": moment.isoformat(),
        "absolute_expires_at": (
            moment + timedelta(hours=policy.limits.absolute_ttl_hours)
        ).isoformat(),
        "failure": None,
        "previous_etag": None,
        "journal_digest": "",
    }
    return stamp_lifecycle_digest(value)


class ValidationCycleController:
    def __init__(
        self,
        journal: LifecycleJournal,
        *,
        lease_id: str,
        active: BlobRecord,
    ) -> None:
        self._journal = journal
        self._lease_id = lease_id
        self._active = active
        self._lock = threading.RLock()

    @property
    def active(self) -> BlobRecord:
        return self._active

    def heartbeat(self, *, now: datetime) -> BlobRecord:
        return self._commit(self._active.value["state"], {}, now)

    def preflight(self, plan: CapacityPlan, *, now: datetime) -> BlobRecord:
        payload = asdict(plan)
        payload["plan_digest"] = plan.plan_digest
        return self._commit(
            "PREFLIGHT",
            {
                "capacity": payload,
                "digests": {"quota_plan_digest": plan.plan_digest},
            },
            now,
        )

    def project_create_intent(
        self,
        *,
        name: str,
        provider_id: str,
        now: datetime,
    ) -> BlobRecord:
        project = copy.deepcopy(self._active.value["project"])
        project.update(
            {
                "name": name,
                "provider_id": provider_id,
                "state": "create_intent",
                "create_intent_at": now.astimezone(UTC).isoformat(),
            }
        )
        resources = [
            *self._active.value["resources"],
            _resource(
                kind="project",
                parent_id=None,
                authority_id=None,
                deterministic_name=name,
                provider_id=provider_id,
                ownership_nonce=self._active.value["lease"]["ownership_nonce"],
                state="create_intent",
                cleanup_method="explicit",
                now=now,
            ),
        ]
        return self._commit(
            "CREATING",
            {"project": project, "resources": resources},
            now,
        )

    def project_created(
        self,
        *,
        endpoint_reference: str,
        project_principal_id: str,
        connection_ids: list[str],
        role_assignment_ids: list[str],
        resource_observations: Mapping[str, str],
        now: datetime,
    ) -> BlobRecord:
        project = copy.deepcopy(self._active.value["project"])
        project.update(
            {
                "endpoint_reference": endpoint_reference,
                "state": "created",
                "create_observed_at": now.astimezone(UTC).isoformat(),
            }
        )
        resources = _mark_created(
            self._active.value["resources"],
            project["provider_id"],
            now,
        )
        resources = _observe_resources(resources, resource_observations, now)
        observed_ids = set(resource_observations.values())
        if (
            project_principal_id not in observed_ids
            or not set(connection_ids).issubset(observed_ids)
            or not set(role_assignment_ids).issubset(observed_ids)
        ):
            raise ContractError("Validation Project observations are incomplete")
        return self._commit(
            "CREATING",
            {
                "project": project,
                "resources": resources,
                "runtime_topology": {
                    "project_reference": project["provider_id"],
                    "connection_ids": connection_ids,
                    "runtime_principal_ids": [project_principal_id],
                },
            },
            now,
        )

    def mark_resources_ambiguous(
        self,
        provider_ids: list[str],
        *,
        now: datetime,
    ) -> BlobRecord:
        with self._lock:
            expected = set(provider_ids)
            resources = []
            found: set[str] = set()
            for resource in self._active.value["resources"]:
                item = copy.deepcopy(resource)
                if item["provider_id"] in expected:
                    item["state"] = "ambiguous_create"
                    found.add(item["provider_id"])
                resources.append(item)
            if found != expected:
                raise ContractError(
                    "Ambiguous validation create has no complete intent set"
                )
            return self._commit(
                self._active.value["state"],
                {"resources": resources},
                now,
            )

    def resource_create_intent(
        self,
        *,
        kind: str,
        parent_id: str | None,
        authority_id: str | None,
        deterministic_name: str,
        provider_id: str,
        cleanup_method: str,
        now: datetime,
    ) -> BlobRecord:
        with self._lock:
            if any(
                item["provider_id"] == provider_id
                for item in self._active.value["resources"]
            ):
                raise ContractError("Validation resource create intent is duplicated")
            resources = [
                *self._active.value["resources"],
                _resource(
                    kind=kind,
                    parent_id=parent_id,
                    authority_id=authority_id,
                    deterministic_name=deterministic_name,
                    provider_id=provider_id,
                    ownership_nonce=self._active.value["lease"][
                        "ownership_nonce"
                    ],
                    state="create_intent",
                    cleanup_method=cleanup_method,
                    now=now,
                ),
            ]
            return self._commit(
                self._active.value["state"],
                {"resources": resources},
                now,
            )

    def resource_created(self, provider_id: str, *, now: datetime) -> BlobRecord:
        with self._lock:
            resources = _mark_created(
                self._active.value["resources"],
                provider_id,
                now,
            )
            return self._commit(
                self._active.value["state"],
                {"resources": resources},
                now,
            )

    def dynamic_resource_event(
        self,
        event: Mapping[str, Any],
        *,
        now: datetime,
    ) -> BlobRecord:
        state = event.get("state")
        intent_reference = str(event.get("intent_reference") or "")
        if not intent_reference:
            raise ContractError("Dynamic validation resource intent is missing")
        if state == "create_intent":
            return self.resource_create_intent(
                kind=str(event["kind"]),
                parent_id=event.get("parent_id"),
                authority_id=event.get("authority_id"),
                deterministic_name=str(event["deterministic_name"]),
                provider_id=intent_reference,
                cleanup_method=str(event.get("cleanup_method") or "explicit"),
                now=now,
            )
        if state == "ambiguous_create":
            with self._lock:
                resources = []
                found = False
                for resource in self._active.value["resources"]:
                    item = copy.deepcopy(resource)
                    if item["provider_id"] == intent_reference:
                        item["state"] = "ambiguous_create"
                        found = True
                    resources.append(item)
                if not found:
                    raise ContractError(
                        "Ambiguous validation create has no recorded intent"
                    )
                return self._commit(
                    self._active.value["state"],
                    {"resources": resources},
                    now,
                )
        if state != "created":
            raise ContractError("Dynamic validation resource event state is invalid")
        provider_id = str(event.get("provider_id") or "")
        if not provider_id:
            raise ContractError("Dynamic validation resource provider ID is missing")
        with self._lock:
            found = False
            resources = []
            existing_ids = {
                item["provider_id"]
                for item in self._active.value["resources"]
                if item["provider_id"] != intent_reference
            }
            for resource in self._active.value["resources"]:
                item = copy.deepcopy(resource)
                if item["provider_id"] == intent_reference:
                    if (
                        provider_id in existing_ids
                        and item["kind"] != "runtime_principal"
                    ):
                        raise ContractError(
                            "Observed validation resource provider ID collides"
                        )
                    if provider_id not in existing_ids:
                        item["provider_id"] = provider_id
                    item["deterministic_name"] = str(
                        event["deterministic_name"]
                    )
                    item["state"] = "created"
                    item["create_observed_at"] = now.astimezone(UTC).isoformat()
                    found = True
                resources.append(item)
            if not found:
                raise ContractError(
                    "Dynamic validation resource observation has no intent"
                )
            return self._commit(
                self._active.value["state"],
                {"resources": resources},
                now,
            )

    def begin_validation(
        self,
        runtime_agents: list[dict[str, Any]],
        *,
        now: datetime,
    ) -> BlobRecord:
        if len(runtime_agents) != 41:
            raise ContractError("Validation requires 41 deployed Agent endpoints")
        topology = {
            "agents": runtime_agents,
            "runtime_principal_ids": sorted(
                {
                    item["runtime_principal_id"]
                    for item in runtime_agents
                    if item["runtime_principal_id"] is not None
                }
            ),
            "telemetry_identity_ids": sorted(
                {item["telemetry_identity_id"] for item in runtime_agents}
            ),
        }
        validate_topology_resource_bindings(
            {
                **self._active.value["runtime_topology"],
                **topology,
            },
            self._active.value["resources"],
        )
        digest = content_hash(runtime_agents)
        return self._commit(
            "VALIDATING",
            {
                "runtime_topology": topology,
                "digests": {"runtime_topology_digest": digest},
            },
            now,
        )

    def freeze(
        self,
        *,
        evidence: BlobRecord,
        head_sha: str,
        tree_sha: str,
        now: datetime,
    ) -> BlobRecord:
        if head_sha != self._active.value["git"]["current_head_sha"]:
            raise ContractError("Scope freeze head differs from the validated head")
        validate_evidence(evidence.value)
        if evidence.value["candidate_head_sha"] != head_sha:
            raise ContractError("Scope freeze evidence belongs to another head")
        evidence_digest = evidence.value.get("evidence_digest")
        if not isinstance(evidence_digest, str) or not evidence_digest.startswith(
            "sha256:"
        ):
            raise ContractError("Validation evidence Blob digest is invalid")
        scope = {
            "head_sha": head_sha,
            "tree_sha": tree_sha,
            "frozen_at": now.astimezone(UTC).isoformat(),
            "source_tree_digest": self._active.value["digests"][
                "source_tree_digest"
            ],
            "validation_contract_digest": self._active.value["digests"][
                "validation_contract_digest"
            ],
            "changed_authority_ids": [],
            "comprehensive_review_required": True,
        }
        return self._commit(
            "FROZEN",
            {
                "git": {
                    "frozen_head_sha": head_sha,
                    "frozen_tree_sha": tree_sha,
                },
                "digests": {"evidence_digest": evidence_digest},
                "evidence_reference": {
                    "path": f"{evidence.container}/{evidence.name}",
                    "version_id": evidence.version_id,
                    "etag": evidence.etag,
                    "digest": evidence_digest,
                },
                "scope_freeze": scope,
            },
            now,
        )

    def receipt_issued(
        self,
        receipt: BlobRecord,
        *,
        now: datetime,
    ) -> BlobRecord:
        digest = receipt.value.get("receipt_digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise ContractError("Validation receipt Blob digest is invalid")
        if (
            receipt.value.get("cycle_id") != self._active.value["cycle_id"]
            or receipt.value.get("epoch") != self._active.value["epoch"]
        ):
            raise ContractError("Validation receipt belongs to another lifecycle")
        self._active = self._journal.complete_receipt_handoff(
            self._active,
            receipt,
            lease_id=self._lease_id,
            now=now,
        )
        return self._active

    def record_review(
        self,
        *,
        mode: str,
        check_reference: str | None,
        findings_digest: str | None,
        now: datetime,
    ) -> BlobRecord:
        if self._active.value["state"] != "FROZEN":
            raise ContractError("Validation permits exactly one frozen-scope review")
        if mode not in {"comprehensive", "shadow_skipped"}:
            raise ContractError("Validation review mode is invalid")
        if mode == "comprehensive" and (
            not check_reference or not findings_digest
        ):
            raise ContractError("Comprehensive validation review proof is incomplete")
        if mode == "shadow_skipped" and (
            check_reference is not None or findings_digest is not None
        ):
            raise ContractError("Skipped shadow review cannot claim review proof")
        state = "REVIEWED" if mode == "comprehensive" else "SHADOW_REVIEW_SKIPPED"
        return self._commit(
            state,
            {
                "review": {
                    "mode": mode,
                    "head_sha": self._active.value["git"]["frozen_head_sha"],
                    "check_reference": check_reference,
                    "findings_digest": findings_digest,
                    "completed_at": now.astimezone(UTC).isoformat(),
                }
            },
            now,
        )

    def begin_revalidation(
        self,
        *,
        head_sha: str,
        tree_sha: str,
        changed_authority_ids: list[str],
        shared_contract_changed: bool,
        now: datetime,
    ) -> BlobRecord:
        if shared_contract_changed:
            raise ContractError(
                "Shared validation contract changed; cleanup and start a new cycle"
            )
        if len(changed_authority_ids) != len(set(changed_authority_ids)):
            raise ContractError("Revalidation authority IDs must be unique")
        scope = copy.deepcopy(self._active.value["scope_freeze"])
        scope["changed_authority_ids"] = sorted(changed_authority_ids)
        return self._commit(
            "REVALIDATING",
            {
                "git": {
                    "current_head_sha": head_sha,
                    "current_tree_sha": tree_sha,
                },
                "scope_freeze": scope,
            },
            now,
        )

    def final_checks(
        self,
        *,
        final_head_sha: str,
        final_tree_sha: str,
        evidence: BlobRecord,
        now: datetime,
    ) -> BlobRecord:
        evidence_digest = evidence.value.get("evidence_digest")
        validate_evidence(evidence.value)
        if (
            not isinstance(evidence_digest, str)
            or evidence.value["candidate_head_sha"] != final_head_sha
        ):
            raise ContractError("Final validation evidence is not current")
        return self._commit(
            "FINAL_CHECKS",
            {
                "git": {
                    "current_head_sha": final_head_sha,
                    "current_tree_sha": final_tree_sha,
                    "final_head_sha": final_head_sha,
                    "final_tree_sha": final_tree_sha,
                },
                "digests": {"evidence_digest": evidence_digest},
                "evidence_reference": {
                    "path": f"{evidence.container}/{evidence.name}",
                    "version_id": evidence.version_id,
                    "etag": evidence.etag,
                    "digest": evidence_digest,
                },
            },
            now,
        )

    def begin_cleanup(
        self,
        *,
        failure: Mapping[str, Any] | None,
        now: datetime,
    ) -> BlobRecord:
        updates: dict[str, Any] = {
            "cleanup": {
                "status": "in_progress",
            }
        }
        if failure is not None:
            updates["failure"] = copy.deepcopy(dict(failure))
        return self._commit("CLEANING", updates, now)

    def complete_cleanup(
        self,
        result: CleanupResult,
        *,
        failed_cycle: bool,
        now: datetime,
    ) -> Any:
        resources = []
        absent = set(result.verified_absent_ids)
        for resource in self._active.value["resources"]:
            item = copy.deepcopy(resource)
            if item["provider_id"] in absent:
                item["state"] = "absence_verified"
                item["delete_observed_at"] = now.astimezone(UTC).isoformat()
            resources.append(item)
        project = copy.deepcopy(self._active.value["project"])
        if project["provider_id"] in absent:
            project["state"] = "deleted"
            project["delete_observed_at"] = now.astimezone(UTC).isoformat()
        cleanup = {
            "status": "exact_clean" if result.exact_clean else "ambiguous",
            "plan_hash": result.plan_hash,
            "exact_clean": result.exact_clean,
            "verified_absent_ids": list(result.verified_absent_ids),
            "retained_shared_manifest_ids": list(
                result.retained_shared_manifest_ids
            ),
            "residue_ids": list(result.residue_ids),
            "verification_at": now.astimezone(UTC).isoformat(),
        }
        state = (
            "FAILED_CLEAN"
            if failed_cycle and result.exact_clean
            else "CLEAN"
            if result.exact_clean
            else "CLEANUP_BLOCKED"
        )
        committed = self._journal.commit(
            self._active,
            lease_id=self._lease_id,
            next_state=state,
            updates={
                "resources": resources,
                "project": project,
                "cleanup": cleanup,
            },
            now=now,
        )
        self._active = committed.active
        if state == "FAILED_CLEAN":
            self._active = self._journal.release(
                self._active,
                lease_id=self._lease_id,
                now=now,
            )
        return committed

    def _commit(
        self,
        state: str,
        updates: Mapping[str, Any],
        now: datetime,
    ) -> BlobRecord:
        with self._lock:
            self._active = self._journal.commit(
                self._active,
                lease_id=self._lease_id,
                next_state=state,
                updates=updates,
                now=now,
            ).active
            return self._active


def _resource(
    *,
    kind: str,
    parent_id: str | None,
    authority_id: str | None,
    deterministic_name: str,
    provider_id: str,
    ownership_nonce: str,
    state: str,
    cleanup_method: str,
    now: datetime,
) -> dict[str, Any]:
    created = state == "created"
    return {
        "kind": kind,
        "parent_id": parent_id,
        "authority_id": authority_id,
        "deterministic_name": deterministic_name,
        "provider_id": provider_id,
        "ownership_nonce": ownership_nonce,
        "create_intent_at": now.astimezone(UTC).isoformat(),
        "create_observed_at": now.astimezone(UTC).isoformat() if created else None,
        "delete_intent_at": None,
        "delete_observed_at": None,
        "state": state,
        "cleanup_method": cleanup_method,
    }


def _mark_created(
    resources: list[dict[str, Any]],
    provider_id: str,
    now: datetime,
) -> list[dict[str, Any]]:
    found = False
    result = []
    for resource in resources:
        item = copy.deepcopy(resource)
        if item["provider_id"] == provider_id:
            item["state"] = "created"
            item["create_observed_at"] = now.astimezone(UTC).isoformat()
            found = True
        result.append(item)
    if not found:
        raise ContractError("Created validation resource has no recorded intent")
    return result


def _observe_resources(
    resources: list[dict[str, Any]],
    observations: Mapping[str, str],
    now: datetime,
) -> list[dict[str, Any]]:
    pending = dict(observations)
    if (
        not pending
        or any(not intent or not provider_id for intent, provider_id in pending.items())
        or len(set(pending.values())) != len(pending)
    ):
        raise ContractError("Validation resource observations are invalid")
    result = []
    for resource in resources:
        item = copy.deepcopy(resource)
        provider_id = pending.pop(item["provider_id"], None)
        if provider_id is not None:
            item["provider_id"] = provider_id
            item["state"] = "created"
            item["create_observed_at"] = now.astimezone(UTC).isoformat()
        result.append(item)
    if pending:
        raise ContractError("Observed validation resource has no recorded intent")
    provider_ids = [item["provider_id"] for item in result]
    if len(provider_ids) != len(set(provider_ids)):
        raise ContractError("Observed validation resources contain a collision")
    return result
