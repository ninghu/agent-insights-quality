from __future__ import annotations

import copy
import threading
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_insights_quality.util import ContractError, content_hash
from agent_insights_quality.validation_evidence import validate_evidence
from agent_insights_quality.validation_lifecycle import (
    LifecycleJournal,
    LocalRecord,
    stamp_lifecycle_digest,
    validation_runtime_root,
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
    substrate: Mapping[str, str],
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
            "evidence_resource_inventory_digest": None,
            "clean_resource_inventory_digest": None,
        },
        "operator": {
            "session_reference": holder_session_reference,
            "operator_reference": holder_operator_reference,
            "run_reference": holder_run_reference,
        },
        "substrate": dict(substrate),
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
            "account_reference": content_hash(
                {"account_resource_id": substrate["account_resource_id"]}
            ),
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
        "deployment": {
            "phase": "phase_1_deployment",
            "traffic_started": False,
            "support_images": [],
            "recoveries": [],
            "failures": [],
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
            "failure": None,
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
                intent_reference=content_hash(
                    {"kind": "project", "provider_id": provider_id}
                ),
                provider_id=provider_id,
                runtime_kind="control",
                discovery_key=provider_id,
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

    def support_images_ready(
        self,
        images: Mapping[str, str],
        *,
        now: datetime,
    ) -> LocalRecord:
        entries = [
            {"logical_version": logical, "image": image}
            for logical, image in sorted(images.items())
        ]
        existing = self._active.value["deployment"]["support_images"]
        if existing:
            if existing != entries:
                raise ContractError(
                    "Validation Support image cache changed during the cycle"
                )
            return self._active
        if len(entries) != 9:
            raise ContractError(
                "Validation requires all nine Support images before deployment"
            )
        return self._commit(
            self._active.value["state"],
            {"deployment": {"support_images": entries}},
            now,
        )

    def authority_recovery(
        self,
        *,
        authority_id: str,
        canonical_agent: str,
        state: str,
        retry_count: int,
        error_code: str,
        now: datetime,
    ) -> LocalRecord:
        if state not in {"ambiguous", "failed", "ready"}:
            raise ContractError("Validation deployment recovery state is invalid")
        with self._lock:
            recoveries = [
                copy.deepcopy(item)
                for item in self._active.value["deployment"]["recoveries"]
                if item["authority_id"] != authority_id
            ]
            previous = next(
                (
                    item
                    for item in self._active.value["deployment"]["recoveries"]
                    if item["authority_id"] == authority_id
                ),
                None,
            )
            if previous is not None and (
                previous["canonical_agent"] != canonical_agent
                or retry_count < previous["retry_count"]
            ):
                raise ContractError(
                    "Validation deployment recovery identity changed"
                )
            recoveries.append(
                {
                    "authority_id": authority_id,
                    "canonical_agent": canonical_agent,
                    "state": state,
                    "retry_count": retry_count,
                    "error_code": error_code,
                }
            )
            recoveries.sort(key=lambda item: item["authority_id"])
            return self._commit(
                self._active.value["state"],
                {"deployment": {"recoveries": recoveries}},
                now,
            )

    def authority_ready(
        self,
        runtime_agent: Mapping[str, Any],
        *,
        now: datetime,
    ) -> LocalRecord:
        authority_id = str(runtime_agent.get("authority_id") or "")
        if not authority_id:
            raise ContractError("Ready validation authority identity is missing")
        with self._lock:
            agents = [
                copy.deepcopy(item)
                for item in self._active.value["runtime_topology"]["agents"]
            ]
            existing = next(
                (
                    item
                    for item in agents
                    if item["authority_id"] == authority_id
                ),
                None,
            )
            if existing is not None:
                if existing != dict(runtime_agent):
                    raise ContractError(
                        "Ready validation authority topology changed"
                    )
                return self._active
            agents.append(copy.deepcopy(dict(runtime_agent)))
            agents.sort(key=lambda item: item["authority_id"])
            principal_ids = sorted(
                {
                    *self._active.value["runtime_topology"][
                        "runtime_principal_ids"
                    ],
                    *(
                        [str(runtime_agent["runtime_principal_id"])]
                        if runtime_agent.get("runtime_principal_id")
                        else []
                    ),
                }
            )
            telemetry_ids = sorted(
                {
                    *self._active.value["runtime_topology"][
                        "telemetry_identity_ids"
                    ],
                    str(runtime_agent["telemetry_identity_id"]),
                }
            )
            recoveries = []
            for item in self._active.value["deployment"]["recoveries"]:
                value = copy.deepcopy(item)
                if value["authority_id"] == authority_id:
                    value["state"] = "ready"
                recoveries.append(value)
            return self._commit(
                self._active.value["state"],
                {
                    "runtime_topology": {
                        "agents": agents,
                        "runtime_principal_ids": principal_ids,
                        "telemetry_identity_ids": telemetry_ids,
                    },
                    "deployment": {"recoveries": recoveries},
                },
                now,
            )

    def authority_failure(
        self,
        *,
        authority_id: str,
        canonical_agent: str,
        stage: str,
        error_code: str,
        request_accepted: bool | None,
        now: datetime,
        matched_reference_count: int | None = None,
        expected_reference_count: int | None = None,
        missing_reference_count: int | None = None,
    ) -> LocalRecord:
        if stage not in {
            "deployment",
            "readiness",
            "contract",
            "traffic",
        }:
            raise ContractError("Validation Agent failure stage is invalid")
        correlation_counts = (
            matched_reference_count,
            expected_reference_count,
            missing_reference_count,
        )
        has_correlation_counts = any(value is not None for value in correlation_counts)
        if has_correlation_counts and (
            not all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
                for value in correlation_counts
            )
            or matched_reference_count + missing_reference_count
            != expected_reference_count
        ):
            raise ContractError("Validation telemetry correlation counts are invalid")
        with self._lock:
            failures = [
                copy.deepcopy(item)
                for item in self._active.value["deployment"]["failures"]
                if not (
                    item["authority_id"] == authority_id
                    and item["stage"] == stage
                )
            ]
            failure = {
                "authority_id": authority_id,
                "canonical_agent": canonical_agent,
                "stage": stage,
                "error_code": error_code,
                "request_accepted": request_accepted,
            }
            if has_correlation_counts:
                failure.update(
                    {
                        "matched_reference_count": matched_reference_count,
                        "expected_reference_count": expected_reference_count,
                        "missing_reference_count": missing_reference_count,
                    }
                )
            failures.append(failure)
            failures.sort(
                key=lambda item: (
                    item["canonical_agent"],
                    item["authority_id"],
                    item["stage"],
                )
            )
            return self._commit(
                self._active.value["state"],
                {"deployment": {"failures": failures}},
                now,
            )

    def begin_phase_one_traffic(
        self,
        authority_ids: set[str],
        *,
        now: datetime,
    ) -> LocalRecord:
        ready_ids = {
            item["authority_id"]
            for item in self._active.value["runtime_topology"]["agents"]
        }
        if (
            self._active.value["deployment"]["phase"]
            != "phase_1_deployment"
            or ready_ids != authority_ids
            or self._active.value["deployment"]["failures"]
        ):
            raise ContractError(
                "Validation phase 1 topology is not fully ready"
            )
        return self._commit(
            self._active.value["state"],
            {
                "deployment": {
                    "phase": "phase_1_traffic",
                    "traffic_started": True,
                }
            },
            now,
        )

    def begin_phase_two_deployment(
        self,
        *,
        now: datetime,
    ) -> LocalRecord:
        if (
            self._active.value["deployment"]["phase"]
            != "phase_1_traffic"
            or self._active.value["deployment"]["failures"]
        ):
            raise ContractError(
                "Validation phase 1 did not authorize phase 2"
            )
        return self._commit(
            self._active.value["state"],
            {"deployment": {"phase": "phase_2_deployment"}},
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
        runtime_kind: str,
        discovery_key: str,
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
                    intent_reference=provider_id,
                    provider_id=provider_id,
                    runtime_kind=runtime_kind,
                    discovery_key=discovery_key,
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
            existing = next(
                (
                    item
                    for item in self._active.value["resources"]
                    if item["intent_reference"] == intent_reference
                ),
                None,
            )
            if existing is not None:
                expected = {
                    "kind": str(event["kind"]),
                    "authority_id": event.get("authority_id"),
                    "deterministic_name": str(event["deterministic_name"]),
                    "runtime_kind": str(event["runtime_kind"]),
                    "discovery_key": str(event["discovery_key"]),
                    "cleanup_method": str(
                        event.get("cleanup_method") or "explicit"
                    ),
                }
                if any(existing[key] != value for key, value in expected.items()):
                    raise ContractError(
                        "Resumed validation resource intent changed"
                    )
                return self._active
            return self.resource_create_intent(
                kind=str(event["kind"]),
                parent_id=event.get("parent_id"),
                authority_id=event.get("authority_id"),
                deterministic_name=str(event["deterministic_name"]),
                provider_id=intent_reference,
                runtime_kind=str(event["runtime_kind"]),
                discovery_key=str(event["discovery_key"]),
                cleanup_method=str(event.get("cleanup_method") or "explicit"),
                now=now,
            )
        if state == "ambiguous_create":
            with self._lock:
                resources = []
                found = False
                for resource in self._active.value["resources"]:
                    item = copy.deepcopy(resource)
                    if item["intent_reference"] == intent_reference:
                        if item["state"] != "created":
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
                if item["intent_reference"] == intent_reference:
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

    def resource_discovered(
        self,
        *,
        intent_reference: str,
        provider_id: str,
        now: datetime,
    ) -> LocalRecord:
        with self._lock:
            resources = []
            found = False
            for resource in self._active.value["resources"]:
                item = copy.deepcopy(resource)
                if item["intent_reference"] == intent_reference:
                    item["resolved_provider_id"] = provider_id
                    found = True
                resources.append(item)
            if not found:
                raise ContractError(
                    "Discovered validation resource has no recorded intent"
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
        if (
            len(self._active.value["deployment"]["support_images"]) != 9
            or any(
                item["state"] != "ready"
                for item in self._active.value["deployment"]["recoveries"]
            )
        ):
            raise ContractError(
                "Validation deployment recovery is not fully ready"
            )
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
                "deployment": {
                    "phase": "phase_2_traffic",
                    "traffic_started": True,
                },
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
            resources=self._active.value["resources"],
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
                "digests": {
                    "evidence_digest": evidence_digest,
                    "evidence_resource_inventory_digest": evidence.value[
                        "resource_inventory_digest"
                    ],
                },
                "evidence_reference": {
                    "path": evidence.path.relative_to(
                        validation_runtime_root()
                    ).as_posix(),
                    "digest": evidence_digest,
                },
                "deployment": {"phase": "complete"},
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
                "failure": None,
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
            "failure": None,
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
                "digests": {
                    "clean_resource_inventory_digest": content_hash(resources),
                },
            },
            now=now,
        )
        self._active = committed
        return committed

    def mark_cleanup_blocked(
        self,
        *,
        failure: Mapping[str, Any],
        now: datetime,
    ) -> LocalRecord:
        return self._commit(
            "CLEANUP_BLOCKED",
            {
                "cleanup": {
                    "status": "ambiguous",
                    "exact_clean": False,
                    "residue_ids": ["cleanup_unverified"],
                    "verification_at": now.astimezone(UTC).isoformat(),
                    "failure": copy.deepcopy(dict(failure)),
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
    intent_reference: str,
    provider_id: str,
    runtime_kind: str,
    discovery_key: str,
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
        "intent_reference": intent_reference,
        "provider_id": provider_id,
        "resolved_provider_id": None,
        "runtime_kind": runtime_kind,
        "discovery_key": discovery_key,
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
        provider_id = pending.pop(item["intent_reference"], None)
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
