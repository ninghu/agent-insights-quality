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
from agent_insights_quality.provisioning import (
    _support_build_context_digest,
    _support_build_context_digest_at_commit,
)
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.registry import publish_validation_registry
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    atomic_json,
    content_hash,
    immutable_json,
    read_json,
)
from agent_insights_quality.validation_credentials import local_azure_operator
from agent_insights_quality.validation_copilot import (
    CopilotClaimError,
    EVALUATION_PROMPT,
    MAX_ACTIVE_COPILOT_CLAIMS,
    active_copilot_claims,
    assessment_path,
    attach_private_package_to_active_pointer,
    authority_evidence_from_evaluation,
    complete_active_pointer,
    copilot_claimant_reference,
    evaluation_lock,
    incomplete_authority_evidence_from_invocation,
    incomplete_result_requires_fresh_invocation,
    load_active_pointer,
    load_claim_pointer,
    load_bound_private_package,
    load_copilot_evaluation,
    pointer_paths,
    release_active_pointer,
    write_active_pointer,
    write_private_package,
)
from agent_insights_quality.validation_assignments import verification_assignment
from agent_insights_quality.validation_authority_results import (
    current_authority_verification_results,
    load_authority_verification_result,
    load_bound_authority_verification_result,
    sanitize_verification_error,
    verification_query_diagnostics,
    write_authority_verification_result,
)
from agent_insights_quality.validation_cycle import (
    ValidationCycleController,
    initial_lifecycle,
)
from agent_insights_quality.validation_evidence import (
    load_reused_authority_evidence,
    persist_evidence,
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
    ValidationLockBusy,
    validation_runtime_root,
)
from agent_insights_quality.validation_leases import CrossProcessTelemetryLease
from agent_insights_quality.validation_invocations import (
    assert_invocation_receipt_set_isolated,
    extract_legacy_shard_invocations,
    load_bound_invocation_receipt,
    load_invocation_receipt,
    recover_supplemental_legacy_invocations,
    write_invocation_receipt,
)
from agent_insights_quality.validation_live import (
    FoundryScenarioAttemptRunner,
    FoundryScenarioVerifier,
    PostResponseTelemetryError,
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
)
from agent_insights_quality.validation_shards import (
    ValidationDeploymentShardStore,
    ValidationShardStore,
    authority_lock,
    import_shard_resources,
    shard_lock,
    validate_shard_assignment,
)
from agent_insights_quality.validation_verifier import (
    authority_verification_outcome,
)

INVOKE_SHARD_CONCURRENCY = 8


def prepare_test_agent_validation() -> dict[str, Any]:
    return _prepare_test_agent_validation()


def _prepare_test_agent_validation(
    *,
    recovery_source_digest: str | None = None,
    recovery_intent: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if recovery_source_digest is not None and recovery_intent is not None:
        raise ContractError("Validation recovery input is ambiguous")
    git = discover_local_git_context()
    policy = load_validation_policy()
    agents, issues = load_catalogs()
    authorities = authority_specs(agents, issues)
    support_agent = next(
        item
        for item in agents["agents"]
        if item["name"] == "support-ticket-agent"
    )
    operator = local_azure_operator()
    base_profile = RuntimeProfile.from_env("staging", "g30")
    lock = LocalValidationLock(validation_runtime_root() / "coordinator.lock")
    with lock:
        journal = LifecycleJournal(lock=lock)
        recovery_requirements: tuple[list[str], list[str]] | None = None
        if recovery_source_digest is not None:
            recovery_source = journal.read_active().value
            if recovery_source["journal_digest"] != recovery_source_digest:
                raise ContractError(
                    "Validation recovery source changed before successor preparation"
                )
            candidate = _validation_recovery_candidate(recovery_source, git=git)
            _assert_recovery_deployment_reuse(recovery_source)
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
        if recovery_source_digest is not None:
            recovery_requirements = _current_invocation_requirements(
                journal=journal,
                plan=plan,
                authorities=authorities,
            )
            recovery_intent = _recovery_intent(
                source=recovery_source,
                incomplete_authority_ids=candidate[
                    "incomplete_authority_ids"
                ],
                fresh_invocation_authority_ids=recovery_requirements[1],
            )
        elif recovery_intent is not None:
            _validate_resumable_recovery_intent(
                journal.read_active().value,
                recovery_intent=recovery_intent,
                git=git,
            )
            recovery_requirements = (
                list(recovery_intent["incomplete_authority_ids"]),
                list(recovery_intent["fresh_invocation_authority_ids"]),
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
            recovery_intent=recovery_intent,
        )
        support_reuse_candidates = _support_image_reuse_candidates(
            journal=journal,
            plan=plan,
            authorities=authorities,
            support_agent=support_agent,
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
                if recovery_requirements is None:
                    (
                        incomplete_current_invocations,
                        fresh_current_invocations,
                    ) = _current_invocation_requirements(
                        journal=journal,
                        plan=plan,
                        authorities=authorities,
                    )
                else:
                    (
                        incomplete_current_invocations,
                        fresh_current_invocations,
                    ) = recovery_requirements
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
            authority_by_id = {
                item.authority_id: item for item in authorities
            }
            reusable_images = {
                logical_version: deployer.resolve_support_image_reuse(
                    authority_by_id[candidate["authority_id"]],
                    candidate,
                )
                for logical_version, candidate in sorted(
                    support_reuse_candidates.items()
                )
            }
            images = prepare_validation_support_images(
                profile,
                support_agent,
                reusable_images=reusable_images,
            ).images
        migrated_authority_ids = {
            candidate["authority_id"]
            for logical_version, candidate in support_reuse_candidates.items()
            if logical_version in reusable_images
        }
        superseded_authority_ids = [
            authority_id
            for authority_id in superseded_authority_ids
            if authority_id not in migrated_authority_ids
        ]
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
                    fresh_current_invocations=fresh_current_invocations,
                ),
            ],
            quota_plan_digest=controller.active.value["digests"][
                "quota_plan_digest"
            ],
        )
        if recovery_intent is not None and desired[
            "deployment_assignments"
        ]:
            raise ContractError(
                "Validation recovery cannot expand into deployment work"
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
    history = _public_authority_history(active)
    history_fields = {
        "agent_count": len(
            {item["canonical_agent"] for item in history}
        ),
        "authority_count": len(history),
        "authority_history": history,
    }
    if active is not None and active["state"] in {"READY", "FAILED"}:
        evidence = read_json(
            validation_runtime_root() / active["evidence_reference"]["path"]
        )
        failed = next(
            (
                item
                for item in evidence["authorities"]
                if item["pass"] is not True
            ),
            None,
        )
        return {
            **history_fields,
            "status": active["state"].casefold(),
            "result": evidence["result"],
            "validated_authority_count": len(
                active["validation_authority_ids"]
            ),
            "reused_authority_count": len(active["reused_authorities"]),
            "evidence_digest": evidence["evidence_digest"],
            "first_failed_authority_id": (
                failed["authority_id"] if failed is not None else None
            ),
        }
    if active["state"] == "CREATING":
        return {
            **history_fields,
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
        incomplete_invoke_shards = _incomplete_invocation_shards(active)
        if incomplete_invoke_shards:
            return {
                **history_fields,
                "status": "invocation_pending",
                "maximum_active_subsessions": 8,
                "invoke_shards": incomplete_invoke_shards,
                "next_commands": [
                    "python -m agent_insights_quality "
                    f"invoke-test-agent-validation-shard --shard-id "
                    f"{item['shard_id']}"
                    for item in incomplete_invoke_shards
                ],
            }
        references = current_authority_verification_results(
            prepared=active,
            authority_ids=active["validation_authority_ids"],
        )
        completed = {
            authority_id: load_authority_verification_result(reference)
            for authority_id, reference in references.items()
        }
        pending = [
            item
            for item in active["verification_authority_assignments"]
            if item["authority_id"] not in completed
        ]
        if pending:
            pending_ids = {
                assignment["authority_id"] for assignment in pending
            }
            claims = [
                item
                for item in active_copilot_claims(prepared=active)
                if item["authority_id"] in pending_ids
            ]
            available_slots = min(
                MAX_ACTIVE_COPILOT_CLAIMS - len(claims),
                len(pending) - len(claims),
            )
            return {
                **history_fields,
                "status": "verification_pending",
                "maximum_active_subsessions": MAX_ACTIVE_COPILOT_CLAIMS,
                "completed_authority_count": len(completed),
                "pending_authority_count": len(pending),
                "active_authority_evaluator_count": len(claims),
                "available_authority_evaluator_slots": available_slots,
                "next_commands": (
                    [
                        "python -m agent_insights_quality "
                        "prepare-test-agent-validation-assessment"
                    ]
                    if available_slots
                    else []
                ),
            }
        incomplete = [
            completed[authority_id]
            for authority_id in active["validation_authority_ids"]
            if authority_id in completed
            and completed[authority_id]["outcome"] == "INCOMPLETE"
        ]
        if incomplete:
            first = incomplete[0]
            return {
                **history_fields,
                "status": "verification_incomplete",
                "maximum_active_subsessions": 8,
                "completed_authority_count": len(completed),
                "pending_authority_count": len(pending),
                "first_failed_authority_id": first["authority_id"],
                "first_failed_outcome": first["outcome"],
                "query_stage": first["query_stage"],
                "error_code": first["error_code"],
                "query_diagnostics": first["query_diagnostics"],
                "next_commands": [
                    "python -m agent_insights_quality recover-test-agent-validation"
                ],
            }
        failed = [
            completed[authority_id]
            for authority_id in active["validation_authority_ids"]
            if completed[authority_id]["outcome"] == "FAIL"
        ]
        return {
            **history_fields,
            "status": "composition_pending",
            "maximum_active_subsessions": 8,
            "completed_authority_count": len(completed),
            "first_failed_authority_id": (
                failed[0]["authority_id"] if failed else None
            ),
            "first_failed_outcome": failed[0]["outcome"] if failed else None,
            "query_stage": None,
            "error_code": None,
            "next_commands": [
                "python -m agent_insights_quality compose-test-agent-validation"
            ],
        }
    return {
        **history_fields,
        "status": active["state"].casefold(),
        "next_commands": [],
    }


def recover_test_agent_validation() -> dict[str, Any]:
    git = discover_local_git_context()
    source = _matching_active(git)
    if source is None:
        raise ContractError(
            "Test Agent Validation recovery requires the current prepared head"
        )
    resumed_intent = source.get("recovery_intent")
    if source["state"] == "CREATING" and isinstance(
        resumed_intent,
        Mapping,
    ):
        _validate_resumable_recovery_intent(
            source,
            recovery_intent=resumed_intent,
            git=git,
        )
        candidate = {
            "incomplete_authority_ids": list(
                resumed_intent["incomplete_authority_ids"]
            )
        }
        prepared = _prepare_test_agent_validation(
            recovery_intent=resumed_intent,
        )
    else:
        candidate = _validation_recovery_candidate(source, git=git)
        prepared = _prepare_test_agent_validation(
            recovery_source_digest=source["journal_digest"],
        )
    if prepared["deployment_shards"]:
        raise ContractError("Validation recovery unexpectedly selected deployment work")
    reconcile_test_agent_validation_deployment()
    successor = _active_for_state("VALIDATING")
    journal = LifecycleJournal(
        lock=LocalValidationLock(validation_runtime_root() / "coordinator.lock")
    )
    _validate_recovery_successor(
        source=source,
        successor=successor,
        incomplete_authority_ids=candidate["incomplete_authority_ids"],
        ancestor_run_ids=journal.superseded_run_ids(successor),
        source_authority_ids=[
            item.authority_id for item in authority_specs(*load_catalogs())
        ],
    )
    result = _prepared_result(successor)
    invoke_shards = result["invoke_shards"]
    result.update(
        {
            "status": (
                "recovery_invocation_pending"
                if invoke_shards
                else "recovery_verification_pending"
            ),
            "recovery_authority_count": len(
                candidate["incomplete_authority_ids"]
            ),
            "next_commands": (
                [
                    "python -m agent_insights_quality "
                    f"invoke-test-agent-validation-shard --shard-id "
                    f"{item['shard_id']}"
                    for item in invoke_shards
                ]
                if invoke_shards
                else [
                    "python -m agent_insights_quality "
                    "prepare-test-agent-validation-assessment"
                ]
            ),
        }
    )
    return result


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
        recovery_intent = controller.active.value["recovery_intent"]
        authority_results = (
            _recovery_authority_result_selection(
                prepared=controller.active.value,
                plan=contexts["plan"],
                authorities=authorities,
                recovery_intent=recovery_intent,
            )
            if recovery_intent is not None
            else {authority.authority_id: None for authority in authorities}
        )
        selected, reused = _merge_authority_result_selection(
            authorities=authorities,
            selected=[],
            reused=[],
            authority_results=authority_results,
            forced=set(desired["forced_validation_authority_ids"]),
        )
        invocation_selected, reused_invocations = (
            _recovery_invocation_selection(
                prepared=controller.active.value,
                plan=contexts["plan"],
                authorities=authorities,
                selected_authority_ids=selected,
                forced_authority_ids=set(
                    desired["forced_invocation_authority_ids"]
                ),
                recovery_intent=recovery_intent,
            )
            if recovery_intent is not None
            else (selected, [])
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


def prepare_test_agent_validation_assessment() -> dict[str, Any]:
    active = _active_for_state("VALIDATING")
    if _incomplete_invocation_shards(active):
        raise ContractError("Validation invocation barrier is incomplete")
    context = _load_prepared()
    prepared = context["prepared"]
    claimant = copilot_claimant_reference()
    try:
        with evaluation_lock():
            claimed = _claim_test_agent_validation_assessment(
                prepared=prepared,
                claimant=claimant,
            )
    except ValidationLockBusy:
        return _assessment_busy_result(
            command="prepare-test-agent-validation-assessment"
        )
    if isinstance(claimed, dict):
        return claimed
    pointer, authority_id, pending_count, active_count = claimed
    authority_by_id = {
        item.authority_id: item for item in context["authorities"]
    }
    runtime_by_id = {
        item["authority_id"]: item
        for item in prepared["runtime_topology"]["agents"]
    }
    authority = authority_by_id[authority_id]
    paired_id = context["paired_baselines"][authority.canonical_agent]
    references, receipts = _invocation_receipts_for_verification(
        context,
        [authority_id],
    )
    if pointer["claim_state"] == "ready":
        package = load_bound_private_package(
            pointer,
            prepared=prepared,
            plan=context["plan"],
            authority=authority,
            runtime=runtime_by_id[authority_id],
            paired_v0_runtime=runtime_by_id[paired_id],
            invocation_reference=references[0],
            invocation_receipt=receipts[0],
        )
        package_path, draft_path = pointer_paths(pointer)
        if draft_path != assessment_path(package["package_hash"]):
            raise ContractError(
                "Active Copilot assessment path binding is stale"
            )
        return _assessment_ready_result(
            package_path=package_path,
            draft_path=draft_path,
            pending_count=pending_count,
            active_count=active_count,
        )
    started_at = datetime.fromisoformat(str(pointer["claimed_at"]))
    try:
        package_record = write_private_package(
            prepared=prepared,
            plan=context["plan"],
            authority=authority,
            runtime=runtime_by_id[authority_id],
            paired_v0_runtime=runtime_by_id[paired_id],
            deployed=context["deployed"][authority_id],
            paired_v0_deployed=context["deployed"][paired_id],
            invocation_reference=references[0],
            invocation_receipt=receipts[0],
            collector=_verifier(context),
            scheduler=context["scheduler"],
            started_at=started_at,
            fence=lambda: _assert_active_assessment_claim(
                prepared,
                pointer,
            ),
        )
    except PostResponseTelemetryError as error:
        query_stage, error_code = sanitize_verification_error(error)
        query_diagnostics = verification_query_diagnostics(error)
        evidence = incomplete_authority_evidence_from_invocation(
            authority=authority,
            runtime=runtime_by_id[authority_id],
            paired_v0_runtime=runtime_by_id[paired_id],
            invocation=receipts[0]["invocation"],
            validated_commit_sha=prepared["commit_sha"],
            error_code=error_code,
        )
        completed_at = datetime.now(UTC)
        with evaluation_lock():
            _assert_active_generation(prepared)
            current_pointer = load_active_pointer(
                claimant_reference=claimant,
                require_ready=False,
            )
            _assert_assessment_claim_binding(
                current_pointer,
                prepared=prepared,
                claimant_reference=claimant,
                authority_id=authority_id,
            )
            _assert_no_completed_authority_result(
                prepared=prepared,
                authority_id=authority_id,
            )
            reference = write_authority_verification_result(
                prepared=prepared,
                plan=context["plan"],
                authority=authority,
                runtime=runtime_by_id[authority_id],
                paired_v0_authority=authority_by_id[paired_id],
                paired_v0_runtime=runtime_by_id[paired_id],
                invocation_reference=references[0],
                authority_evidence=evidence,
                outcome="INCOMPLETE",
                started_at=started_at,
                completed_at=completed_at,
                query_stage=query_stage,
                error_code=error_code,
                query_diagnostics=query_diagnostics,
                fence=lambda: _assert_active_generation(prepared),
            )
            complete_active_pointer(
                current_pointer,
                completed_at=completed_at,
            )
        return _copilot_authority_verification_result(
            load_authority_verification_result(reference)
        )
    except (CopilotClaimError, ContractError, OSError, RuntimeError):
        _release_assessment_claim_after_failure(
            prepared=prepared,
            pointer=pointer,
        )
        raise
    with evaluation_lock():
        _assert_active_generation(prepared)
        current_pointer = load_active_pointer(
            claimant_reference=claimant,
            require_ready=False,
        )
        _assert_assessment_claim_binding(
            current_pointer,
            prepared=prepared,
            claimant_reference=claimant,
            authority_id=authority_id,
        )
        _assert_no_completed_authority_result(
            prepared=prepared,
            authority_id=authority_id,
        )
        pointer = attach_private_package_to_active_pointer(
            current_pointer,
            package_record,
        )
    package_path, draft_path = pointer_paths(pointer)
    return _assessment_ready_result(
        package_path=package_path,
        draft_path=draft_path,
        pending_count=pending_count,
        active_count=active_count,
    )


def release_test_agent_validation_assessment() -> dict[str, Any]:
    active = _active_for_state("VALIDATING")
    if _incomplete_invocation_shards(active):
        raise ContractError("Validation invocation barrier is incomplete")
    context = _load_prepared()
    prepared = context["prepared"]
    claimant = copilot_claimant_reference()
    try:
        with evaluation_lock():
            _assert_active_generation(prepared)
            try:
                pointer = load_claim_pointer(claimant_reference=claimant)
            except FileNotFoundError as error:
                raise ContractError(
                    "This worktree has no Copilot assessment claim to release"
                ) from error
            authority_id = str(pointer["authority_id"])
            _assert_assessment_claim_binding(
                pointer,
                prepared=prepared,
                claimant_reference=claimant,
                authority_id=authority_id,
            )
            current = current_authority_verification_results(
                prepared=prepared,
                authority_ids=[authority_id],
            ).get(authority_id)
            if current is not None or pointer["claim_state"] == "completed":
                raise ContractError(
                    "Completed Copilot assessment result cannot be released"
                )
            release_active_pointer(pointer)
            remaining = len(prepared["verification_authority_assignments"]) - len(
                current_authority_verification_results(
                    prepared=prepared,
                    authority_ids=prepared["validation_authority_ids"],
                )
            )
    except ValidationLockBusy:
        return _assessment_busy_result(
            command="release-test-agent-validation-assessment"
        )
    return {
        "status": "assessment_released",
        "pending_authority_count": remaining,
        "next_command": (
            "python -m agent_insights_quality "
            "prepare-test-agent-validation-assessment"
        ),
    }


def import_test_agent_validation_assessment() -> dict[str, Any]:
    active = _active_for_state("VALIDATING")
    if _incomplete_invocation_shards(active):
        raise ContractError("Validation invocation barrier is incomplete")
    context = _load_prepared()
    prepared = context["prepared"]
    claimant = copilot_claimant_reference()
    with evaluation_lock():
        _assert_active_generation(prepared)
        try:
            pointer = load_active_pointer(claimant_reference=claimant)
        except FileNotFoundError as error:
            raise ContractError(
                "This worktree has no active Copilot assessment claim"
            ) from error
        if (
            pointer["origin_run_id"] != prepared["run_id"]
            or pointer["origin_commit_sha"] != prepared["commit_sha"]
        ):
            raise ContractError("Stale Copilot assessment session is fenced")
        authority_id = str(pointer["authority_id"])
        expected_assignment = next(
            (
                item
                for item in prepared["verification_authority_assignments"]
                if item["authority_id"] == authority_id
            ),
            None,
        )
        if (
            expected_assignment is None
            or expected_assignment
            != verification_assignment(prepared, authority_id)
            or pointer["assignment_digest"]
            != expected_assignment["assignment_digest"]
        ):
            raise ContractError(
                "Copilot assessment authority is not currently assigned"
            )
        current = current_authority_verification_results(
            prepared=prepared,
            authority_ids=[authority_id],
        ).get(authority_id)
        if current is not None:
            result = _copilot_authority_verification_result(
                load_authority_verification_result(current)
            )
            complete_active_pointer(pointer)
            return result
        authority_by_id = {
            item.authority_id: item for item in context["authorities"]
        }
        runtime_by_id = {
            item["authority_id"]: item
            for item in prepared["runtime_topology"]["agents"]
        }
        authority = authority_by_id[authority_id]
        paired_id = context["paired_baselines"][authority.canonical_agent]
        references, receipts = _invocation_receipts_for_verification(
            context,
            [authority_id],
        )
        package = load_bound_private_package(
            pointer,
            prepared=prepared,
            plan=context["plan"],
            authority=authority,
            runtime=runtime_by_id[authority_id],
            paired_v0_runtime=runtime_by_id[paired_id],
            invocation_reference=references[0],
            invocation_receipt=receipts[0],
        )
        _, draft_path = pointer_paths(pointer)
        if draft_path != assessment_path(package["package_hash"]):
            raise ContractError("Active Copilot assessment path binding is stale")
        evaluation, evaluation_reference = load_copilot_evaluation(
            draft_path,
            package=package,
        )
        evidence = authority_evidence_from_evaluation(
            package=package,
            evaluation=evaluation,
            authority=authority,
            runtime=runtime_by_id[authority_id],
            validated_commit_sha=prepared["commit_sha"],
        )
        outcome, query_stage, error_code = authority_verification_outcome(
            evidence
        )
        reference = write_authority_verification_result(
            prepared=prepared,
            plan=context["plan"],
            authority=authority,
            runtime=runtime_by_id[authority_id],
            paired_v0_authority=authority_by_id[paired_id],
            paired_v0_runtime=runtime_by_id[paired_id],
            invocation_reference=references[0],
            authority_evidence=evidence,
            outcome=outcome,
            started_at=datetime.fromisoformat(package["created_at"]),
            completed_at=datetime.now(UTC),
            query_stage=query_stage,
            error_code=error_code,
            query_diagnostics=None,
            fence=lambda: _assert_active_generation(prepared),
            copilot_evaluation=evaluation_reference,
        )
        result = _copilot_authority_verification_result(
            load_authority_verification_result(reference)
        )
        complete_active_pointer(pointer)
        return result


def compose_test_agent_validation() -> dict[str, Any]:
    context = _load_prepared()
    prepared = context["prepared"]
    authority_by_id = {
        item.authority_id: item for item in context["authorities"]
    }
    runtime_by_id = {
        item["authority_id"]: item
        for item in prepared["runtime_topology"]["agents"]
    }
    current_references = current_authority_verification_results(
        prepared=prepared,
        authority_ids=prepared["validation_authority_ids"],
    )
    missing = [
        authority_id
        for authority_id in prepared["validation_authority_ids"]
        if authority_id not in current_references
    ]
    if missing:
        return {
            "status": "verification_pending",
            "completed_authority_count": len(current_references),
            "pending_authority_count": len(missing),
            "next_authority_id": missing[0],
        }
    current_results = []
    invocation_receipts: dict[str, dict[str, Any]] = {}
    for authority_id in prepared["validation_authority_ids"]:
        authority = authority_by_id[authority_id]
        paired_id = context["paired_baselines"][authority.canonical_agent]
        result = load_bound_authority_verification_result(
            current_references[authority_id],
            authority=authority,
            paired_v0_authority=authority_by_id[paired_id],
            runtime=runtime_by_id[authority_id],
            paired_v0_runtime=runtime_by_id[paired_id],
            prepared=prepared,
            plan=context["plan"],
            require_current_generation=True,
        )
        if result["outcome"] == "INCOMPLETE":
            return {
                "status": "verification_incomplete",
                "completed_authority_count": len(current_results),
                "pending_authority_count": 0,
                "first_failed_authority_id": authority_id,
                "first_failed_outcome": result["outcome"],
                "query_stage": result["query_stage"],
                "error_code": result["error_code"],
                "query_diagnostics": result["query_diagnostics"],
            }
        fresh_evidence = result["authority_evidence"]
        if not isinstance(fresh_evidence, dict):
            raise ContractError("Completed authority result lacks evidence")
        current_results.append(fresh_evidence)
        receipt = load_invocation_receipt(result["invocation_receipt"])
        if receipt["authority_id"] in invocation_receipts:
            raise ContractError("Validation invocation receipt coverage collides")
        invocation_receipts[receipt["authority_id"]] = receipt
    fresh = current_results
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
                "schema_version": "3.0.0",
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
            "first_failed_authority_id": next(
                (
                    item["authority_id"]
                    for item in authority_evidence
                    if item["pass"] is not True
                ),
                None,
            ),
        }


def _load_prepared(
    shard_id: int | None = None,
    *,
    assignment_field: str = "invocation_shard_assignments",
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
        "plan": plan,
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


def _support_image_reuse_candidates(
    *,
    journal: LifecycleJournal,
    plan: Mapping[str, Any],
    authorities: list[Any],
    support_agent: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    previous = journal.read_optional()
    registry = _read_deployment_registry()
    if (
        previous is None
        or previous.value.get("desired_state_reference") is None
        or registry is None
    ):
        return {}
    active = previous.value
    desired = _load_desired_state(active)
    if (
        active["repository"] != plan["repository"]
        or active["pr_number"] != plan["pr_number"]
        or active["project"]["name"] != plan["project_name"]
        or desired.get("environment_id") != plan["environment_id"]
        or desired.get("project_name") != plan["project_name"]
        or registry.get("environment_id") != plan["environment_id"]
        or registry.get("project_name") != plan["project_name"]
    ):
        return {}
    desired_authorities = desired.get("authorities")
    if not isinstance(desired_authorities, list):
        raise ContractError("Retained Support image authority bindings are invalid")
    desired_by_id = {
        item["authority_id"]: item
        for item in desired_authorities
        if isinstance(item, Mapping) and isinstance(item.get("authority_id"), str)
    }
    registry_by_id = {
        item["authority_id"]: item for item in registry["authorities"]
    }
    if (
        len(desired_by_id) != len(desired_authorities)
        or len(registry_by_id) != len(registry["authorities"])
    ):
        raise ContractError("Retained Support image authorities collide")
    active_agents = active["runtime_topology"]["agents"]
    active_runtime_by_id = {
        item["authority_id"]: item
        for item in active_agents
        if isinstance(item, Mapping) and isinstance(item.get("authority_id"), str)
    }
    if len(active_runtime_by_id) != len(active_agents):
        raise ContractError("Retained Support runtime authorities collide")
    support_root = ROOT / "agents" / str(support_agent["name"])
    candidates = {}
    for authority in authorities:
        if authority.canonical_agent != support_agent["name"]:
            continue
        desired_authority = desired_by_id.get(authority.authority_id)
        registry_authority = registry_by_id.get(authority.authority_id)
        if desired_authority is None or registry_authority is None:
            continue
        # Desired state proves the source commit; the canonical registry proves
        # the deployed provider binding, including after an interrupted prepare.
        current_context_digest = _support_build_context_digest(
            support_root,
            authority.logical_version,
        )
        retained_context_digest = _support_build_context_digest_at_commit(
            support_root,
            authority.logical_version,
            str(desired["commit_sha"]),
        )
        desired_binding = {
            "authority_id": authority.authority_id,
            "authority_kind": authority.authority_kind,
            "canonical_agent": authority.canonical_agent,
            "logical_version": authority.logical_version,
            "runtime_kind": authority.runtime_kind,
            "framework": authority.framework,
            "source_content_digest": authority.source_content_digest,
        }
        registry_fields = (
            "authority_id",
            "runtime_kind",
            "framework",
            "runtime_agent_name",
            "source_content_digest",
        )
        runtime = registry_authority.get("runtime")
        active_runtime = active_runtime_by_id.get(authority.authority_id)
        runtime_fields = (
            "authority_id",
            "runtime_kind",
            "runtime_agent_name",
            "runtime_agent_version",
            "provider_agent_id",
            "provider_agent_version_id",
            "provider_content_digest",
            "hosted_identity_id",
            "hosted_blueprint_id",
            "hosted_deployment_id",
            "runtime_principal_id",
        )
        if (
            retained_context_digest != current_context_digest
            or any(
                desired_authority.get(field) != value
                for field, value in desired_binding.items()
            )
            or any(
                registry_authority.get(field) != desired_authority.get(field)
                for field in registry_fields
            )
            or not isinstance(runtime, Mapping)
            or runtime.get("authority_id") != authority.authority_id
            or runtime.get("runtime_kind") != authority.runtime_kind
            or runtime.get("runtime_agent_name")
            != registry_authority["runtime_agent_name"]
            or runtime.get("provider_content_digest")
            != registry_authority["provider_content_digest"]
            or registry_authority.get("version_intent")
            != content_hash(
                {
                    "runtime_agent_name": registry_authority[
                        "runtime_agent_name"
                    ],
                    "logical_version": authority.logical_version,
                    "provider_content_digest": registry_authority[
                        "provider_content_digest"
                    ],
                }
            )
            or active_runtime is not None
            and any(
                active_runtime.get(field) != runtime.get(field)
                for field in runtime_fields
            )
        ):
            continue
        candidates[authority.logical_version] = {
            "authority_id": authority.authority_id,
            "build_context_digest": current_context_digest,
            "registry_authority": registry_authority,
        }
    return candidates


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


def _validation_recovery_candidate(
    active: Mapping[str, Any],
    *,
    git: Any,
) -> dict[str, Any]:
    if (
        active.get("state") != "VALIDATING"
        or active.get("repository") != git.repository
        or active.get("pr_number") != git.pr_number
        or active.get("commit_sha") != git.commit_sha
    ):
        raise ContractError(
            "Test Agent Validation recovery requires the current validating head"
        )
    if _incomplete_invocation_shards(active):
        raise ContractError(
            "Test Agent Validation recovery requires a complete invocation barrier"
        )
    authority_ids = list(active["validation_authority_ids"])
    references = current_authority_verification_results(
        prepared=active,
        authority_ids=authority_ids,
    )
    if set(references) != set(authority_ids):
        raise ContractError(
            "Test Agent Validation recovery requires all selected evaluations"
        )
    incomplete = [
        authority_id
        for authority_id in authority_ids
        if load_authority_verification_result(references[authority_id])["outcome"]
        == "INCOMPLETE"
    ]
    if not incomplete:
        raise ContractError(
            "Test Agent Validation recovery requires an INCOMPLETE authority"
        )
    return {"incomplete_authority_ids": incomplete}


def _recovery_intent(
    *,
    source: Mapping[str, Any],
    incomplete_authority_ids: list[str],
    fresh_invocation_authority_ids: list[str],
) -> dict[str, Any]:
    value = {
        "source_run_id": source["run_id"],
        "source_journal_digest": source["journal_digest"],
        "source_authority_results": _source_authority_result_references(source),
        "source_invocation_receipts": _source_invocation_receipt_references(source),
        "incomplete_authority_ids": list(incomplete_authority_ids),
        "fresh_invocation_authority_ids": list(
            fresh_invocation_authority_ids
        ),
        "intent_digest": "",
    }
    value["intent_digest"] = content_hash(
        {key: item for key, item in value.items() if key != "intent_digest"}
    )
    return value


def _source_authority_result_references(
    source: Mapping[str, Any],
) -> list[dict[str, str]]:
    by_id = {
        item["authority_id"]: dict(item)
        for item in source["reused_authorities"]
        if "authority_result_digest" in item
    }
    by_id.update(
        current_authority_verification_results(
            prepared=source,
            authority_ids=source["validation_authority_ids"],
        )
    )
    expected = {
        item["authority_id"] for item in source["runtime_topology"]["agents"]
    }
    if set(by_id) != expected:
        raise ContractError(
            "Validation recovery source result coverage is incomplete"
        )
    return [by_id[authority_id] for authority_id in sorted(by_id)]


def _source_invocation_receipt_references(
    source: Mapping[str, Any],
) -> list[dict[str, str]]:
    by_id = {
        item["authority_id"]: dict(item)
        for item in source["reused_invocations"]
    }
    for assignment in source["invocation_shard_assignments"]:
        artifact = ValidationShardStore(
            prepared=source,
            shard_id=assignment["shard_id"],
            authority_ids=assignment["authority_ids"],
        ).read_invocations()
        if artifact["status"] != "invoked":
            raise ContractError(
                "Validation recovery source invocation coverage is incomplete"
            )
        for reference in artifact["invocation_receipts"]:
            if reference["authority_id"] in by_id:
                raise ContractError(
                    "Validation recovery source invocation coverage collides"
                )
            by_id[reference["authority_id"]] = dict(reference)
    expected = set(source["validation_authority_ids"])
    if set(by_id) != expected:
        raise ContractError(
            "Validation recovery source invocation coverage is incomplete"
        )
    return [by_id[authority_id] for authority_id in sorted(by_id)]


def _validate_resumable_recovery_intent(
    active: Mapping[str, Any],
    *,
    recovery_intent: Mapping[str, Any],
    git: Any,
) -> None:
    if (
        active.get("state") != "CREATING"
        or active.get("repository") != git.repository
        or active.get("pr_number") != git.pr_number
        or active.get("commit_sha") != git.commit_sha
        or active.get("supersedes")
        != recovery_intent.get("source_journal_digest")
        or active.get("recovery_intent") != recovery_intent
        or recovery_intent.get("intent_digest")
        != content_hash(
            {
                key: item
                for key, item in recovery_intent.items()
                if key != "intent_digest"
            }
        )
        or not set(
            recovery_intent.get("fresh_invocation_authority_ids") or []
        ).issubset(
            set(recovery_intent.get("incomplete_authority_ids") or [])
        )
    ):
        raise ContractError("Validation recovery intent is stale")


def _assert_recovery_deployment_reuse(active: Mapping[str, Any]) -> None:
    desired = _load_desired_state(dict(active))
    registry = _read_deployment_registry()
    if registry is None:
        raise ContractError(
            "Validation recovery requires the exact deployment registry"
        )
    desired_by_id = {
        item["authority_id"]: item for item in desired["authorities"]
    }
    registry_by_id = {
        item["authority_id"]: item for item in registry["authorities"]
    }
    runtime_by_id = {
        item["authority_id"]: item for item in active["runtime_topology"]["agents"]
    }
    fields = (
        "authority_id",
        "runtime_kind",
        "framework",
        "runtime_agent_name",
        "source_content_digest",
        "provider_content_digest",
        "version_intent",
    )
    if (
        registry["environment_id"] != desired["environment_id"]
        or registry["project_name"] != active["project"]["name"]
        or set(desired_by_id) != set(runtime_by_id)
        or set(registry_by_id) != set(runtime_by_id)
        or any(
            any(
                registry_by_id[authority_id].get(field)
                != desired_by_id[authority_id].get(field)
                for field in fields
            )
            or registry_by_id[authority_id].get("runtime")
            != runtime_by_id[authority_id]
            for authority_id in runtime_by_id
        )
    ):
        raise ContractError(
            "Validation recovery deployment registry binding is not exact"
        )


def _validate_recovery_successor(
    *,
    source: Mapping[str, Any],
    successor: Mapping[str, Any],
    incomplete_authority_ids: list[str],
    ancestor_run_ids: list[str],
    source_authority_ids: list[str] | None = None,
) -> None:
    selected = list(successor["validation_authority_ids"])
    reused = [item["authority_id"] for item in successor["reused_authorities"]]
    invoked = list(successor["invocation_authority_ids"])
    source_authorities = source_authority_ids or [
        item["authority_id"] for item in source["runtime_topology"]["agents"]
    ]
    if (
        ancestor_run_ids[:1] != [source["run_id"]]
        or successor["repository"] != source["repository"]
        or successor["pr_number"] != source["pr_number"]
        or successor["commit_sha"] != source["commit_sha"]
        or successor["deployment_assignments"]
        or selected != incomplete_authority_ids
        or set(reused).intersection(selected)
        or set(reused).union(selected) != set(source_authorities)
        or not set(invoked).issubset(set(selected))
        or len(successor["invocation_shard_assignments"]) > 8
        or len(successor["verification_authority_assignments"]) != len(selected)
    ):
        raise ContractError(
            "Automatic validation recovery successor selection is invalid"
        )


def _forced_invocation_authority_ids(
    *,
    migration: Mapping[str, Any],
    supplemental: Mapping[str, Any],
    incomplete_current_invocations: list[str],
    fresh_current_invocations: list[str],
) -> list[str]:
    del migration, supplemental, incomplete_current_invocations
    return list(dict.fromkeys(fresh_current_invocations))


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


def _assert_assessment_claim_binding(
    pointer: Mapping[str, Any],
    *,
    prepared: Mapping[str, Any],
    claimant_reference: str,
    authority_id: str,
) -> None:
    expected = verification_assignment(prepared, authority_id)
    assignment = next(
        (
            item
            for item in prepared["verification_authority_assignments"]
            if item["authority_id"] == authority_id
        ),
        None,
    )
    if (
        pointer["claimant_reference"] != claimant_reference
        or pointer["origin_run_id"] != prepared["run_id"]
        or pointer["origin_commit_sha"] != prepared["commit_sha"]
        or pointer["authority_id"] != authority_id
        or pointer["assignment_digest"] != expected["assignment_digest"]
        or assignment != expected
    ):
        raise ContractError("Copilot assessment claim binding is stale")


def _assert_active_assessment_claim(
    prepared: Mapping[str, Any],
    pointer: Mapping[str, Any],
) -> None:
    claimant = str(pointer["claimant_reference"])
    authority_id = str(pointer["authority_id"])
    try:
        with evaluation_lock():
            _assert_active_generation(prepared)
            current = load_active_pointer(
                claimant_reference=claimant,
                require_ready=False,
            )
            _assert_assessment_claim_binding(
                current,
                prepared=prepared,
                claimant_reference=claimant,
                authority_id=authority_id,
            )
            _assert_no_completed_authority_result(
                prepared=prepared,
                authority_id=authority_id,
            )
    except (ContractError, OSError) as error:
        raise CopilotClaimError(
            "Copilot assessment claim is no longer active"
        ) from error


def _assert_no_completed_authority_result(
    *,
    prepared: Mapping[str, Any],
    authority_id: str,
) -> None:
    if current_authority_verification_results(
        prepared=prepared,
        authority_ids=[authority_id],
    ).get(authority_id) is not None:
        raise ContractError("Copilot assessment authority already has a result")


def _release_assessment_claim_after_failure(
    *,
    prepared: Mapping[str, Any],
    pointer: Mapping[str, Any],
) -> None:
    claimant = str(pointer["claimant_reference"])
    try:
        with evaluation_lock():
            active = _active_for_state("VALIDATING")
            if (
                active["state"] != "VALIDATING"
                or active["run_id"] != prepared["run_id"]
                or active["commit_sha"] != prepared["commit_sha"]
            ):
                return
            try:
                current = load_claim_pointer(claimant_reference=claimant)
            except FileNotFoundError:
                return
            if current["pointer_digest"] != pointer["pointer_digest"]:
                return
            _assert_assessment_claim_binding(
                current,
                prepared=prepared,
                claimant_reference=claimant,
                authority_id=str(pointer["authority_id"]),
            )
            if current["claim_state"] in {"completed", "released"}:
                return
            _assert_no_completed_authority_result(
                prepared=prepared,
                authority_id=str(pointer["authority_id"]),
            )
            release_active_pointer(current)
    except ValidationLockBusy as error:
        raise CopilotClaimError(
            "Copilot assessment claim release is temporarily busy"
        ) from error


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


def _current_invocation_requirements(
    *,
    journal: LifecycleJournal,
    plan: Mapping[str, Any],
    authorities: list[Any],
) -> tuple[list[str], list[str]]:
    try:
        active = journal.read_active().value
    except (ContractError, OSError):
        return [], []
    if (
        active["state"] != "VALIDATING"
        or active["repository"] != plan["repository"]
        or active["pr_number"] != plan["pr_number"]
    ):
        return [], []
    authority_ids = list(active["invocation_authority_ids"])
    try:
        completed = {
            reference["authority_id"]
            for reference in _source_invocation_receipt_references(active)
            if reference["authority_id"] in authority_ids
        }
    except (ContractError, OSError, ValueError):
        completed = set()
    forced = {
        authority_id
        for authority_id in authority_ids
        if authority_id not in completed
    }
    fresh_required: set[str] = set()
    result_references = current_authority_verification_results(
        prepared=active,
        authority_ids=active["validation_authority_ids"],
    )
    for authority_id, reference in result_references.items():
        result = load_authority_verification_result(reference)
        receipt = (
            load_invocation_receipt(result["invocation_receipt"])
            if result["outcome"] == "INCOMPLETE"
            and result.get("authority_evidence") is None
            else None
        )
        if incomplete_result_requires_fresh_invocation(
            result,
            invocation=(
                receipt["invocation"] if receipt is not None else None
            ),
        ) or (
            result["outcome"] == "INCOMPLETE"
            and _recovery_source_has_same_nonpass(active, result)
        ):
            forced.add(authority_id)
            fresh_required.add(authority_id)
    ordered_forced = [
        authority.authority_id
        for authority in authorities
        if authority.authority_id in forced
    ]
    return ordered_forced, [
        authority_id
        for authority_id in ordered_forced
        if authority_id in fresh_required
    ]


def _recovery_source_has_same_nonpass(
    active: Mapping[str, Any],
    result: Mapping[str, Any],
) -> bool:
    recovery_intent = active.get("recovery_intent")
    if not isinstance(recovery_intent, Mapping):
        return False
    reference = next(
        (
            item
            for item in recovery_intent["source_authority_results"]
            if item["authority_id"] == result["authority_id"]
        ),
        None,
    )
    if reference is None:
        return False
    prior = load_authority_verification_result(reference)
    return (
        prior["outcome"] in {"FAIL", "INCOMPLETE"}
        and prior["artifact_digest"] != result["artifact_digest"]
        and prior["binding"]["invocation_receipt_digest"]
        == result["binding"]["invocation_receipt_digest"]
    )


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
        poll_seconds=context["policy"].trace_hydration_poll_seconds,
        maximum_wait_seconds=context[
            "policy"
        ].trace_hydration_maximum_wait_seconds,
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
        poll_seconds=context["policy"].trace_hydration_poll_seconds,
        maximum_wait_seconds=context[
            "policy"
        ].trace_hydration_maximum_wait_seconds,
    )


def _authority_targets(context: dict[str, Any], authority: Any) -> list[Any]:
    ids = {
        authority.authority_id,
        context["paired_baselines"][authority.canonical_agent],
    }
    return [context["deployed"][authority_id] for authority_id in sorted(ids)]


def _prepared_result(value: dict[str, Any]) -> dict[str, Any]:
    verification_ready = not _incomplete_invocation_shards(value)
    return {
        "status": "prepared",
        "commit_sha": value["commit_sha"],
        "authority_count": len(value["validation_authority_ids"]),
        "reused_authority_count": len(value["reused_authorities"]),
        "deployment_shards": value["deployment_assignments"],
        "invoke_shards": value["invocation_shard_assignments"],
        "maximum_active_subsessions": 8,
        "invoke_shard_concurrency": 8,
        "verification_authority_concurrency": (
            MAX_ACTIVE_COPILOT_CLAIMS if verification_ready else 0
        ),
        "verification_pending_authority_count": (
            len(value["verification_authority_assignments"])
            if verification_ready
            else 0
        ),
    }


def _public_authority_history(
    active: Mapping[str, Any],
) -> list[dict[str, Any]]:
    agents, issues = load_catalogs()
    authorities = authority_specs(agents, issues)
    references = {
        item["authority_id"]: dict(item)
        for item in active["reused_authorities"]
        if "authority_result_digest" in item
    }
    references.update(
        current_authority_verification_results(
            prepared=active,
            authority_ids=active["validation_authority_ids"],
        )
    )
    result = []
    for authority in authorities:
        reference = references.get(authority.authority_id)
        if reference is None:
            status = "missing"
            reason = "current_generation_pending"
        else:
            value = load_authority_verification_result(reference)
            status = str(value["outcome"])
            reason = None if status == "PASS" else "current_non_pass"
        result.append(
            {
                "authority_id": authority.authority_id,
                "canonical_agent": authority.canonical_agent,
                "status": status,
                "changed": [],
                "verification_required_reason": reason,
            }
        )
    return result


def _incomplete_invocation_shards(
    prepared: Mapping[str, Any],
) -> list[dict[str, Any]]:
    incomplete = []
    for assignment in prepared["invocation_shard_assignments"]:
        store = ValidationShardStore(
            prepared=prepared,
            shard_id=assignment["shard_id"],
            authority_ids=assignment["authority_ids"],
        )
        try:
            artifact = store.read_invocations()
        except (ContractError, OSError):
            incomplete.append(dict(assignment))
            continue
        references = artifact.get("invocation_receipts")
        if (
            artifact.get("status") != "invoked"
            or not isinstance(references, list)
            or any(not isinstance(item, Mapping) for item in references)
            or [item.get("authority_id") for item in references]
            != sorted(assignment["authority_ids"])
        ):
            incomplete.append(dict(assignment))
    return incomplete


def _merge_authority_result_selection(
    *,
    authorities: list[Any],
    selected: list[str],
    reused: list[dict[str, str]],
    authority_results: Mapping[str, dict[str, str] | None],
    forced: set[str],
) -> tuple[list[str], list[dict[str, str]]]:
    selected_ids = set(selected)
    reused_by_id = {item["authority_id"]: item for item in reused}
    for authority_id in forced:
        selected_ids.add(authority_id)
        reused_by_id.pop(authority_id, None)
    for authority_id, reference in authority_results.items():
        if reference is None:
            selected_ids.add(authority_id)
            reused_by_id.pop(authority_id, None)
        else:
            selected_ids.discard(authority_id)
            reused_by_id[authority_id] = reference
    ordered_ids = [item.authority_id for item in authorities]
    return (
        [authority_id for authority_id in ordered_ids if authority_id in selected_ids],
        [
            reused_by_id[authority_id]
            for authority_id in ordered_ids
            if authority_id in reused_by_id
        ],
    )


def _recovery_authority_result_selection(
    *,
    prepared: Mapping[str, Any],
    plan: Mapping[str, Any],
    authorities: list[Any],
    recovery_intent: Mapping[str, Any],
) -> dict[str, dict[str, str] | None]:
    references = {
        item["authority_id"]: dict(item)
        for item in recovery_intent["source_authority_results"]
    }
    by_id = {item.authority_id: item for item in authorities}
    runtime_by_id = {
        item["authority_id"]: item
        for item in prepared["runtime_topology"]["agents"]
    }
    baseline_by_agent = {
        item.canonical_agent: item
        for item in authorities
        if item.authority_kind == "baseline"
    }
    if set(references) != set(by_id):
        raise ContractError("Validation recovery result manifest is incomplete")
    selected: dict[str, dict[str, str] | None] = {}
    for authority_id, reference in references.items():
        authority = by_id[authority_id]
        baseline = baseline_by_agent[authority.canonical_agent]
        result = load_bound_authority_verification_result(
            reference,
            authority=authority,
            paired_v0_authority=baseline,
            runtime=runtime_by_id[authority_id],
            paired_v0_runtime=runtime_by_id[baseline.authority_id],
            prepared=prepared,
            plan=plan,
            require_current_generation=False,
        )
        selected[authority_id] = (
            None if result["outcome"] == "INCOMPLETE" else reference
        )
    return selected


def _recovery_invocation_selection(
    *,
    prepared: Mapping[str, Any],
    plan: Mapping[str, Any],
    authorities: list[Any],
    selected_authority_ids: list[str],
    forced_authority_ids: set[str],
    recovery_intent: Mapping[str, Any],
) -> tuple[list[str], list[dict[str, str]]]:
    references = {
        item["authority_id"]: dict(item)
        for item in recovery_intent["source_invocation_receipts"]
    }
    by_id = {item.authority_id: item for item in authorities}
    runtime_by_id = {
        item["authority_id"]: item
        for item in prepared["runtime_topology"]["agents"]
    }
    baseline_by_agent = {
        item.canonical_agent: item
        for item in authorities
        if item.authority_kind == "baseline"
    }
    if not set(selected_authority_ids).issubset(references):
        raise ContractError("Validation recovery invocation manifest is incomplete")
    invoke = []
    reused = []
    reused_values = []
    for authority_id in selected_authority_ids:
        if authority_id in forced_authority_ids:
            invoke.append(authority_id)
            continue
        authority = by_id[authority_id]
        baseline = baseline_by_agent[authority.canonical_agent]
        reference = references[authority_id]
        reused_values.append(
            load_bound_invocation_receipt(
                reference,
                authority=authority,
                paired_v0_authority=baseline,
                runtime=runtime_by_id[authority_id],
                paired_v0_runtime=runtime_by_id[baseline.authority_id],
                prepared=prepared,
                plan=plan,
            )
        )
        reused.append(reference)
    assert_invocation_receipt_set_isolated(reused_values)
    return invoke, reused


def _copilot_authority_verification_result(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "status": (
            "verification_incomplete"
            if value["outcome"] == "INCOMPLETE"
            else "verified"
        ),
        "outcome": value["outcome"],
        "query_stage": value["query_stage"],
        "error_code": value["error_code"],
        "query_diagnostics": value["query_diagnostics"],
        "authority_result_digest": value["artifact_digest"],
    }


def _assessment_ready_result(
    *,
    package_path: Path,
    draft_path: Path,
    pending_count: int,
    active_count: int,
) -> dict[str, Any]:
    return {
        "status": "assessment_ready",
        "pending_authority_count": pending_count,
        "active_authority_evaluator_count": active_count,
        "available_authority_evaluator_slots": (
            MAX_ACTIVE_COPILOT_CLAIMS - active_count
        ),
        "package_path": str(package_path),
        "prompt_path": str(EVALUATION_PROMPT),
        "assessment_path": str(draft_path),
        "next_command": (
            "python -m agent_insights_quality "
            "import-test-agent-validation-assessment"
        ),
    }


def _claim_test_agent_validation_assessment(
    *,
    prepared: Mapping[str, Any],
    claimant: str,
) -> tuple[dict[str, Any], str, int, int] | dict[str, Any]:
    _assert_active_generation(prepared)
    existing = current_authority_verification_results(
        prepared=prepared,
        authority_ids=prepared["validation_authority_ids"],
    )
    pending = [
        assignment["authority_id"]
        for assignment in prepared["verification_authority_assignments"]
        if assignment["authority_id"] not in existing
    ]
    if not pending:
        return {
            "status": "verification_complete",
            "pending_authority_count": 0,
        }
    claims = [
        item
        for item in active_copilot_claims(prepared=prepared)
        if item["authority_id"] in pending
    ]
    pointer = next(
        (
            item
            for item in claims
            if item["claimant_reference"] == claimant
        ),
        None,
    )
    if pointer is None:
        claimed_authority_ids = {str(item["authority_id"]) for item in claims}
        available = [
            authority_id
            for authority_id in pending
            if authority_id not in claimed_authority_ids
        ]
        if len(claims) >= MAX_ACTIVE_COPILOT_CLAIMS or not available:
            return {
                "status": "assessment_capacity_full",
                "pending_authority_count": len(pending),
                "active_authority_evaluator_count": len(claims),
                "available_authority_evaluator_slots": 0,
            }
        authority_id = available[0]
        pointer = write_active_pointer(
            prepared=prepared,
            authority_id=authority_id,
            claimant_reference=claimant,
        )
        active_count = len(claims) + 1
    else:
        authority_id = str(pointer["authority_id"])
        active_count = len(claims)
    _assert_assessment_claim_binding(
        pointer,
        prepared=prepared,
        claimant_reference=claimant,
        authority_id=authority_id,
    )
    return pointer, authority_id, len(pending), active_count


def _assessment_busy_result(*, command: str) -> dict[str, Any]:
    return {
        "status": "assessment_busy",
        "retryable": True,
        "next_command": f"python -m agent_insights_quality {command}",
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
