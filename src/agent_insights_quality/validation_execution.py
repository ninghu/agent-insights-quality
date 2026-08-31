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
from agent_insights_quality.progress import ProgressReporter
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
    AgentDeploymentIncomplete,
    AuthoritySpec,
    ScenarioAttemptRunner,
    deploy_all_authorities,
    execute_validation_phase,
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
    support_image_factory: Callable[[], Mapping[str, str]],
    runner: ScenarioAttemptRunner,
    scheduler: ValidationScheduler,
    policy: ValidationPolicy,
    model_contract: Mapping[str, Any],
    assert_commit: Callable[[], None],
    record_duration: Callable[[str, float], None] = lambda _stage, _value: None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
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
            support_image_factory=support_image_factory,
            runner=runner,
            scheduler=scheduler,
            policy=policy,
            model_contract=model_contract,
            assert_commit=assert_commit,
            record_duration=record_duration,
            monotonic=monotonic,
            sleep=sleep,
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
    support_image_factory: Callable[[], Mapping[str, str]],
    runner: ScenarioAttemptRunner,
    scheduler: ValidationScheduler,
    policy: ValidationPolicy,
    model_contract: Mapping[str, Any],
    assert_commit: Callable[[], None],
    record_duration: Callable[[str, float], None],
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
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
    if controller.active.value["project"]["state"] == "created":
        project = _project_from_lifecycle(controller.active.value)
    else:
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
                    *(
                        str(item["intent_reference"])
                        for item in project_intents
                    ),
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
    planned = plan_runtime_topology(
        authorities,
        cycle_suffix=plan["cycle_id"].removeprefix("validation-"),
        policy=policy,
    )
    deployer = deployer_factory(project)

    def record_resource(event: dict[str, Any]) -> None:
        controller.dynamic_resource_event(event, now=now())

    planned_by_id = {item.authority_id: item for item in planned}
    ready_by_id = {
        item["authority_id"]: item
        for item in controller.active.value["runtime_topology"]["agents"]
    }
    recovery_by_id = {
        item["authority_id"]: item
        for item in controller.active.value["deployment"]["recoveries"]
    }
    attempted = {
        item["authority_id"]
        for item in controller.active.value["resources"]
        if item.get("authority_id")
        and item["kind"] == "provider_agent"
    }
    for authority in authorities:
        if (
            authority.authority_id in ready_by_id
            or authority.authority_id not in attempted
        ):
            continue
        previous_retry = int(
            recovery_by_id.get(authority.authority_id, {}).get(
                "retry_count",
                0,
            )
        )
        recovered_versions = {
            item["authority_id"]
            for item in recovery_by_id.values()
            if item["canonical_agent"] == authority.canonical_agent
        }
        if (
            previous_retry
            >= policy.limits.max_recovery_versions_per_agent
            or (
                authority.authority_id not in recovered_versions
                and len(recovered_versions)
                >= policy.limits.max_recovery_versions_per_agent
            )
        ):
            summary = {
                "authority_id": authority.authority_id,
                "canonical_agent": authority.canonical_agent,
                "stage": "deployment",
                "error_code": "recovery_exhausted",
                "request_accepted": False,
            }
            controller.authority_failure(
                **summary,
                now=now(),
            )
            raise AgentDeploymentIncomplete([summary])
        controller.authority_recovery(
            authority_id=authority.authority_id,
            canonical_agent=authority.canonical_agent,
            state="ambiguous",
            retry_count=previous_retry + 1,
            error_code="interrupted_deployment",
            now=now(),
        )
    recovery_by_id = {
        item["authority_id"]: item
        for item in controller.active.value["deployment"]["recoveries"]
    }

    def record_ready(authority: AuthoritySpec, runtime: Any) -> None:
        controller.authority_ready(
            _runtime_agent_payload(
                planned_by_id[authority.authority_id],
                runtime,
            ),
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

    failure_reporter = ProgressReporter("aiq-validation-agent")

    def record_agent_failure(summary: dict[str, Any]) -> None:
        controller.authority_failure(
            authority_id=str(summary["authority_id"]),
            canonical_agent=str(summary["canonical_agent"]),
            stage=str(summary["stage"]),
            error_code=str(summary["error_code"]),
            request_accepted=summary["request_accepted"],
            now=now(),
        )
        accepted = summary["request_accepted"]
        failure_reporter.emit(
            "agent="
            f"{summary['canonical_agent']} authority={summary['authority_id']} "
            f"stage={summary['stage']} code={summary['error_code']} "
            "request_accepted="
            f"{'unknown' if accepted is None else str(accepted).lower()}"
        )

    phase_one, phase_two = _validation_phases(authorities, policy)

    def deploy_phase(
        phase: list[AuthoritySpec],
        *,
        require_canaries: bool,
    ) -> dict[str, Any]:
        phase_ids = {item.authority_id for item in phase}
        current_recoveries = {
            item["authority_id"]: item
            for item in controller.active.value["deployment"]["recoveries"]
        }
        recovered_by_agent: dict[str, list[str]] = {}
        for item in current_recoveries.values():
            recovered_by_agent.setdefault(
                str(item["canonical_agent"]),
                [],
            ).append(str(item["authority_id"]))
        with lifecycle_heartbeat(controller, now=now):
            return deploy_all_authorities(
                phase,
                [
                    item
                    for item in planned
                    if item.authority_id in phase_ids
                ],
                deployer=deployer,
                maximum_concurrency=capacity_plan.provisioning_concurrency,
                record_resource=record_resource,
                existing_deployed={
                    authority_id: _deployed_runtime(item)
                    for authority_id, item in ready_by_id.items()
                    if authority_id in phase_ids
                },
                retry_counts={
                    authority_id: int(item["retry_count"])
                    for authority_id, item in current_recoveries.items()
                    if authority_id in phase_ids
                },
                max_recovery_versions_per_agent=(
                    policy.limits.max_recovery_versions_per_agent
                ),
                record_ready=record_ready,
                record_recovery=record_recovery,
                record_failure=record_agent_failure,
                require_architecture_canaries=require_canaries,
                prior_recovered_authorities=recovered_by_agent,
            )

    def wait_clean_interval(phase: str) -> None:
        started = monotonic()
        reporter = ProgressReporter("aiq-validation-clean-window")
        with lifecycle_heartbeat(controller, now=now):
            with reporter.heartbeat(
                f"{phase} pre-traffic clean telemetry interval",
                interval_seconds=60,
            ):
                sleep(policy.limits.clean_interval_seconds)
        record_duration(
            f"{phase}_clean_interval_seconds",
            monotonic() - started,
        )

    with lifecycle_heartbeat(controller, now=now):
        deployer.wait_project()
    phase_one_started = monotonic()
    phase_one_deployed = deploy_phase(
        phase_one,
        require_canaries=True,
    )
    record_duration(
        "phase_1_deployment_seconds",
        monotonic() - phase_one_started,
    )
    wait_clean_interval("phase_1")
    controller.begin_phase_one_traffic(
        {item.authority_id for item in phase_one},
        now=now(),
    )
    with lifecycle_heartbeat(controller, now=now):
        phase_one_evidence = execute_validation_phase(
            phase_one,
            phase_one_deployed,
            runner=runner,
            scheduler=scheduler,
            model_contract=model_contract,
            validated_commit_sha=plan["commit_sha"],
            record_failure=record_agent_failure,
            paired_baselines={
                item.canonical_agent: item.authority_id
                for item in authorities
                if item.authority_kind == "baseline"
            },
        )

    controller.begin_phase_two_deployment(now=now())
    support_images = support_image_factory()
    deployer.set_support_images(support_images)
    phase_two_started = monotonic()
    phase_two_deployed = deploy_phase(
        phase_two,
        require_canaries=False,
    )
    record_duration(
        "phase_2_deployment_seconds",
        monotonic() - phase_two_started,
    )
    deployed = {**phase_one_deployed, **phase_two_deployed}
    runtime_agents = [
        _runtime_agent_payload(item, deployed[item.authority_id])
        for item in planned
    ]
    actual_topology_digest = content_hash(runtime_agents)
    wait_clean_interval("phase_2")
    controller.begin_validation(runtime_agents, now=now())
    if (
        controller.active.value["digests"]["runtime_topology_digest"]
        != actual_topology_digest
    ):
        raise ContractError("Committed validation runtime topology digest changed")
    with lifecycle_heartbeat(controller, now=now):
        phase_two_evidence = execute_validation_phase(
            phase_two,
            deployed,
            runner=runner,
            scheduler=scheduler,
            model_contract=model_contract,
            validated_commit_sha=plan["commit_sha"],
            record_failure=record_agent_failure,
            paired_baselines={
                item.canonical_agent: item.authority_id
                for item in authorities
                if item.authority_kind == "baseline"
            },
        )
    evidence_by_id = {
        item["authority_id"]: item
        for item in [*phase_one_evidence, *phase_two_evidence]
    }
    authority_evidence = [
        evidence_by_id[item.authority_id] for item in authorities
    ]
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


def _runtime_agent_payload(
    planned: Any,
    runtime: Any,
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
        "hosted_identity_id": runtime.hosted_identity_id,
        "hosted_blueprint_id": runtime.hosted_blueprint_id,
        "hosted_deployment_id": runtime.hosted_deployment_id,
        "foundry_agent_name": runtime.runtime_agent_name,
        "foundry_agent_version": runtime.runtime_agent_version,
        "runtime_principal_id": runtime.runtime_principal_id,
        "telemetry_identity_id": runtime.telemetry_identity_id,
        "connection_ids": list(runtime.connection_ids),
    }


def _validation_phases(
    authorities: list[AuthoritySpec],
    policy: ValidationPolicy,
) -> tuple[list[AuthoritySpec], list[AuthoritySpec]]:
    phase_one_ids = {
        f"{policy.prompt_canary_agent}/v0",
        f"{policy.hosted_canary_agent}/v0",
    }
    phase_one = [
        item
        for item in authorities
        if item.authority_id in phase_one_ids
    ]
    phase_two = [
        item
        for item in authorities
        if item.authority_id not in phase_one_ids
    ]
    if (
        {item.authority_id for item in phase_one} != phase_one_ids
        or len(phase_one) != 2
        or len(phase_two) != 39
        or any(
            item.runtime_kind != "prompt"
            for item in phase_one
            if item.canonical_agent == policy.prompt_canary_agent
        )
        or any(
            item.runtime_kind == "prompt"
            for item in phase_one
            if item.canonical_agent == policy.hosted_canary_agent
        )
    ):
        raise ContractError(
            "Validation phase Agent assignments are not reviewed"
        )
    return phase_one, phase_two


def _deployed_runtime(item: Mapping[str, Any]) -> Any:
    from agent_insights_quality.validation_runtime import DeployedRuntime

    return DeployedRuntime(
        authority_id=str(item["authority_id"]),
        runtime_kind=str(item["runtime_kind"]),
        runtime_agent_name=str(item["runtime_agent_name"]),
        runtime_agent_version=str(item["runtime_agent_version"]),
        provider_agent_id=str(item["provider_agent_id"]),
        provider_agent_version_id=str(item["provider_agent_version_id"]),
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
    project_principals = [
        item["provider_id"]
        for item in lifecycle["resources"]
        if item["kind"] == "runtime_principal"
        and item.get("authority_id") is None
        and item["state"] == "created"
    ]
    role_assignments = [
        item["provider_id"]
        for item in lifecycle["resources"]
        if item["kind"] == "role_assignment"
        and item["state"] == "created"
    ]
    if (
        project["state"] != "created"
        or not project["provider_id"]
        or len(project_principals) != 1
        or len(role_assignments) != 4
        or len(lifecycle["runtime_topology"]["connection_ids"]) != 2
    ):
        raise ContractError(
            "Retained validation Project topology is incomplete"
        )
    return ProjectDeployment(
        project_name=str(project["name"]),
        project_id=str(project["provider_id"]),
        project_principal_id=str(project_principals[0]),
        project_endpoint="",
        connection_ids=tuple(
            lifecycle["runtime_topology"]["connection_ids"]
        ),
        role_assignment_ids=tuple(role_assignments),
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
