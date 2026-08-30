from __future__ import annotations

import copy
import threading
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_insights_quality.util import ContractError, content_hash, runtime_root
from agent_insights_quality.validation_evidence import validate_evidence
from agent_insights_quality.validation_lifecycle import (
    LifecycleJournal,
    LocalRecord,
    stamp_lifecycle_digest,
    validate_topology_resource_bindings,
)
from agent_insights_quality.validation_policy import ValidationPolicy
from agent_insights_quality.validation_quota import CapacityPlan
from agent_insights_quality.validation_cleanup import CleanupResult


def initial_lifecycle(
    plan: Mapping[str, Any],
    *,
    policy: ValidationPolicy,
    ownership_nonce: str,
    holder_session_reference: str,
    holder_operator_reference: str,
    holder_run_reference: str,
    account_reference: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    if plan.get("kind") != "test-agent-validation-plan":
        raise ContractError("Local validation plan kind is invalid")
    if plan.get("telemetry_resource_set") != "g29":
        raise ContractError("Local validation must use g29")
    value = {
        "schema_version": "1.0.0",
        "kind": "test-agent-validation-lifecycle",
        "snapshot_type": "event",
        "cycle_id": plan["cycle_id"],
        "revision": 1,
        "state": "LOCKED",
        "repository": plan["repository"],
        "pr_number": plan["pr_number"],
        "commit_sha": plan["commit_sha"],
        "digests": {
            "validation_digest": plan["validation_digest"],
            "execution_matrix_digest": plan["execution_matrix_digest"],
            "runtime_topology_digest": plan["planned_topology_digest"],
            "quota_plan_digest": None,
            "evidence_digest": None,
        },
        "operator": {
            "session_reference": holder_session_reference,
            "operator_reference": holder_operator_reference,
            "run_reference": holder_run_reference,
        },
        "ownership_nonce": ownership_nonce,
        "capacity": None,
        "project": {
            "name": plan["project_name"],
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
        "cleanup": {
            "status": "not_started",
            "plan_hash": None,
            "exact_clean": False,
            "verified_absent_ids": [],
            "retained_shared_manifest_ids": [],
            "residue_ids": [],
            "verification_at": None,
        },
        "event_reference": None,
        "clean_reference": None,
        "evidence_reference": None,
        "started_at": moment.isoformat(),
        "last_activity_at": moment.isoformat(),
        "absolute_expires_at": (
            moment + timedelta(hours=policy.limits.absolute_ttl_hours)
        ).isoformat(),
        "failure": None,
        "previous_journal_digest": None,
        "journal_digest": "",
    }
    return stamp_lifecycle_digest(value)


class ValidationCycleController:
    def __init__(
        self,
        journal: LifecycleJournal,
        *,
        active: LocalRecord,
    ) -> None:
        self._journal = journal
        self._active = active
        self._lock = threading.RLock()

    @property
    def active(self) -> LocalRecord:
        return self._active

    def heartbeat(self, *, now: datetime) -> LocalRecord:
        return self._commit(self._active.value["state"], {}, now)

    def preflight(self, plan: CapacityPlan, *, now: datetime) -> LocalRecord:
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
    ) -> LocalRecord:
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
                ownership_nonce=self._active.value["ownership_nonce"],
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
    ) -> LocalRecord:
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
    ) -> LocalRecord:
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
    ) -> LocalRecord:
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
                    ownership_nonce=self._active.value["ownership_nonce"],
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

    def resource_created(self, provider_id: str, *, now: datetime) -> LocalRecord:
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
    ) -> LocalRecord:
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
    ) -> LocalRecord:
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

    def final_checks(
        self,
        *,
        commit_sha: str,
        evidence: LocalRecord,
        now: datetime,
    ) -> LocalRecord:
        evidence_digest = evidence.value.get("evidence_digest")
        validate_evidence(
            evidence.value,
            runtime_topology=self._active.value["runtime_topology"],
        )
        if (
            not isinstance(evidence_digest, str)
            or evidence.value["commit_sha"] != commit_sha
            or commit_sha != self._active.value["commit_sha"]
        ):
            raise ContractError("Final validation evidence is not current")
        return self._commit(
            "FINAL_CHECKS",
            {
                "digests": {"evidence_digest": evidence_digest},
                "evidence_reference": {
                    "path": evidence.path.relative_to(
                        runtime_root() / "test-agent-validation"
                    ).as_posix(),
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
    ) -> LocalRecord:
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
            next_state=state,
            updates={
                "resources": resources,
                "project": project,
                "cleanup": cleanup,
            },
            now=now,
        )
        self._active = committed
        return committed

    def mark_cleanup_blocked(self, *, now: datetime) -> LocalRecord:
        return self._commit(
            "CLEANUP_BLOCKED",
            {
                "cleanup": {
                    "status": "ambiguous",
                    "exact_clean": False,
                    "residue_ids": ["cleanup_unverified"],
                    "verification_at": now.astimezone(UTC).isoformat(),
                }
            },
            now,
        )

    def _commit(
        self,
        state: str,
        updates: Mapping[str, Any],
        now: datetime,
    ) -> LocalRecord:
        with self._lock:
            self._active = self._journal.commit(
                self._active,
                next_state=state,
                updates=updates,
                now=now,
            )
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
