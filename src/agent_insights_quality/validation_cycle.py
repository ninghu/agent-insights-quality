from __future__ import annotations

import copy
import threading
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_insights_quality.util import ContractError, content_hash
from agent_insights_quality.validation_evidence import validate_evidence
from agent_insights_quality.validation_assignments import (
    verification_assignment,
)
from agent_insights_quality.validation_lifecycle import (
    LifecycleJournal,
    LocalRecord,
    stamp_lifecycle_digest,
    validation_runtime_root,
    validate_topology_resource_bindings,
)
from agent_insights_quality.validation_policy import ValidationPolicy
from agent_insights_quality.validation_quota import CapacityPlan


def initial_lifecycle(
    plan: Mapping[str, Any],
    *,
    policy: ValidationPolicy,
    ownership_nonce: str,
    holder_session_reference: str,
    holder_operator_reference: str,
    holder_run_reference: str,
    substrate: Mapping[str, str],
    recovery_intent: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    if plan.get("kind") != "test-agent-validation-plan":
        raise ContractError("Local validation plan kind is invalid")
    if (
        plan.get("environment_id") != policy.environment_id
        or plan.get("location") != policy.location
        or plan.get("project_name") != policy.project_name
        or plan.get("telemetry_resource_set") != "g30"
    ):
        raise ContractError("Local validation must use the reviewed Sweden environment")
    value = {
        "schema_version": "4.0.0",
        "kind": "test-agent-validation-lifecycle",
        "snapshot_type": "event",
        "run_id": plan["run_id"],
        "event_sequence": 1,
        "state": "LOCKED",
        "repository": plan["repository"],
        "pr_number": plan["pr_number"],
        "commit_sha": plan["commit_sha"],
        "digests": {
            "validation_digest": plan["validation_digest"],
            "shared_validation_digest": plan["shared_validation_digest"],
            "verifier_digest": plan["verifier_digest"],
            "invocation_contract_digest": plan[
                "invocation_contract_digest"
            ],
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
        "substrate": dict(substrate),
        "ownership_nonce": ownership_nonce,
        "capacity": None,
        "project": {
            "name": plan["project_name"],
            "provider_id": None,
            "endpoint_reference": None,
            "project_principal_id": None,
            "state": "unbound",
            "bound_observed_at": None,
        },
        "runtime_topology": {
            "account_reference": content_hash(
                {"account_resource_id": substrate["account_resource_id"]}
            ),
            "project_reference": None,
            "telemetry_resource_set": "g30",
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
            "phase": "preparing",
            "traffic_started": False,
            "support_images": [],
            "recoveries": [],
            "failures": [],
        },
        "desired_state_reference": None,
        "deployment_assignments": [],
        "validation_authority_ids": [
            item["authority_id"] for item in plan["authorities"]
        ],
        "reused_authorities": [],
        "invocation_authority_ids": [],
        "reused_invocations": [],
        "invocation_shard_assignments": [],
        "verification_authority_assignments": [],
        "resources": [],
        "event_reference": None,
        "evidence_reference": None,
        "recovery_intent": (
            copy.deepcopy(dict(recovery_intent))
            if recovery_intent is not None
            else None
        ),
        "supersedes": None,
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

    def project_bound(
        self,
        *,
        name: str,
        provider_id: str,
        endpoint_reference: str,
        project_principal_id: str,
        connection_ids: list[str],
        now: datetime,
    ) -> LocalRecord:
        if name != self._active.value["project"]["name"]:
            raise ContractError("Durable validation Project name changed")
        project = copy.deepcopy(self._active.value["project"])
        project.update(
            {
                "provider_id": provider_id,
                "endpoint_reference": endpoint_reference,
                "project_principal_id": project_principal_id,
                "state": "bound",
                "bound_observed_at": now.astimezone(UTC).isoformat(),
            }
        )
        self._commit(
            "CREATING",
            {
                "project": project,
                "runtime_topology": {
                    "project_reference": project["provider_id"],
                    "connection_ids": connection_ids,
                    "runtime_principal_ids": [project_principal_id],
                },
            },
            now,
        )
        durable_bindings = [
            (
                "runtime_principal",
                project_principal_id,
                f"{name}-project-principal",
            ),
            *[
                (
                    "connection",
                    connection_id,
                    f"{name}-project-connection-{index:02d}",
                )
                for index, connection_id in enumerate(
                    sorted(set(connection_ids)),
                    start=1,
                )
            ],
        ]
        for kind, binding_id, deterministic_name in durable_bindings:
            event = {
                "kind": kind,
                "intent_reference": content_hash(
                    {
                        "kind": kind,
                        "parent_id": provider_id,
                        "provider_id": binding_id,
                    }
                ),
                "deterministic_name": deterministic_name,
                "runtime_kind": "control",
                "discovery_key": f"{provider_id}|{kind}|{binding_id}",
                "authority_id": None,
                "parent_id": provider_id,
                "retention": "retained",
            }
            self.dynamic_resource_event(
                {**event, "state": "create_intent"},
                now=now,
            )
            resource = next(
                item
                for item in self._active.value["resources"]
                if item["intent_reference"] == event["intent_reference"]
            )
            expected = {
                "kind": kind,
                "parent_id": provider_id,
                "authority_id": None,
                "deterministic_name": deterministic_name,
                "runtime_kind": "control",
                "discovery_key": event["discovery_key"],
                "ownership_nonce": self._active.value["ownership_nonce"],
                "retention": "retained",
            }
            if any(resource[key] != value for key, value in expected.items()):
                raise ContractError("Durable validation Project binding changed")
            if resource["state"] == "created":
                if resource["provider_id"] != binding_id:
                    raise ContractError(
                        "Durable validation Project provider binding changed"
                    )
                continue
            if (
                resource["state"] != "create_intent"
                or resource["provider_id"] != event["intent_reference"]
            ):
                raise ContractError(
                    "Durable validation Project binding state is invalid"
                )
            self.dynamic_resource_event(
                {**event, "state": "created", "provider_id": binding_id},
                now=now,
            )
        return self._active

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
                    "Validation Support image cache changed during the run"
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
        retention: str,
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
                    retention=retention,
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
                    "retention": str(event.get("retention") or "retained"),
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
                retention=str(event.get("retention") or "retained"),
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

    def complete_prepare(
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
        selected = [item["authority_id"] for item in runtime_agents]
        return self._commit(
            "VALIDATING",
            {
                "runtime_topology": topology,
                "digests": {"runtime_topology_digest": digest},
                "deployment": {"phase": "prepared", "traffic_started": False},
                "validation_authority_ids": selected,
                "invocation_authority_ids": selected,
                "reused_invocations": [],
                "invocation_shard_assignments": _shard_assignments(
                    selected,
                    quota_plan_digest=self._active.value["digests"][
                        "quota_plan_digest"
                    ],
                ),
                "verification_authority_assignments": [
                    verification_assignment(self._active.value, authority_id)
                    for authority_id in selected
                ],
            },
            now,
        )

    def desired_state_ready(
        self,
        *,
        reference: Mapping[str, str],
        deployment_assignments: list[dict[str, Any]],
        now: datetime,
    ) -> LocalRecord:
        return self._commit(
            "CREATING",
            {
                "desired_state_reference": dict(reference),
                "deployment_assignments": deployment_assignments,
            },
            now,
        )

    def set_authority_selection(
        self,
        *,
        selected_authority_ids: list[str],
        reused_authorities: list[dict[str, str]],
        invocation_authority_ids: list[str],
        reused_invocations: list[dict[str, str]],
        now: datetime,
    ) -> LocalRecord:
        all_ids = {
            item["authority_id"]
            for item in self._active.value["runtime_topology"]["agents"]
        }
        reused_ids = {item["authority_id"] for item in reused_authorities}
        reused_invocation_ids = {
            item["authority_id"] for item in reused_invocations
        }
        if (
            len(selected_authority_ids) != len(set(selected_authority_ids))
            or len(reused_ids) != len(reused_authorities)
            or set(selected_authority_ids).intersection(reused_ids)
            or set(selected_authority_ids).union(reused_ids) != all_ids
            or len(invocation_authority_ids)
            != len(set(invocation_authority_ids))
            or len(reused_invocation_ids) != len(reused_invocations)
            or set(invocation_authority_ids).intersection(
                reused_invocation_ids
            )
            or set(invocation_authority_ids).union(reused_invocation_ids)
            != set(selected_authority_ids)
        ):
            raise ContractError("Validation authority selection is incomplete")
        return self._commit(
            "VALIDATING",
            {
                "validation_authority_ids": selected_authority_ids,
                "reused_authorities": reused_authorities,
                "invocation_authority_ids": invocation_authority_ids,
                "reused_invocations": reused_invocations,
                "invocation_shard_assignments": _shard_assignments(
                    invocation_authority_ids,
                    quota_plan_digest=self._active.value["digests"][
                        "quota_plan_digest"
                    ],
                ),
                "verification_authority_assignments": [
                    verification_assignment(self._active.value, authority_id)
                    for authority_id in selected_authority_ids
                ],
            },
            now,
        )

    def complete(
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
            "READY" if evidence.value["result"] == "PASS" else "FAILED",
            {
                "digests": {"evidence_digest": evidence_digest},
                "evidence_reference": {
                    "path": evidence.path.relative_to(
                        validation_runtime_root()
                    ).as_posix(),
                    "digest": evidence_digest,
                },
                "deployment": {"phase": "complete", "traffic_started": True},
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
    retention: str,
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
        "runtime_kind": runtime_kind,
        "discovery_key": discovery_key,
        "ownership_nonce": ownership_nonce,
        "create_intent_at": now.astimezone(UTC).isoformat(),
        "create_observed_at": now.astimezone(UTC).isoformat() if created else None,
        "state": state,
        "retention": retention,
    }


def _shard_assignments(
    authority_ids: list[str],
    *,
    quota_plan_digest: str,
) -> list[dict[str, Any]]:
    shard_count = min(8, len(authority_ids))
    if shard_count == 0:
        return []
    groups = [
        authority_ids[index::shard_count]
        for index in range(shard_count)
    ]
    return [
        {
            "shard_id": index,
            "authority_ids": group,
            "quota_plan_digest": quota_plan_digest,
        }
        for index, group in enumerate(groups, start=1)
    ]


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
