from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.live import LiveRuntime, TelemetryOnlyRuntime
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.registry import publish_validation_registry
from agent_insights_quality.util import (
    ContractError,
    atomic_json,
    content_hash,
    immutable_json,
    read_json,
)
from agent_insights_quality.validation_credentials import local_azure_operator
from agent_insights_quality.validation_cycle import (
    ValidationCycleController,
    initial_lifecycle,
)
from agent_insights_quality.validation_evidence import (
    load_reused_authority_evidence,
    persist_evidence,
    select_reusable_authority_evidence,
    stamp_evidence_digests,
    validate_evidence,
)
from agent_insights_quality.validation_execution import (
    _deployed_runtime,
    _project_from_lifecycle,
    _runtime_agent_payload,
    lifecycle_heartbeat,
)
from agent_insights_quality.validation_lifecycle import (
    LifecycleJournal,
    LocalValidationLock,
    validation_runtime_root,
)
from agent_insights_quality.validation_leases import CrossProcessTelemetryLease
from agent_insights_quality.validation_invocations import (
    assert_invocation_receipt_set_isolated,
    extract_legacy_shard_invocations,
    load_bound_invocation_receipt,
    load_invocation_receipt,
    recover_supplemental_legacy_invocations,
    select_reusable_invocation_receipts,
    write_invocation_receipt,
)
from agent_insights_quality.validation_live import (
    FoundryScenarioAttemptRunner,
    FoundryScenarioVerifier,
)
from agent_insights_quality.validation_local import (
    _capacity_from_lifecycle,
    _substrate,
    discover_local_git_context,
)
from agent_insights_quality.validation_manifest import (
    authority_specs,
    prepare_bound_validation_plan,
    prepare_validation_plan,
    validate_validation_plan,
    validation_authority_cost,
    validation_endpoint_costs,
)
from agent_insights_quality.validation_permissions import (
    assert_validation_permissions,
)
from agent_insights_quality.validation_policy import load_validation_policy
from agent_insights_quality.validation_provisioning import (
    FoundryAuthorityDeployer,
    ValidationProjectProvisioner,
    measure_test_agent_capacity,
    prepare_validation_support_images,
    validation_runtime_profile,
)
from agent_insights_quality.validation_quota import (
    CapacityPlan,
    ValidationScheduler,
    WeightedTokenBucket,
    build_capacity_plan,
)
from agent_insights_quality.validation_runtime import (
    deploy_all_authorities,
    deployment_resource_events,
    invoke_validation_shard,
    plan_runtime_topology,
    verify_validation_shard,
)
from agent_insights_quality.validation_shards import (
    ValidationDeploymentShardStore,
    ValidationShardStore,
    authority_lock,
    compose_shard_authorities,
    import_shard_resources,
    shard_lock,
    validate_shard_assignment,
)

INVOKE_SHARD_CONCURRENCY = 8


def prepare_test_agent_validation() -> dict[str, Any]:
    git = discover_local_git_context()
    policy = load_validation_policy()
    agents, issues = load_catalogs()
    authorities = authority_specs(agents, issues)
    operator = local_azure_operator()
    base_profile = RuntimeProfile.from_env("staging", "g30")
    lock = LocalValidationLock(validation_runtime_root() / "coordinator.lock")
    with lock:
        journal = LifecycleJournal(lock=lock)
        plan = prepare_validation_plan(
            agents=agents,
            issues=issues,
            policy=policy,
            repository=git.repository,
            pr_number=git.pr_number,
            commit_sha=git.commit_sha,
            local_run_id=uuid.uuid4().hex,
        )
        validate_validation_plan(
            plan,
            agents=agents,
            issues=issues,
            policy=policy,
        )
        profile = validation_runtime_profile(
            plan["project_name"],
            run_id=plan["run_id"],
            base=base_profile,
        )
        assert_validation_permissions(base_profile, operator)
        LiveRuntime(
            base_profile,
            token_provider=operator.token_provider,
        ).assert_telemetry_read_access()
        capacity = build_capacity_plan(
            measure_test_agent_capacity(profile),
            policy=policy,
            costs=validation_endpoint_costs(authorities),
        )
        initial = initial_lifecycle(
            plan,
            policy=policy,
            ownership_nonce=uuid.uuid4().hex,
            holder_session_reference=content_hash({"session": uuid.uuid4().hex}),
            holder_operator_reference=operator.operator_reference,
            holder_run_reference=content_hash({"run": uuid.uuid4().hex}),
            substrate=_substrate(operator, base_profile),
        )
        with recover_supplemental_legacy_invocations(
            active_path=journal.active_path,
            plan=plan,
            authorities=authorities,
        ) as supplemental:
            with extract_legacy_shard_invocations(
                active_path=journal.active_path,
                plan=plan,
                authorities=authorities,
            ) as migration:
                incomplete_current_invocations = (
                    _incomplete_current_invocations(
                        journal=journal,
                        plan=plan,
                        authorities=authorities,
                    )
                )
                active, superseded_authority_ids = journal.begin_run(
                    initial,
                    all_authority_ids=[
                        item.authority_id for item in authorities
                    ],
                )
        controller = ValidationCycleController(journal, active=active)
        controller.preflight(capacity, now=datetime.now(UTC))
        provisioner = ValidationProjectProvisioner(
            profile,
            local_operator_id=operator.object_id,
            policy=policy,
        )
        with lifecycle_heartbeat(controller, now=lambda: datetime.now(UTC)):
            project = provisioner.bind(plan["project_name"])
        controller.project_bound(
            name=project.project_name,
            provider_id=project.project_id,
            endpoint_reference=content_hash(
                {"project_endpoint": project.project_endpoint}
            ),
            project_principal_id=project.project_principal_id,
            connection_ids=list(project.connection_ids),
            now=datetime.now(UTC),
        )
        support_agent = next(
            item
            for item in agents["agents"]
            if item["name"] == "support-ticket-agent"
        )

        deployer = FoundryAuthorityDeployer(
            profile=profile,
            agent_catalog=agents,
            issue_catalog=issues,
            token_provider=operator.token_provider,
            project=project,
            support_images={},
        )
        with lifecycle_heartbeat(controller, now=lambda: datetime.now(UTC)):
            deployer.wait_project()
            images = prepare_validation_support_images(profile, support_agent).images
        controller.support_images_ready(images, now=datetime.now(UTC))
        deployer.set_support_images(images)
        planned = plan_runtime_topology(
            authorities,
            run_suffix=plan["run_id"].removeprefix("validation-"),
            policy=policy,
        )
        desired = _desired_state(
            plan=plan,
            authorities=authorities,
            planned=list(planned),
            deployer=deployer,
            support_images=images,
            superseded_authority_ids=superseded_authority_ids,
            forced_invocation_authority_ids=[
                *_forced_invocation_authority_ids(
                    migration=migration,
                    supplemental=supplemental,
                    incomplete_current_invocations=(
                        incomplete_current_invocations
                    ),
                ),
            ],
            quota_plan_digest=controller.active.value["digests"][
                "quota_plan_digest"
            ],
        )
        desired_path = (
            validation_runtime_root()
            / "desired-state"
            / git.repository.replace("/", "--")
            / str(git.pr_number)
            / plan["run_id"]
            / f"{desired['desired_state_digest'].removeprefix('sha256:')}.json"
        )
        immutable_json(desired_path, desired)
        controller.desired_state_ready(
            reference={
                "path": desired_path.relative_to(
                    validation_runtime_root()
                ).as_posix(),
                "digest": desired["desired_state_digest"],
            },
            deployment_assignments=desired["deployment_assignments"],
            now=datetime.now(UTC),
        )
        return _prepared_result(controller.active.value)


def run_test_agent_validation() -> dict[str, Any]:
    git = discover_local_git_context()
    active = _matching_active(git)
    if active is None:
        return {
            "status": "unprepared",
            "next_commands": [
                "python -m agent_insights_quality prepare-test-agent-validation"
            ],
        }
    if active is not None and active["state"] in {"READY", "FAILED"}:
        evidence = read_json(
            validation_runtime_root() / active["evidence_reference"]["path"]
        )
        return {
            "status": active["state"].casefold(),
            "result": evidence["result"],
            "authority_count": 41,
            "validated_authority_count": len(
                active["validation_authority_ids"]
            ),
            "reused_authority_count": len(active["reused_authorities"]),
            "evidence_digest": evidence["evidence_digest"],
        }
    if active["state"] == "CREATING":
        return {
            "status": "deployment_pending",
            "maximum_active_subsessions": 8,
            "shards": active["deployment_assignments"],
            "next_commands": [
                "python -m agent_insights_quality "
                f"deploy-test-agent-validation-shard --shard-id {item['shard_id']}"
                for item in active["deployment_assignments"]
            ]
            + [
                "python -m agent_insights_quality "
                "reconcile-test-agent-validation-deployment"
            ],
        }
    if active["state"] == "VALIDATING":
        return {
            "status": "validation_pending",
            "maximum_active_subsessions": 8,
            "invoke_shards": active["invocation_shard_assignments"],
            "verify_shards": active["shard_assignments"],
            "next_commands": [
                "python -m agent_insights_quality "
                f"invoke-test-agent-validation-shard --shard-id {item['shard_id']}"
                for item in active["invocation_shard_assignments"]
            ]
            + [
                "python -m agent_insights_quality "
                f"verify-test-agent-validation-shard --shard-id {item['shard_id']}"
                for item in active["shard_assignments"]
            ]
            + [
                "python -m agent_insights_quality compose-test-agent-validation"
            ],
        }
    return {
        "status": active["state"].casefold(),
        "next_commands": [],
    }


def deploy_test_agent_validation_shard(
    *,
    shard_id: int,
) -> dict[str, Any]:
    active = _active_for_state("CREATING")
    authority_ids = _assignment_authority_ids(
        active,
        field="deployment_assignments",
        shard_id=shard_id,
    )
    context = _deployment_context(shard_id, authority_ids)
    store = context["store"]
    completed = store.completed_authority_ids()
    by_id = {item.authority_id: item for item in context["authorities"]}
    planned_by_id = {
        item.authority_id: item for item in context["planned"]
    }
    desired_by_id = {
        item["authority_id"]: item
        for item in context["desired"]["authorities"]
    }
    for authority_id in authority_ids:
        if authority_id in completed:
            continue
        with authority_lock(
            run_id=context["active"]["run_id"],
            authority_id=authority_id,
        ):
            if authority_id in store.completed_authority_ids():
                continue
            resources: list[dict[str, Any]] = []
            deployed = deploy_all_authorities(
                [by_id[authority_id]],
                [planned_by_id[authority_id]],
                deployer=context["deployer"],
                maximum_concurrency=1,
                record_resource=resources.append,
                max_recovery_versions_per_agent=(
                    context["policy"].limits.max_recovery_versions_per_agent
                ),
            )[authority_id]
            if (
                deployed.provider_content_digest
                != desired_by_id[authority_id]["provider_content_digest"]
            ):
                raise ContractError(
                    "Deployed authority differs from immutable desired state"
                )
            store.write_authority(
                authority_id=authority_id,
                runtime=deployed,
                resources=resources,
            )
    receipt = store.complete()
    return {
        "status": "deployed",
        "shard_id": shard_id,
        "authority_count": len(authority_ids),
        "receipt_digest": receipt["receipt_digest"],
    }


def reconcile_test_agent_validation_deployment() -> dict[str, Any]:
    active = _active_for_state("CREATING")
    desired = _load_desired_state(active)
    contexts = _deployment_context()
    receipts = [
        ValidationDeploymentShardStore(
            prepared=active,
            shard_id=assignment["shard_id"],
            authority_ids=assignment["authority_ids"],
            desired_state_digest=desired["desired_state_digest"],
        ).read()
        for assignment in active["deployment_assignments"]
    ]
    deployed_by_id = {
        receipt["authority_id"]: _deployed_runtime(receipt["runtime"])
        for shard in receipts
        for receipt in shard["authorities"]
    }
    deployed_by_id.update(
        {
            item["authority_id"]: _deployed_runtime(item["runtime"])
            for item in desired["reused_runtimes"]
        }
    )
    authorities = contexts["authorities"]
    planned = contexts["planned"]
    if set(deployed_by_id) != {item.authority_id for item in authorities}:
        raise ContractError("Deployment receipts do not cover desired topology")
    by_id = {item.authority_id: item for item in authorities}
    contexts["deployer"].assert_no_monitors()
    for authority in authorities:
        contexts["deployer"].assert_ready(
            authority,
            deployed_by_id[authority.authority_id],
        )

    lock = LocalValidationLock(validation_runtime_root() / "coordinator.lock")
    with lock:
        journal = LifecycleJournal(lock=lock)
        current = journal.read_active()
        if (
            current.value["state"] != "CREATING"
            or current.value["run_id"] != active["run_id"]
            or current.value["desired_state_reference"]
            != active["desired_state_reference"]
        ):
            raise ContractError("Stale deployment reconciliation is fenced")
        controller = ValidationCycleController(journal, active=current)
        deployment_artifacts = [
            {
                "shard_id": shard["shard_id"],
                "resources": [
                    event
                    for receipt in shard["authorities"]
                    for event in receipt["resources"]
                ],
            }
            for shard in receipts
        ]
        for item in desired["reused_runtimes"]:
            authority_id = item["authority_id"]
            deployment_artifacts.append(
                {
                    "shard_id": 100 + len(deployment_artifacts),
                    "resources": deployment_resource_events(
                        by_id[authority_id],
                        next(
                            entry
                            for entry in planned
                            if entry.authority_id == authority_id
                        ),
                        deployed_by_id[authority_id],
                    ),
                }
            )
        import_shard_resources(
            controller,
            deployment_artifacts,
            now=lambda: datetime.now(UTC),
        )
        planned_by_id = {item.authority_id: item for item in planned}
        runtime_agents = []
        for authority in authorities:
            payload = _runtime_agent_payload(
                planned_by_id[authority.authority_id],
                deployed_by_id[authority.authority_id],
            )
            controller.authority_ready(payload, now=datetime.now(UTC))
            runtime_agents.append(payload)
        _write_deployment_registry(
            controller.active.value,
            desired=desired,
            profile=contexts["profile"],
        )
        controller.complete_prepare(runtime_agents, now=datetime.now(UTC))
        selected, reused = select_reusable_authority_evidence(
            authorities=authorities,
            runtime_topology=controller.active.value["runtime_topology"],
            repository=controller.active.value["repository"],
            pr_number=controller.active.value["pr_number"],
            environment_id=contexts["policy"].environment_id,
            location=contexts["policy"].location,
            telemetry_resource_set=contexts["policy"].telemetry_resource_set,
            shared_validation_digest=desired["shared_validation_digest"],
            forced_authority_ids=set(
                desired["forced_validation_authority_ids"]
            ),
        )
        invocation_selected, reused_invocations = (
            select_reusable_invocation_receipts(
                authorities=authorities,
                authority_ids=selected,
                runtime_topology=controller.active.value["runtime_topology"],
                prepared=controller.active.value,
                plan=contexts["plan"],
                forced_authority_ids=set(
                    desired["forced_invocation_authority_ids"]
                ),
            )
        )
        controller.set_authority_selection(
            selected_authority_ids=selected,
            reused_authorities=reused,
            invocation_authority_ids=invocation_selected,
            reused_invocations=reused_invocations,
            now=datetime.now(UTC),
        )
        return _prepared_result(controller.active.value)


def invoke_test_agent_validation_shard(
    *,
    shard_id: int,
) -> dict[str, Any]:
    context = _load_prepared(
        shard_id,
        assignment_field="invocation_shard_assignments",
        partition_invoke_capacity=True,
    )
    run_id = context["prepared"]["run_id"]
    with shard_lock(
        repository=context["git"].repository,
        pr_number=context["git"].pr_number,
        run_id=run_id,
        shard_id=shard_id,
    ):
        store = context["store"]
        artifact = store.begin_invocation()
        completed_ids = store.completed_invocation_authority_ids()
        pending = [
            item
            for item in context["assigned"]
            if item.authority_id not in completed_ids
        ]
        if artifact["status"] == "invoked":
            return {
                "status": "invoked",
                "shard_id": shard_id,
                "authority_count": len(context["assigned"]),
                "artifact_digest": artifact["artifact_digest"],
            }
        for authority in pending:
            resources: list[dict[str, Any]] = []
            runner = _runner(context, record_resource=resources.append)
            runner.prepare_hosted_routes(
                _authority_targets(context, authority)
            )
            invocation = invoke_validation_shard(
                [authority],
                context["deployed"],
                runner=runner,
                scheduler=context["scheduler"],
                model_contract=context["agents"]["models"]["test_agents"],
                paired_baselines=context["paired_baselines"],
            )[0]
            reference = write_invocation_receipt(
                prepared=context["prepared"],
                plan=context["plan"],
                shard_id=shard_id,
                authority=authority,
                runtime=context["deployed"][authority.authority_id],
                paired_v0_authority=(
                    None
                    if authority.authority_kind == "baseline"
                    else next(
                        item
                        for item in context["authorities"]
                        if item.authority_id
                        == context["paired_baselines"][
                            authority.canonical_agent
                        ]
                    )
                ),
                paired_v0_runtime=(
                    None
                    if authority.authority_kind == "baseline"
                    else context["deployed"][
                        context["paired_baselines"][authority.canonical_agent]
                    ]
                ),
                invocation=invocation,
                resources=resources,
                fence=store.assert_active,
            )
            store.record_invocation_receipt(reference)
        artifact = store.complete_invocation()
        return {
            "status": "invoked",
            "shard_id": shard_id,
            "authority_count": len(context["assigned"]),
            "artifact_digest": artifact["artifact_digest"],
        }


def verify_test_agent_validation_shard(
    *,
    shard_id: int,
) -> dict[str, Any]:
    active = _active_for_state("VALIDATING")
    authority_ids = _assignment_authority_ids(
        active,
        field="shard_assignments",
        shard_id=shard_id,
    )
    context = _load_prepared(
        shard_id,
        assignment_field="shard_assignments",
    )
    run_id = context["prepared"]["run_id"]
    with shard_lock(
        repository=context["git"].repository,
        pr_number=context["git"].pr_number,
        run_id=run_id,
        shard_id=shard_id,
    ):
        store = context["store"]
        if store.package_exists():
            package = store.read_package()
            return {
                "status": "verified",
                "shard_id": shard_id,
                "authority_count": len(package["authorities"]),
                "failed_authority_count": sum(
                    item["pass"] is not True
                    for item in package["authorities"]
                ),
                "artifact_digest": package["artifact_digest"],
            }
        references, receipts = _invocation_receipts_for_verification(
            context,
            authority_ids,
        )
        authorities = verify_validation_shard(
            context["assigned"],
            context["deployed"],
            [item["invocation"] for item in receipts],
            runner=_verifier(context),
            scheduler=context["scheduler"],
            model_contract=context["agents"]["models"]["test_agents"],
            validated_commit_sha=context["git"].commit_sha,
            paired_baselines=context["paired_baselines"],
        )
        package = store.write_package(
            authorities=authorities,
            invocation_receipts=references,
        )
        return {
            "status": "verified",
            "shard_id": shard_id,
            "authority_count": len(authorities),
            "failed_authority_count": sum(
                item["pass"] is not True for item in authorities
            ),
            "artifact_digest": package["artifact_digest"],
        }


def compose_test_agent_validation() -> dict[str, Any]:
    context = _load_prepared()
    prepared = context["prepared"]
    packages = []
    invocation_receipts: dict[str, dict[str, Any]] = {}
    for assignment in prepared["shard_assignments"]:
        store = ValidationShardStore(
            prepared=prepared,
            shard_id=assignment["shard_id"],
            authority_ids=assignment["authority_ids"],
        )
        package = store.read_package()
        if (
            package.get("verifier_commit_sha") != prepared["commit_sha"]
            or package.get("verifier_digest")
            != prepared["digests"]["shared_validation_digest"]
        ):
            raise ContractError(
                "Validation shard package verifier binding is stale"
            )
        for reference in package["invocation_receipts"]:
            receipt = load_invocation_receipt(reference)
            authority_id = receipt["authority_id"]
            if authority_id in invocation_receipts:
                raise ContractError(
                    "Validation invocation receipt coverage collides"
                )
            invocation_receipts[authority_id] = receipt
        packages.append(package)
    fresh = compose_shard_authorities(
        packages,
        context["assigned"],
    )
    reused = [
        load_reused_authority_evidence(reference)
        for reference in prepared["reused_authorities"]
    ]
    by_id = {
        item["authority_id"]: item
        for item in [*fresh, *reused]
    }
    authority_evidence = [
        by_id[item.authority_id] for item in context["authorities"]
    ]
    if len(by_id) != len(context["authorities"]):
        raise ContractError("Validation composition authority coverage is incomplete")

    lock = LocalValidationLock(validation_runtime_root() / "coordinator.lock")
    with lock:
        journal = LifecycleJournal(lock=lock)
        controller = ValidationCycleController(
            journal,
            active=journal.read_active(),
        )
        if (
            controller.active.value["state"] != "VALIDATING"
            or controller.active.value["run_id"] != prepared["run_id"]
            or controller.active.value["desired_state_reference"]
            != prepared["desired_state_reference"]
        ):
            raise ContractError("Stale validation composition is fenced")
        import_shard_resources(
            controller,
            [
                {
                    "shard_id": receipt["origin_shard_id"],
                    "resources": receipt["resources"],
                }
                for receipt in invocation_receipts.values()
            ],
            now=lambda: datetime.now(UTC),
        )
        active = controller.active.value
        evidence = stamp_evidence_digests(
            {
                "schema_version": "2.0.0",
                "kind": "test-agent-validation-evidence",
                "repository": active["repository"],
                "pr_number": active["pr_number"],
                "run_id": active["run_id"],
                "commit_sha": active["commit_sha"],
                "completed_at": datetime.now(UTC).isoformat(),
                "result": (
                    "PASS"
                    if all(item["pass"] for item in authority_evidence)
                    else "FAIL"
                ),
                "validation_digest": active["digests"]["validation_digest"],
                "shared_validation_digest": active["digests"][
                    "shared_validation_digest"
                ],
                "execution_matrix_digest": active["digests"][
                    "execution_matrix_digest"
                ],
                "runtime_topology_digest": active["digests"][
                    "runtime_topology_digest"
                ],
                "resource_inventory_digest": content_hash(active["resources"]),
                "environment_id": context["plan"]["environment_id"],
                "location": context["plan"]["location"],
                "telemetry_resource_set": "g30",
                "authorities": authority_evidence,
                "evidence_digest": "",
            }
        )
        validate_evidence(
            evidence,
            runtime_topology=active["runtime_topology"],
            resources=active["resources"],
        )
        record = persist_evidence(
            evidence,
            repository=active["repository"],
            pr_number=active["pr_number"],
            run_id=active["run_id"],
        )
        controller.complete(
            commit_sha=active["commit_sha"],
            evidence=record,
            now=datetime.now(UTC),
        )
        return {
            "status": "complete",
            "result": evidence["result"],
            "authority_count": len(authority_evidence),
            "validated_authority_count": len(fresh),
            "reused_authority_count": len(reused),
            "evidence_digest": evidence["evidence_digest"],
        }


def _load_prepared(
    shard_id: int | None = None,
    *,
    assignment_field: str = "shard_assignments",
    partition_invoke_capacity: bool = False,
) -> dict[str, Any]:
    git = discover_local_git_context()
    policy = load_validation_policy()
    agents, issues = load_catalogs()
    authorities = authority_specs(agents, issues)
    coordinator_lock = LocalValidationLock(
        validation_runtime_root() / "coordinator.lock"
    )
    prepared = LifecycleJournal(lock=coordinator_lock).read_active().value
    plan = prepare_bound_validation_plan(
        agents=agents,
        issues=issues,
        policy=policy,
        repository=git.repository,
        pr_number=git.pr_number,
        commit_sha=git.commit_sha,
        run_id=prepared["run_id"],
    )
    validate_validation_plan(
        plan,
        agents=agents,
        issues=issues,
        policy=policy,
    )
    if (
        prepared["state"] != "VALIDATING"
        or prepared["deployment"]["phase"] != "prepared"
        or prepared["repository"] != git.repository
        or prepared["pr_number"] != git.pr_number
        or prepared["commit_sha"] != git.commit_sha
        or prepared["digests"]["validation_digest"] != plan["validation_digest"]
        or prepared["digests"]["invocation_contract_digest"]
        != plan["invocation_contract_digest"]
        or len(prepared["runtime_topology"]["agents"]) != 41
    ):
        raise ContractError("Prepared validation topology is not current")
    selected_ids = list(prepared["validation_authority_ids"])
    selected_by_id = {
        item.authority_id: item
        for item in authorities
        if item.authority_id in selected_ids
    }
    selected = [selected_by_id[authority_id] for authority_id in selected_ids]
    assigned = []
    authority_ids: list[str] = []
    if shard_id is not None:
        authority_ids = _assignment_authority_ids(
            prepared,
            field=assignment_field,
            shard_id=shard_id,
        )
        assigned = validate_shard_assignment(
            shard_id,
            authority_ids or [],
            selected,
        )
        expected = next(
            (
                item["authority_ids"]
                for item in prepared[assignment_field]
                if item["shard_id"] == shard_id
            ),
            None,
        )
        if expected != authority_ids:
            raise ContractError("Validation shard assignment differs from prepare")
    operator = local_azure_operator()
    profile = validation_runtime_profile(
        plan["project_name"],
        run_id=prepared["run_id"],
        base=RuntimeProfile.from_env("staging", "g30"),
    )
    capacity = _capacity_from_lifecycle(prepared)
    deployed = {
        item["authority_id"]: _deployed_runtime(item)
        for item in prepared["runtime_topology"]["agents"]
    }
    request_capacity, token_capacity = (
        _invoke_worker_capacity(capacity, assigned)
        if partition_invoke_capacity
        else (capacity.available_rpm, capacity.available_tpm)
    )
    scheduler = ValidationScheduler(
        capacity,
        WeightedTokenBucket(
            request_capacity=request_capacity,
            token_capacity=token_capacity,
        ),
        telemetry_lease=lambda: CrossProcessTelemetryLease(
            run_id=prepared["run_id"],
            capacity=capacity,
            fence=lambda: _assert_active_generation(prepared),
        ),
    )
    return {
        "git": git,
        "policy": policy,
        "agents": agents,
        "authorities": authorities,
        "assigned": assigned if shard_id is not None else selected,
        "plan": plan,
        "prepared": prepared,
        "operator": operator,
        "profile": profile,
        "deployed": deployed,
        "scheduler": scheduler,
        "paired_baselines": {
            item.canonical_agent: item.authority_id
            for item in authorities
            if item.authority_kind == "baseline"
        },
        "store": (
            ValidationShardStore(
                prepared=prepared,
                shard_id=int(shard_id),
                authority_ids=authority_ids or [],
            )
            if shard_id is not None
            else None
        ),
    }


def _deployment_context(
    shard_id: int | None = None,
    authority_ids: list[str] | None = None,
) -> dict[str, Any]:
    git = discover_local_git_context()
    active = _active_for_state("CREATING")
    if (
        active["repository"] != git.repository
        or active["pr_number"] != git.pr_number
        or active["commit_sha"] != git.commit_sha
    ):
        raise ContractError("Prepared deployment does not match the current head")
    desired = _load_desired_state(active)
    policy = load_validation_policy()
    agents, issues = load_catalogs()
    authorities = authority_specs(agents, issues)
    plan = prepare_bound_validation_plan(
        agents=agents,
        issues=issues,
        policy=policy,
        repository=git.repository,
        pr_number=git.pr_number,
        commit_sha=git.commit_sha,
        run_id=active["run_id"],
    )
    planned = list(
        plan_runtime_topology(
            authorities,
            run_suffix=active["run_id"].removeprefix("validation-"),
            policy=policy,
        )
    )
    if shard_id is not None:
        expected = next(
            (
                item["authority_ids"]
                for item in active["deployment_assignments"]
                if item["shard_id"] == shard_id
            ),
            None,
        )
        if expected != authority_ids:
            raise ContractError("Deployment shard assignment differs from desired state")
    operator = local_azure_operator()
    profile = validation_runtime_profile(
        plan["project_name"],
        run_id=active["run_id"],
        base=RuntimeProfile.from_env("staging", "g30"),
    )
    deployer = FoundryAuthorityDeployer(
        profile=profile,
        agent_catalog=agents,
        issue_catalog=issues,
        token_provider=operator.token_provider,
        project=_project_from_lifecycle(active),
        support_images=desired["support_images"],
    )
    return {
        "git": git,
        "active": active,
        "desired": desired,
        "policy": policy,
        "agents": agents,
        "authorities": authorities,
        "planned": planned,
        "operator": operator,
        "profile": profile,
        "deployer": deployer,
        "store": (
            ValidationDeploymentShardStore(
                prepared=active,
                shard_id=int(shard_id),
                authority_ids=authority_ids or [],
                desired_state_digest=desired["desired_state_digest"],
            )
            if shard_id is not None
            else None
        ),
    }


def _desired_state(
    *,
    plan: dict[str, Any],
    authorities: list[Any],
    planned: list[Any],
    deployer: FoundryAuthorityDeployer,
    support_images: dict[str, str],
    superseded_authority_ids: list[str],
    forced_invocation_authority_ids: list[str],
    quota_plan_digest: str,
) -> dict[str, Any]:
    registry = _read_deployment_registry()
    registry_by_id = (
        {
            item["authority_id"]: item
            for item in registry["authorities"]
        }
        if registry is not None
        else {}
    )
    desired_authorities = []
    reused_runtimes = []
    deployment_ids = []
    for authority, target in zip(authorities, planned, strict=True):
        provider_content_digest = deployer.desired_content_digest(authority)
        item = {
            "authority_id": authority.authority_id,
            "authority_kind": authority.authority_kind,
            "canonical_agent": authority.canonical_agent,
            "logical_version": authority.logical_version,
            "runtime_kind": authority.runtime_kind,
            "framework": authority.framework,
            "runtime_agent_name": target.runtime_agent_name,
            "source_content_digest": authority.source_content_digest,
            "provider_content_digest": provider_content_digest,
            "version_intent": content_hash(
                {
                    "runtime_agent_name": target.runtime_agent_name,
                    "logical_version": authority.logical_version,
                    "provider_content_digest": provider_content_digest,
                }
            ),
        }
        desired_authorities.append(item)
        previous = registry_by_id.get(authority.authority_id)
        if previous is not None and all(
            previous.get(field) == item[field]
            for field in (
                "authority_id",
                "runtime_kind",
                "framework",
                "runtime_agent_name",
                "source_content_digest",
                "provider_content_digest",
                "version_intent",
            )
        ):
            reused_runtimes.append(
                {
                    "authority_id": authority.authority_id,
                    "runtime": previous["runtime"],
                }
            )
            continue
        existing = deployer.find_existing(authority, target)
        if existing is not None:
            reused_runtimes.append(
                {
                    "authority_id": authority.authority_id,
                    "runtime": asdict(existing),
                }
            )
            continue
        deployment_ids.append(authority.authority_id)
    value = {
        "schema_version": "2.0.0",
        "kind": "test-agent-validation-desired-state",
        "run_id": plan["run_id"],
        "repository": plan["repository"],
        "pr_number": plan["pr_number"],
        "commit_sha": plan["commit_sha"],
        "environment_id": plan["environment_id"],
        "project_name": plan["project_name"],
        "validation_digest": plan["validation_digest"],
        "shared_validation_digest": plan["shared_validation_digest"],
        "invocation_contract_digest": plan["invocation_contract_digest"],
        "support_images": dict(sorted(support_images.items())),
        "authorities": desired_authorities,
        "deployment_authority_ids": deployment_ids,
        "deployment_assignments": _assignments(
            deployment_ids,
            quota_plan_digest=quota_plan_digest,
        ),
        "reused_runtimes": reused_runtimes,
        "forced_validation_authority_ids": list(
            dict.fromkeys(superseded_authority_ids)
        ),
        "forced_invocation_authority_ids": list(
            dict.fromkeys(forced_invocation_authority_ids)
        ),
        "desired_state_digest": "",
    }
    value["desired_state_digest"] = content_hash(
        {
            key: item
            for key, item in value.items()
            if key != "desired_state_digest"
        }
    )
    return value


def _load_desired_state(active: dict[str, Any]) -> dict[str, Any]:
    reference = active.get("desired_state_reference")
    if not isinstance(reference, dict):
        raise ContractError("Immutable validation desired state is missing")
    root = validation_runtime_root()
    path = (root / str(reference.get("path") or "")).resolve()
    if root.resolve() not in path.parents:
        raise ContractError("Validation desired state path escapes runtime root")
    value = read_json(path)
    digest = content_hash(
        {
            key: item
            for key, item in value.items()
            if key != "desired_state_digest"
        }
    )
    if (
        value.get("schema_version") != "2.0.0"
        or value.get("kind") != "test-agent-validation-desired-state"
        or value.get("run_id") != active["run_id"]
        or value.get("repository") != active["repository"]
        or value.get("pr_number") != active["pr_number"]
        or value.get("commit_sha") != active["commit_sha"]
        or value.get("desired_state_digest") != digest
        or reference.get("digest") != digest
        or value.get("deployment_assignments")
        != active["deployment_assignments"]
    ):
        raise ContractError("Immutable validation desired state binding is invalid")
    return value


def _read_deployment_registry() -> dict[str, Any] | None:
    path = validation_runtime_root() / "deployment-registry.json"
    if not path.is_file():
        return None
    try:
        value = read_json(path)
    except (ContractError, OSError):
        return None
    digest = content_hash(
        {
            key: item
            for key, item in value.items()
            if key != "registry_digest"
        }
    )
    if value.get("registry_digest") != digest:
        return None
    try:
        _validate_deployment_registry(value)
    except ContractError:
        return None
    return value


def _write_deployment_registry(
    active: dict[str, Any],
    *,
    desired: dict[str, Any],
    profile: RuntimeProfile,
) -> None:
    desired_by_id = {
        item["authority_id"]: item for item in desired["authorities"]
    }
    value = {
        "schema_version": "2.0.0",
        "kind": "test-agent-validation-deployment-registry",
        "environment_id": "swedencentral-g30",
        "project_name": active["project"]["name"],
        "authorities": [
            {
                **{
                    field: desired_by_id[item["authority_id"]][field]
                    for field in (
                        "authority_id",
                        "runtime_kind",
                        "framework",
                        "runtime_agent_name",
                        "source_content_digest",
                        "provider_content_digest",
                        "version_intent",
                    )
                },
                "runtime": item,
            }
            for item in active["runtime_topology"]["agents"]
        ],
        "registry_digest": "",
    }
    value["registry_digest"] = content_hash(
        {key: item for key, item in value.items() if key != "registry_digest"}
    )
    _validate_deployment_registry(value)
    root = validation_runtime_root()
    immutable_json(
        root
        / "deployment-registries"
        / active["run_id"]
        / f"{value['registry_digest'].removeprefix('sha256:')}.json",
        value,
    )
    canonical = root / "deployment-registry.json"
    atomic_json(canonical, value)
    publish_validation_registry(profile, canonical)


def _validate_deployment_registry(value: dict[str, Any]) -> None:
    schema = read_json(
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "test-agent-validation-deployment-registry.schema.json"
    )
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise ContractError(
            f"Validation deployment registry is invalid: {errors[0].message}"
        )
    authority_ids = [item["authority_id"] for item in value["authorities"]]
    if len(authority_ids) != len(set(authority_ids)):
        raise ContractError("Validation deployment registry authorities collide")


def _active_for_state(state: str) -> dict[str, Any]:
    lock = LocalValidationLock(validation_runtime_root() / "coordinator.lock")
    active = LifecycleJournal(lock=lock).read_active().value
    if active["state"] != state:
        raise ContractError(f"Validation lifecycle is not {state}")
    return active


def _matching_active(git: Any) -> dict[str, Any] | None:
    lock = LocalValidationLock(validation_runtime_root() / "coordinator.lock")
    try:
        active = LifecycleJournal(lock=lock).read_active().value
    except (ContractError, OSError):
        return None
    if (
        active["repository"] != git.repository
        or active["pr_number"] != git.pr_number
        or active["commit_sha"] != git.commit_sha
    ):
        return None
    return active


def _assignments(
    authority_ids: list[str],
    *,
    quota_plan_digest: str,
    maximum_shards: int = 8,
) -> list[dict[str, Any]]:
    shard_count = min(maximum_shards, len(authority_ids))
    if shard_count == 0:
        return []
    return [
        {
            "shard_id": index + 1,
            "authority_ids": authority_ids[index::shard_count],
            "quota_plan_digest": quota_plan_digest,
        }
        for index in range(shard_count)
    ]


def _assignment_authority_ids(
    active: Mapping[str, Any],
    *,
    field: str,
    shard_id: int,
) -> list[str]:
    if field not in {
        "deployment_assignments",
        "invocation_shard_assignments",
        "shard_assignments",
    }:
        raise ContractError("Validation assignment stage is invalid")
    matches = [
        item
        for item in active[field]
        if item["shard_id"] == shard_id
    ]
    if len(matches) != 1:
        raise ContractError("Validation shard is not assigned in the active generation")
    return list(matches[0]["authority_ids"])


def _forced_invocation_authority_ids(
    *,
    migration: Mapping[str, Any],
    supplemental: Mapping[str, Any],
    incomplete_current_invocations: list[str],
) -> list[str]:
    available = set(supplemental["imported_authority_ids"])
    return list(
        dict.fromkeys(
            [
                *migration["incomplete_authority_ids"],
                *[
                    item
                    for item in incomplete_current_invocations
                    if item not in available
                ],
                *supplemental["incomplete_authority_ids"],
            ]
        )
    )


def _assert_active_generation(prepared: Mapping[str, Any]) -> None:
    active = _active_for_state("VALIDATING")
    if (
        active["run_id"] != prepared["run_id"]
        or active["commit_sha"] != prepared["commit_sha"]
        or active["digests"]["validation_digest"]
        != prepared["digests"]["validation_digest"]
        or active["digests"]["quota_plan_digest"]
        != prepared["digests"]["quota_plan_digest"]
    ):
        raise ContractError("Stale telemetry verifier is fenced")


def _invocation_receipts_for_verification(
    context: Mapping[str, Any],
    authority_ids: list[str],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    prepared = context["prepared"]
    reused_by_id = {
        item["authority_id"]: item
        for item in prepared["reused_invocations"]
    }
    current_by_id: dict[str, dict[str, str]] = {}
    for assignment in prepared["invocation_shard_assignments"]:
        store = ValidationShardStore(
            prepared=prepared,
            shard_id=assignment["shard_id"],
            authority_ids=assignment["authority_ids"],
        )
        artifact = store.read_invocations()
        if artifact["status"] != "invoked":
            raise ContractError(
                "Validation invocation barrier is incomplete"
            )
        references = artifact["invocation_receipts"]
        if [item["authority_id"] for item in references] != sorted(
            assignment["authority_ids"]
        ):
            raise ContractError(
                "Finalized invocation shard receipt coverage is invalid"
            )
        for reference in references:
            if reference["authority_id"] in current_by_id:
                raise ContractError(
                    "Current invocation receipt coverage collides"
                )
            current_by_id[reference["authority_id"]] = reference
    if (
        set(current_by_id) != set(prepared["invocation_authority_ids"])
        or set(reused_by_id).intersection(current_by_id)
        or set(reused_by_id).union(current_by_id)
        != set(prepared["validation_authority_ids"])
    ):
        raise ContractError(
            "Lifecycle-selected invocation receipt coverage is invalid"
        )

    authority_by_id = {
        item.authority_id: item for item in context["authorities"]
    }
    runtime_by_id = {
        item["authority_id"]: item
        for item in prepared["runtime_topology"]["agents"]
    }
    bound_by_id: dict[str, tuple[dict[str, str], dict[str, Any]]] = {}
    for authority_id in prepared["validation_authority_ids"]:
        reference = current_by_id.get(authority_id) or reused_by_id.get(
            authority_id
        )
        if reference is None:
            raise ContractError("Selected invocation receipt is missing")
        authority = authority_by_id[authority_id]
        paired_id = context["paired_baselines"][authority.canonical_agent]
        receipt = load_bound_invocation_receipt(
            reference,
            authority=authority,
            paired_v0_authority=authority_by_id[paired_id],
            runtime=runtime_by_id[authority_id],
            paired_v0_runtime=runtime_by_id[paired_id],
            prepared=prepared,
            plan=context["plan"],
        )
        if (
            authority_id in current_by_id
            and (
                receipt["origin_run_id"] != prepared["run_id"]
                or receipt["origin_shard_id"]
                != next(
                    item["shard_id"]
                    for item in prepared["invocation_shard_assignments"]
                    if authority_id in item["authority_ids"]
                )
            )
        ):
            raise ContractError(
                "Current-generation invocation receipt origin is invalid"
            )
        bound_by_id[authority_id] = (reference, receipt)
    assert_invocation_receipt_set_isolated(
        [item[1] for item in bound_by_id.values()]
    )
    return (
        [bound_by_id[item][0] for item in authority_ids],
        [bound_by_id[item][1] for item in authority_ids],
    )


def _incomplete_current_invocations(
    *,
    journal: LifecycleJournal,
    plan: Mapping[str, Any],
    authorities: list[Any],
) -> list[str]:
    try:
        active = journal.read_active().value
    except (ContractError, OSError):
        return []
    if active["state"] != "VALIDATING":
        return []
    authority_ids = list(active["invocation_authority_ids"])
    if not authority_ids:
        return []
    try:
        _, references = select_reusable_invocation_receipts(
            authorities=authorities,
            authority_ids=authority_ids,
            runtime_topology=active["runtime_topology"],
            prepared=active,
            plan=plan,
        )
        completed = {
            reference["authority_id"]
            for reference in references
            if load_invocation_receipt(reference)["origin_run_id"]
            == active["run_id"]
        }
    except (ContractError, OSError, ValueError):
        return authority_ids
    return [
        authority_id
        for authority_id in authority_ids
        if authority_id not in completed
    ]


def _runner(
    context: dict[str, Any],
    *,
    record_resource: Any,
) -> FoundryScenarioAttemptRunner:
    return FoundryScenarioAttemptRunner(
        LiveRuntime(
            context["profile"],
            token_provider=context["operator"].token_provider,
            use_traffic_ledger=False,
        ),
        endpoint_costs={
            item.authority_id: validation_authority_cost(item)
            for item in context["authorities"]
        },
        stabilization_seconds=context[
            "policy"
        ].trace_hydration_stabilization_seconds,
        record_resource=record_resource,
    )


def _verifier(context: dict[str, Any]) -> FoundryScenarioVerifier:
    return FoundryScenarioVerifier(
        TelemetryOnlyRuntime(
            context["profile"],
            token_provider=context["operator"].token_provider,
        ),
        endpoint_costs={
            item.authority_id: validation_authority_cost(item)
            for item in context["authorities"]
        },
        stabilization_seconds=context[
            "policy"
        ].trace_hydration_stabilization_seconds,
    )


def _authority_targets(context: dict[str, Any], authority: Any) -> list[Any]:
    ids = {
        authority.authority_id,
        context["paired_baselines"][authority.canonical_agent],
    }
    return [context["deployed"][authority_id] for authority_id in sorted(ids)]


def _prepared_result(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "prepared",
        "commit_sha": value["commit_sha"],
        "authority_count": len(value["validation_authority_ids"]),
        "reused_authority_count": len(value["reused_authorities"]),
        "deployment_shards": value["deployment_assignments"],
        "invoke_shards": value["invocation_shard_assignments"],
        "verify_shards": value["shard_assignments"],
        "maximum_active_subsessions": 8,
        "invoke_shard_concurrency": 8,
        "verify_shard_concurrency": 8,
    }


def _assert_git(expected: Any) -> None:
    if discover_local_git_context() != expected:
        raise ContractError("Local commit or pull request changed during validation")


def _invoke_worker_capacity(
    capacity: CapacityPlan,
    assigned: list[Any],
) -> tuple[int, int]:
    request_capacity = capacity.available_rpm // INVOKE_SHARD_CONCURRENCY
    token_capacity = capacity.available_tpm // INVOKE_SHARD_CONCURRENCY
    costs = [validation_authority_cost(item) for item in assigned]
    if (
        not costs
        or request_capacity < max(item.requests for item in costs)
        or token_capacity < max(item.tokens for item in costs)
    ):
        raise ContractError(
            "Partitioned validation capacity cannot fit the shard endpoint cost"
        )
    return request_capacity, token_capacity
