from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

from agent_insights_quality.util import ContractError, content_hash, read_json
from agent_insights_quality.validation_cycle import ValidationCycleController
from agent_insights_quality.validation_cleanup import (
    CleanupBackend,
    CleanupEngine,
    CleanupPlanItem,
    build_cleanup_plan,
)
from agent_insights_quality.validation_evidence import (
    persist_evidence,
    stamp_evidence_digests,
    validate_evidence,
)
from agent_insights_quality.validation_lifecycle import LocalRecord
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


def execute_validation_plan(
    *,
    plan: Mapping[str, Any],
    authorities: list[AuthoritySpec],
    capacity_plan: CapacityPlan,
    controller: ValidationCycleController,
    project_provisioner: ValidationProjectProvisioner,
    deployer_factory: Callable[[ProjectDeployment], FoundryAuthorityDeployer],
    runner: ScenarioAttemptRunner,
    scheduler: ValidationScheduler,
    policy: ValidationPolicy,
    model_contract: Mapping[str, Any],
    assert_commit: Callable[[], None],
    record_duration: Callable[[str, float], None] = lambda _stage, _value: None,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[dict[str, Any], LocalRecord]:
    try:
        return _execute_validation_plan(
            plan=plan,
            authorities=authorities,
            capacity_plan=capacity_plan,
            controller=controller,
            project_provisioner=project_provisioner,
            deployer_factory=deployer_factory,
            runner=runner,
            scheduler=scheduler,
            policy=policy,
            model_contract=model_contract,
            assert_commit=assert_commit,
            record_duration=record_duration,
            monotonic=monotonic,
            now=now,
        )
    except (ContractError, OSError, RuntimeError) as error:
        state = controller.active.value["state"]
        if state not in {
            "CLEANING",
            "CLEAN",
            "FAILED_CLEAN",
            "CLEANUP_BLOCKED",
        }:
            controller.begin_cleanup(
                failure={
                    "error_code": "validation_execution_failed",
                    "detail_digest": content_hash(
                        {"error_type": type(error).__name__}
                    ),
                    "failed_at": now().astimezone(UTC).isoformat(),
                },
                now=now(),
            )
        raise


def _execute_validation_plan(
    *,
    plan: Mapping[str, Any],
    authorities: list[AuthoritySpec],
    capacity_plan: CapacityPlan,
    controller: ValidationCycleController,
    project_provisioner: ValidationProjectProvisioner,
    deployer_factory: Callable[[ProjectDeployment], FoundryAuthorityDeployer],
    runner: ScenarioAttemptRunner,
    scheduler: ValidationScheduler,
    policy: ValidationPolicy,
    model_contract: Mapping[str, Any],
    assert_commit: Callable[[], None],
    record_duration: Callable[[str, float], None],
    monotonic: Callable[[], float],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[dict[str, Any], LocalRecord]:
    if plan.get("kind") != "test-agent-validation-plan":
        raise ContractError("Local validation plan is invalid")
    if len(authorities) != 41:
        raise ContractError("Initial validation pass requires all 41 authorities")
    assert_commit()
    validate_capacity_plan(capacity_plan, policy=policy)
    project_provisioner.assert_test_agent_model(
        dict(plan["test_agent_model"])
    )
    controller.preflight(capacity_plan, now=now())
    project_id = project_provisioner.expected_project_id(
        plan["project_name"]
    )
    project_started = monotonic()
    controller.project_create_intent(
        name=plan["project_name"],
        provider_id=project_id,
        now=now(),
    )
    ownership_nonce = controller.active.value["ownership_nonce"]
    project_intents = project_provisioner.resource_intents(
        project_name=plan["project_name"],
        cycle_id=plan["cycle_id"],
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
                project_name=plan["project_name"],
                cycle_id=plan["cycle_id"],
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
    record_duration(
        "project_connections_seconds",
        monotonic() - project_started,
    )
    activation_started = monotonic()
    planned = plan_runtime_topology(
        authorities,
        cycle_suffix=plan["cycle_id"].removeprefix("validation-"),
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
    record_duration(
        "agent_activation_seconds",
        monotonic() - activation_started,
    )
    with lifecycle_heartbeat(controller, now=now):
        authority_evidence = execute_validation_matrix(
            authorities,
            deployed,
            runner=runner,
            scheduler=scheduler,
            model_contract=model_contract,
            validated_commit_sha=plan["commit_sha"],
        )
    assert_commit()
    evidence = stamp_evidence_digests(
        {
            "schema_version": "1.0.0",
            "kind": "test-agent-validation-evidence",
            "repository": plan["repository"],
            "pr_number": plan["pr_number"],
            "cycle_id": plan["cycle_id"],
            "commit_sha": plan["commit_sha"],
            "validation_digest": plan["validation_digest"],
            "execution_matrix_digest": plan["execution_matrix_digest"],
            "runtime_topology_digest": actual_topology_digest,
            "resource_inventory_digest": content_hash(
                controller.active.value["resources"]
            ),
            "telemetry_resource_set": "g29",
            "authorities": authority_evidence,
            "evidence_digest": "",
        }
    )
    validate_evidence(
        evidence,
        runtime_topology=controller.active.value["runtime_topology"],
        resources=controller.active.value["resources"],
    )
    if not all(item["pass"] for item in evidence["authorities"]):
        raise ContractError("Local validation evidence did not pass all 41 authorities")
    evidence_record = persist_evidence(
        evidence,
        repository=plan["repository"],
        pr_number=plan["pr_number"],
        cycle_id=plan["cycle_id"],
    )
    controller.final_checks(
        commit_sha=plan["commit_sha"],
        evidence=evidence_record,
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
) -> LocalRecord:
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
        else lifecycle["ownership_nonce"]
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

    def record_discovery(item: CleanupPlanItem) -> None:
        if item.resolved_provider_id is None:
            raise ContractError("Resolved cleanup intent has no provider identity")
        controller.resource_discovered(
            intent_reference=item.intent_reference,
            provider_id=item.resolved_provider_id,
            now=now(),
        )

    result = CleanupEngine(backend).execute(
        plan,
        record_delete_intent=record_delete_intent,
        record_discovery=record_discovery,
    )
    committed = controller.complete_cleanup(
        result,
        failed_cycle=failed_cycle,
        now=now(),
    )
    if committed.value["snapshot_type"] != "active" or (
        committed.value["clean_reference"] is None
    ):
        raise ContractError("Validation cleanup did not create immutable CLEAN proof")
    clean_path = (
        controller.active.path.parent
        / str(committed.value["clean_reference"]["path"])
    )
    clean_value = read_json(clean_path)
    return LocalRecord(
        path=clean_path,
        value=clean_value,
        digest=str(committed.value["clean_reference"]["digest"]),
    )
