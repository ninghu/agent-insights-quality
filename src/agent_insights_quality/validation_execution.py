from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

from agent_insights_quality.progress import ProgressReporter
from agent_insights_quality.util import ContractError, content_hash, read_json
from agent_insights_quality.validation_cleanup import (
    CleanupBackend,
    CleanupEngine,
    CleanupPlanItem,
    build_cleanup_plan,
)
from agent_insights_quality.validation_cycle import ValidationCycleController
from agent_insights_quality.validation_lifecycle import LocalRecord
from agent_insights_quality.validation_policy import ValidationPolicy
from agent_insights_quality.validation_provisioning import (
    FoundryAuthorityDeployer,
    ProjectDeployment,
    ValidationProjectProvisioner,
)
from agent_insights_quality.validation_quota import (
    CapacityPlan,
    validate_capacity_plan,
)
from agent_insights_quality.validation_runtime import (
    AuthoritySpec,
    DeployedRuntime,
    deploy_all_authorities,
    plan_runtime_topology,
)


def prepare_validation_topology(
    *,
    plan: Mapping[str, Any],
    authorities: list[AuthoritySpec],
    capacity_plan: CapacityPlan,
    controller: ValidationCycleController,
    project_provisioner: ValidationProjectProvisioner,
    deployer_factory: Callable[[ProjectDeployment], FoundryAuthorityDeployer],
    support_image_factory: Callable[[], Mapping[str, str]],
    policy: ValidationPolicy,
    assert_commit: Callable[[], None],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, DeployedRuntime]:
    if plan.get("kind") != "test-agent-validation-plan" or len(authorities) != 41:
        raise ContractError("Validation prepare requires the exact 41-authority plan")
    assert_commit()
    validate_capacity_plan(capacity_plan, policy=policy)
    project_provisioner.assert_test_agent_model(dict(plan["test_agent_model"]))
    if controller.active.value["project"]["state"] == "bound":
        project = _project_from_lifecycle(controller.active.value)
    else:
        controller.preflight(capacity_plan, now=now())
        with lifecycle_heartbeat(controller, now=now):
            project = project_provisioner.bind(str(plan["project_name"]))
        controller.project_bound(
            name=project.project_name,
            provider_id=project.project_id,
            endpoint_reference=content_hash(
                {"project_endpoint": project.project_endpoint}
            ),
            project_principal_id=project.project_principal_id,
            connection_ids=list(project.connection_ids),
            now=now(),
        )
    planned = plan_runtime_topology(
        authorities,
        cycle_suffix=str(plan["cycle_id"]).removeprefix("validation-"),
        policy=policy,
    )
    deployer = deployer_factory(project)
    with lifecycle_heartbeat(controller, now=now):
        deployer.wait_project()
    deployer.set_support_images(support_image_factory())
    planned_by_id = {item.authority_id: item for item in planned}
    ready_by_id = {
        item["authority_id"]: item
        for item in controller.active.value["runtime_topology"]["agents"]
    }
    recoveries = {
        item["authority_id"]: item
        for item in controller.active.value["deployment"]["recoveries"]
    }

    def record_ready(authority: AuthoritySpec, runtime: DeployedRuntime) -> None:
        controller.authority_ready(
            _runtime_agent_payload(planned_by_id[authority.authority_id], runtime),
            now=now(),
        )

    def record_recovery(
        authority: AuthoritySpec,
        state: str,
        retry_count: int,
        error_code: str,
    ) -> None:
        controller.authority_recovery(
            authority_id=authority.authority_id,
            canonical_agent=authority.canonical_agent,
            state=state,
            retry_count=retry_count,
            error_code=error_code,
            now=now(),
        )

    reporter = ProgressReporter("aiq-validation-agent")

    def record_failure(summary: dict[str, Any]) -> None:
        controller.authority_failure(
            authority_id=str(summary["authority_id"]),
            canonical_agent=str(summary["canonical_agent"]),
            stage=str(summary["stage"]),
            error_code=str(summary["error_code"]),
            request_accepted=summary["request_accepted"],
            now=now(),
        )
        reporter.emit(
            f"agent={summary['canonical_agent']} "
            f"authority={summary['authority_id']} stage={summary['stage']} "
            f"code={summary['error_code']}"
        )

    recovered_by_agent: dict[str, list[str]] = {}
    for item in recoveries.values():
        recovered_by_agent.setdefault(str(item["canonical_agent"]), []).append(
            str(item["authority_id"])
        )
    with lifecycle_heartbeat(controller, now=now):
        deployed = deploy_all_authorities(
            authorities,
            planned,
            deployer=deployer,
            maximum_concurrency=capacity_plan.provisioning_concurrency,
            record_resource=lambda event: controller.dynamic_resource_event(
                event,
                now=now(),
            ),
            existing_deployed={
                authority_id: _deployed_runtime(item)
                for authority_id, item in ready_by_id.items()
            },
            retry_counts={
                authority_id: int(item["retry_count"])
                for authority_id, item in recoveries.items()
            },
            max_recovery_versions_per_agent=(
                policy.limits.max_recovery_versions_per_agent
            ),
            record_ready=record_ready,
            record_recovery=record_recovery,
            record_failure=record_failure,
            prior_recovered_authorities=recovered_by_agent,
        )
    runtime_agents = [
        _runtime_agent_payload(item, deployed[item.authority_id])
        for item in planned
    ]
    controller.complete_prepare(runtime_agents, now=now())
    assert_commit()
    return deployed


def _runtime_agent_payload(
    planned: Any,
    runtime: DeployedRuntime,
) -> dict[str, Any]:
    return {
        "authority_id": planned.authority_id,
        "canonical_agent": planned.canonical_agent,
        "logical_version": planned.logical_version,
        "runtime_kind": planned.runtime_kind,
        "framework": planned.framework,
        "runtime_agent_name": runtime.runtime_agent_name,
        "runtime_agent_version": runtime.runtime_agent_version,
        "provider_agent_id": runtime.provider_agent_id,
        "provider_agent_version_id": runtime.provider_agent_version_id,
        "provider_content_digest": runtime.provider_content_digest,
        "hosted_identity_id": runtime.hosted_identity_id,
        "hosted_blueprint_id": runtime.hosted_blueprint_id,
        "hosted_deployment_id": runtime.hosted_deployment_id,
        "foundry_agent_name": runtime.runtime_agent_name,
        "foundry_agent_version": runtime.runtime_agent_version,
        "runtime_principal_id": runtime.runtime_principal_id,
        "telemetry_identity_id": runtime.telemetry_identity_id,
        "connection_ids": list(runtime.connection_ids),
    }


def _deployed_runtime(item: Mapping[str, Any]) -> DeployedRuntime:
    return DeployedRuntime(
        authority_id=str(item["authority_id"]),
        runtime_kind=str(item["runtime_kind"]),
        runtime_agent_name=str(item["runtime_agent_name"]),
        runtime_agent_version=str(item["runtime_agent_version"]),
        provider_agent_id=str(item["provider_agent_id"]),
        provider_agent_version_id=str(item["provider_agent_version_id"]),
        provider_content_digest=str(item["provider_content_digest"]),
        hosted_identity_id=item.get("hosted_identity_id"),
        hosted_blueprint_id=item.get("hosted_blueprint_id"),
        hosted_deployment_id=item.get("hosted_deployment_id"),
        runtime_principal_id=item.get("runtime_principal_id"),
        telemetry_identity_id=str(item["telemetry_identity_id"]),
        connection_ids=tuple(item["connection_ids"]),
    )


def _project_from_lifecycle(
    lifecycle: Mapping[str, Any],
) -> ProjectDeployment:
    project = lifecycle["project"]
    if (
        project["state"] != "bound"
        or not project["provider_id"]
        or not project["project_principal_id"]
        or len(lifecycle["runtime_topology"]["connection_ids"]) != 2
    ):
        raise ContractError("Retained validation Project topology is incomplete")
    return ProjectDeployment(
        project_name=str(project["name"]),
        project_id=str(project["provider_id"]),
        project_principal_id=str(project["project_principal_id"]),
        project_endpoint="",
        connection_ids=tuple(lifecycle["runtime_topology"]["connection_ids"]),
        role_assignment_ids=(),
        resource_observations=(),
    )


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
        controller._commit("CLEANING", {"resources": resources}, now())

    result = CleanupEngine(backend).execute(
        plan,
        record_delete_intent=record_delete_intent,
        record_discovery=lambda item: controller.resource_discovered(
            intent_reference=item.intent_reference,
            provider_id=str(item.resolved_provider_id or ""),
            now=now(),
        ),
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
