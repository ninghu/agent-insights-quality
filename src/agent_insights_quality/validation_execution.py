from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

from agent_insights_quality.util import ContractError, content_hash
from agent_insights_quality.validation_cycle import ValidationCycleController
from agent_insights_quality.validation_blob import BlobRecord
from agent_insights_quality.validation_cleanup import (
    CleanupBackend,
    CleanupEngine,
    CleanupPlanItem,
    build_cleanup_plan,
)
from agent_insights_quality.validation_evidence import (
    EvidenceBlobStore,
    persist_evidence,
    stamp_evidence_digests,
    validate_evidence,
)
from agent_insights_quality.validation_provisioning import (
    FoundryAuthorityDeployer,
    ProjectDeployment,
    ValidationProjectProvisioner,
)
from agent_insights_quality.validation_quota import (
    CapacityPlan,
    ValidationScheduler,
    validate_capacity_plan,
)
from agent_insights_quality.validation_policy import ValidationPolicy
from agent_insights_quality.validation_runtime import (
    AuthoritySpec,
    ScenarioAttemptRunner,
    deploy_all_authorities,
    execute_validation_matrix,
    plan_runtime_topology,
)


def execute_initial_candidate_pass(
    *,
    candidate: Mapping[str, Any],
    authorities: list[AuthoritySpec],
    capacity_plan: CapacityPlan,
    controller: ValidationCycleController,
    project_provisioner: ValidationProjectProvisioner,
    deployer_factory: Callable[[ProjectDeployment], FoundryAuthorityDeployer],
    runner: ScenarioAttemptRunner,
    scheduler: ValidationScheduler,
    evidence_store: EvidenceBlobStore,
    policy_manifest_digest: str,
    policy: ValidationPolicy,
    model_contract: Mapping[str, Any],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[dict[str, Any], BlobRecord]:
    try:
        return _execute_initial_candidate_pass(
            candidate=candidate,
            authorities=authorities,
            capacity_plan=capacity_plan,
            controller=controller,
            project_provisioner=project_provisioner,
            deployer_factory=deployer_factory,
            runner=runner,
            scheduler=scheduler,
            evidence_store=evidence_store,
            policy_manifest_digest=policy_manifest_digest,
            policy=policy,
            model_contract=model_contract,
            now=now,
        )
    except (ContractError, OSError, RuntimeError) as error:
        state = controller.active.value["state"]
        if state not in {
            "CLEANING",
            "CLEAN",
            "FAILED_CLEAN",
            "CLEANUP_BLOCKED",
            "RECEIPT_ISSUED",
        }:
            controller.begin_cleanup(
                failure={
                    "error_code": "candidate_execution_failed",
                    "detail_digest": content_hash(
                        {"error_type": type(error).__name__}
                    ),
                    "failed_at": now().astimezone(UTC).isoformat(),
                },
                now=now(),
            )
        raise


def _execute_initial_candidate_pass(
    *,
    candidate: Mapping[str, Any],
    authorities: list[AuthoritySpec],
    capacity_plan: CapacityPlan,
    controller: ValidationCycleController,
    project_provisioner: ValidationProjectProvisioner,
    deployer_factory: Callable[[ProjectDeployment], FoundryAuthorityDeployer],
    runner: ScenarioAttemptRunner,
    scheduler: ValidationScheduler,
    evidence_store: EvidenceBlobStore,
    policy_manifest_digest: str,
    policy: ValidationPolicy,
    model_contract: Mapping[str, Any],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[dict[str, Any], BlobRecord]:
    if candidate.get("kind") != "test-agent-validation-candidate":
        raise ContractError("Validation candidate manifest is invalid")
    if len(authorities) != 41:
        raise ContractError("Initial validation pass requires all 41 authorities")
    validate_capacity_plan(capacity_plan, policy=policy)
    project_provisioner.assert_test_agent_model(
        dict(candidate["test_agent_model"])
    )
    controller.preflight(capacity_plan, now=now())
    project_id = project_provisioner.expected_project_id(
        candidate["project_name"]
    )
    controller.project_create_intent(
        name=candidate["project_name"],
        provider_id=project_id,
        now=now(),
    )
    ownership_nonce = controller.active.value["lease"]["ownership_nonce"]
    project_intents = project_provisioner.resource_intents(
        project_name=candidate["project_name"],
        cycle_id=candidate["cycle_id"],
        ownership_nonce=ownership_nonce,
    )
    for intent in project_intents:
        controller.dynamic_resource_event(
            {**intent, "state": "create_intent"},
            now=now(),
        )
    try:
        with lifecycle_heartbeat(controller, now=now):
            project = project_provisioner.create(
                project_name=candidate["project_name"],
                cycle_id=candidate["cycle_id"],
                ownership_nonce=ownership_nonce,
            )
    except (ContractError, OSError, RuntimeError):
        controller.mark_resources_ambiguous(
            [
                project_id,
                *(str(item["intent_reference"]) for item in project_intents),
            ],
            now=now(),
        )
        raise
    if project.project_id != project_id:
        raise ContractError(
            "Ephemeral validation Project ID differs from create intent"
        )
    project_provisioner.assert_telemetry_connection()
    controller.project_created(
        endpoint_reference=content_hash(
            {"project_endpoint": project.project_endpoint}
        ),
        project_principal_id=project.project_principal_id,
        connection_ids=list(project.connection_ids),
        role_assignment_ids=list(project.role_assignment_ids),
        resource_observations=dict(project.resource_observations),
        now=now(),
    )
    planned = plan_runtime_topology(
        authorities,
        cycle_suffix=candidate["cycle_id"].removeprefix("validation-"),
        policy=policy,
    )
    deployer = deployer_factory(project)

    def record_resource(event: dict[str, Any]) -> None:
        controller.dynamic_resource_event(event, now=now())

    with lifecycle_heartbeat(controller, now=now):
        deployer.wait_project()
        deployed = deploy_all_authorities(
            authorities,
            planned,
            deployer=deployer,
            maximum_concurrency=capacity_plan.provisioning_concurrency,
            record_resource=record_resource,
        )
    runtime_agents = []
    for item in planned:
        runtime = deployed[item.authority_id]
        runtime_agents.append(
            {
                "authority_id": item.authority_id,
                "canonical_agent": item.canonical_agent,
                "logical_version": item.logical_version,
                "runtime_kind": item.runtime_kind,
                "framework": item.framework,
                "runtime_agent_name": runtime.runtime_agent_name,
                "runtime_agent_version": runtime.runtime_agent_version,
                "provider_agent_id": runtime.provider_agent_id,
                "provider_agent_version_id": runtime.provider_agent_version_id,
                "hosted_identity_id": runtime.hosted_identity_id,
                "hosted_blueprint_id": runtime.hosted_blueprint_id,
                "hosted_deployment_id": runtime.hosted_deployment_id,
                "foundry_agent_name": runtime.runtime_agent_name,
                "foundry_agent_version": runtime.runtime_agent_version,
                "runtime_principal_id": runtime.runtime_principal_id,
                "telemetry_identity_id": runtime.telemetry_identity_id,
                "connection_ids": list(runtime.connection_ids),
            }
        )
    actual_topology_digest = content_hash(runtime_agents)
    controller.begin_validation(runtime_agents, now=now())
    if (
        controller.active.value["digests"]["runtime_topology_digest"]
        != actual_topology_digest
    ):
        raise ContractError("Committed validation runtime topology digest changed")
    with lifecycle_heartbeat(controller, now=now):
        authority_evidence = execute_validation_matrix(
            authorities,
            deployed,
            runner=runner,
            scheduler=scheduler,
            model_contract=model_contract,
            validated_head_sha=candidate["candidate_head_sha"],
        )
    evidence = stamp_evidence_digests(
        {
            "schema_version": "1.0.0",
            "kind": "test-agent-validation-evidence",
            "cycle_id": candidate["cycle_id"],
            "epoch": controller.active.value["epoch"],
            "repository": candidate["repository"],
            "pr_number": candidate["pr_number"],
            "candidate_head_sha": candidate["candidate_head_sha"],
            "candidate_tree_sha": candidate["candidate_tree_sha"],
            "policy_manifest_digest": policy_manifest_digest,
            "catalog_hashes": dict(candidate["catalog_hashes"]),
            "artifact_manifest_hash": candidate["artifact_manifest_hash"],
            "source_tree_digest": candidate["source_tree_digest"],
            "validation_contract_digest": candidate[
                "validation_contract_digest"
            ],
            "execution_matrix_digest": candidate[
                "execution_matrix_digest"
            ],
            "runtime_topology_digest": actual_topology_digest,
            "quota_plan_digest": capacity_plan.plan_digest,
            "telemetry_resource_set": "g29",
            "test_agent_model": dict(candidate["test_agent_model"]),
            "authorities": authority_evidence,
            "evidence_digest": "",
        }
    )
    validate_evidence(
        evidence,
        runtime_topology=controller.active.value["runtime_topology"],
    )
    evidence_record = persist_evidence(evidence_store, evidence)
    controller.freeze(
        evidence=evidence_record,
        head_sha=candidate["candidate_head_sha"],
        tree_sha=candidate["candidate_tree_sha"],
        now=now(),
    )
    return evidence, controller.active


@contextmanager
def lifecycle_heartbeat(
    controller: ValidationCycleController,
    *,
    now: Callable[[], datetime],
    interval_seconds: int = 45,
) -> Iterator[None]:
    if interval_seconds <= 0 or interval_seconds >= 60:
        raise ContractError("Validation heartbeat interval must be below 60 seconds")
    stopped = threading.Event()
    failures: list[BaseException] = []

    def pulse() -> None:
        while not stopped.wait(interval_seconds):
            try:
                controller.heartbeat(now=now())
            except (ContractError, OSError, RuntimeError) as error:
                failures.append(error)
                return

    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=interval_seconds)
    if failures:
        raise ContractError("Validation lifecycle heartbeat failed") from failures[0]


def cleanup_validation_cycle(
    *,
    controller: ValidationCycleController,
    backend: CleanupBackend,
    policy: ValidationPolicy,
    failed_cycle: bool,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BlobRecord:
    controller.begin_cleanup(failure=None, now=now())
    lifecycle = controller.active.value
    ownership_nonces = {
        item["ownership_nonce"] for item in lifecycle["resources"]
    }
    if len(ownership_nonces) > 1:
        raise ContractError("Validation resources have mixed ownership")
    ownership_nonce = (
        next(iter(ownership_nonces))
        if ownership_nonces
        else lifecycle["lease"]["ownership_nonce"]
    )
    plan = build_cleanup_plan(
        cycle_id=lifecycle["cycle_id"],
        ownership_nonce=ownership_nonce,
        resources=lifecycle["resources"],
        documented_project_cascade=policy.documented_project_cascade,
    )

    def record_delete_intent(item: CleanupPlanItem) -> None:
        resources = []
        found = False
        for resource in controller.active.value["resources"]:
            value = dict(resource)
            if resource["provider_id"] == item.provider_id:
                value["state"] = "delete_intent"
                value["delete_intent_at"] = now().astimezone(UTC).isoformat()
                found = True
            resources.append(value)
        if not found:
            raise ContractError("Cleanup resource disappeared before delete intent")
        controller._commit(
            "CLEANING",
            {"resources": resources},
            now(),
        )

    result = CleanupEngine(backend).execute(
        plan,
        record_delete_intent=record_delete_intent,
    )
    committed = controller.complete_cleanup(
        result,
        failed_cycle=failed_cycle,
        now=now(),
    )
    if committed.clean is None:
        raise ContractError("Validation cleanup did not create immutable CLEAN proof")
    return committed.clean
