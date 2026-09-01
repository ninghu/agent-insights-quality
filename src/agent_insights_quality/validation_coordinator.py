from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.live import LiveRuntime
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.util import ContractError, content_hash
from agent_insights_quality.validation_cleanup import CleanupEngine, build_cleanup_plan
from agent_insights_quality.validation_cleanup_azure import (
    AzureValidationCleanupBackend,
)
from agent_insights_quality.validation_credentials import local_azure_operator
from agent_insights_quality.validation_cycle import (
    ValidationCycleController,
    initial_lifecycle,
)
from agent_insights_quality.validation_evidence import (
    persist_evidence,
    stamp_evidence_digests,
    validate_evidence,
)
from agent_insights_quality.validation_execution import (
    _deployed_runtime,
    prepare_validation_topology,
)
from agent_insights_quality.validation_lifecycle import (
    LifecycleJournal,
    LocalValidationLock,
    validation_runtime_root,
)
from agent_insights_quality.validation_live import FoundryScenarioAttemptRunner
from agent_insights_quality.validation_local import (
    _capacity_from_lifecycle,
    _substrate,
    discover_local_git_context,
)
from agent_insights_quality.validation_manifest import (
    authority_specs,
    prepare_resumed_validation_plan,
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
from agent_insights_quality.validation_reconciler import ValidationReconciler
from agent_insights_quality.validation_runtime import (
    invoke_validation_shard,
    verify_validation_shard,
)
from agent_insights_quality.validation_shards import (
    SHARD_COUNT,
    ValidationShardStore,
    compose_shard_authorities,
    import_shard_resources,
    materialize_shard_resources,
    shard_root,
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
        previous = journal.read_optional()
        if previous is not None and previous.value["state"] not in {
            "CLEAN",
            "FAILED_CLEAN",
        }:
            if (
                previous.value["state"] == "VALIDATING"
                and previous.value["deployment"]["phase"] == "prepared"
                and previous.value["repository"] == git.repository
                and previous.value["pr_number"] == git.pr_number
                and previous.value["commit_sha"] == git.commit_sha
            ):
                return _prepared_result(previous.value)
            raise ContractError(
                "Incomplete prepared validation must be cleaned before prepare"
            )
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
            cycle_id=plan["cycle_id"],
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
        controller = ValidationCycleController(
            journal,
            active=journal.begin_cycle(initial),
        )
        support_agent = next(
            item
            for item in agents["agents"]
            if item["name"] == "support-ticket-agent"
        )

        def support_images() -> dict[str, str]:
            result = prepare_validation_support_images(
                profile,
                support_agent,
                cycle_id=plan["cycle_id"],
                record_resource=lambda event: controller.dynamic_resource_event(
                    event,
                    now=datetime.now(UTC),
                ),
            )
            controller.support_images_ready(
                result.images,
                now=datetime.now(UTC),
            )
            return result.images

        prepare_validation_topology(
            plan=plan,
            authorities=authorities,
            capacity_plan=capacity,
            controller=controller,
            project_provisioner=ValidationProjectProvisioner(
                profile,
                local_operator_id=operator.object_id,
                policy=policy,
            ),
            deployer_factory=lambda project: FoundryAuthorityDeployer(
                profile=profile,
                agent_catalog=agents,
                issue_catalog=issues,
                token_provider=operator.token_provider,
                project=project,
                support_images={},
            ),
            support_image_factory=support_images,
            policy=policy,
            assert_commit=lambda: _assert_git(git),
        )
        return _prepared_result(controller.active.value)


def invoke_test_agent_validation_shard(
    *,
    cycle_id: str,
    shard_id: int,
    authority_ids: list[str],
) -> dict[str, Any]:
    context = _load_prepared(
        cycle_id,
        shard_id,
        authority_ids,
        partition_invoke_capacity=True,
    )
    with shard_lock(
        repository=context["git"].repository,
        pr_number=context["git"].pr_number,
        cycle_id=cycle_id,
        shard_id=shard_id,
    ):
        store = context["store"]
        store.begin_invocation()
        runner = _runner(context, record_resource=store.record_resource)
        runner.prepare_hosted_routes(_shard_targets(context))
        results = invoke_validation_shard(
            context["assigned"],
            context["deployed"],
            runner=runner,
            scheduler=context["scheduler"],
            model_contract=context["agents"]["models"]["test_agents"],
            paired_baselines=context["paired_baselines"],
            record_authority=store.record_authority,
        )
        artifact = store.complete_invocation()
        return {
            "status": "invoked",
            "cycle_id": cycle_id,
            "shard_id": shard_id,
            "authority_count": len(results),
            "artifact_digest": artifact["artifact_digest"],
        }


def verify_test_agent_validation_shard(
    *,
    cycle_id: str,
    shard_id: int,
    authority_ids: list[str],
) -> dict[str, Any]:
    context = _load_prepared(cycle_id, shard_id, authority_ids)
    with shard_lock(
        repository=context["git"].repository,
        pr_number=context["git"].pr_number,
        cycle_id=cycle_id,
        shard_id=shard_id,
    ):
        store = context["store"]
        invocation = store.read_invocations()
        if invocation.get("status") != "invoked":
            raise ContractError("Validation shard invocation is not complete")
        authorities = verify_validation_shard(
            context["assigned"],
            context["deployed"],
            invocation["invocations"],
            runner=_runner(context, record_resource=lambda _event: None),
            scheduler=context["scheduler"],
            model_contract=context["agents"]["models"]["test_agents"],
            validated_commit_sha=context["git"].commit_sha,
            paired_baselines=context["paired_baselines"],
        )
        if not all(item["pass"] for item in authorities):
            raise ContractError("Validation shard mechanical evidence is incomplete")
        package = store.write_package(authorities=authorities)
        return {
            "status": "verified",
            "cycle_id": cycle_id,
            "shard_id": shard_id,
            "authority_count": len(authorities),
            "artifact_digest": package["artifact_digest"],
        }


def compose_test_agent_validation(*, cycle_id: str) -> dict[str, Any]:
    context = _load_prepared(cycle_id)
    packages = []
    invocations = []
    for shard_id in range(1, SHARD_COUNT + 1):
        root = (
            validation_runtime_root()
            / "shards"
            / context["git"].repository.replace("/", "/")
            / str(context["git"].pr_number)
            / cycle_id
            / f"shard-{shard_id:02d}"
        )
        raw = _read_shard_ids(root / "invocations.json")
        store = ValidationShardStore(
            prepared=context["prepared"],
            shard_id=shard_id,
            authority_ids=raw,
        )
        invocation = store.read_invocations()
        package = store.read_package()
        if package.get("invocation_digest") != invocation["artifact_digest"]:
            raise ContractError(
                "Validation shard package does not bind its exact invocation"
            )
        invocations.append(invocation)
        packages.append(package)
    authority_evidence = compose_shard_authorities(
        packages,
        context["authorities"],
    )
    lock = LocalValidationLock(validation_runtime_root() / "coordinator.lock")
    with lock:
        journal = LifecycleJournal(lock=lock)
        controller = ValidationCycleController(
            journal,
            active=journal.read_active(),
        )
        if controller.active.value["state"] != "VALIDATING":
            raise ContractError("Validation cycle has already been composed")
        import_shard_resources(
            controller,
            invocations,
            now=lambda: datetime.now(UTC),
        )
        active = controller.active.value
        evidence = stamp_evidence_digests(
            {
                "schema_version": "1.0.0",
                "kind": "test-agent-validation-evidence",
                "repository": active["repository"],
                "pr_number": active["pr_number"],
                "cycle_id": active["cycle_id"],
                "commit_sha": active["commit_sha"],
                "validation_digest": active["digests"]["validation_digest"],
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
            cycle_id=active["cycle_id"],
        )
        controller.final_checks(
            commit_sha=active["commit_sha"],
            evidence=record,
            now=datetime.now(UTC),
        )
        return {
            "status": "composed",
            "cycle_id": cycle_id,
            "authority_count": len(authority_evidence),
            "evidence_digest": evidence["evidence_digest"],
        }


def cleanup_test_agent_validation(
    *,
    cycle_id: str,
    shard_id: int | None = None,
    authority_ids: list[str] | None = None,
) -> dict[str, Any]:
    if shard_id is not None:
        context = _load_shard_cleanup(
            cycle_id,
            shard_id,
            authority_ids or [],
        )
        with shard_lock(
            repository=context["active"]["repository"],
            pr_number=context["active"]["pr_number"],
            cycle_id=cycle_id,
            shard_id=shard_id,
        ):
            store = context["store"]
            artifact = store.read_invocations()
            ownership_nonce = content_hash(
                {"cycle_id": cycle_id, "shard_id": shard_id}
            ).removeprefix("sha256:")
            resources = materialize_shard_resources(
                artifact,
                ownership_nonce=ownership_nonce,
                now=datetime.now(UTC),
            )
            backend = AzureValidationCleanupBackend(
                profile=context["profile"],
                runtime_topology=context["active"]["runtime_topology"],
                resources=resources,
                token_provider=context["operator"].token_provider,
            )
            result = CleanupEngine(backend).execute(
                build_cleanup_plan(
                    cycle_id=f"{cycle_id}-shard-{shard_id:02d}",
                    ownership_nonce=ownership_nonce,
                    resources=resources,
                    documented_project_cascade=context[
                        "policy"
                    ].documented_project_cascade,
                ),
                record_delete_intent=lambda _item: None,
            )
            if not result.exact_clean:
                raise ContractError("Validation shard cleanup is not exact")
            store.write_cleanup(
                invocation_digest=artifact["artifact_digest"],
                retained_count=len(result.retained_durable_ids),
            )
            return {
                "status": "clean",
                "cycle_id": cycle_id,
                "shard_id": shard_id,
                "exact_clean": True,
            }
    lock = LocalValidationLock(validation_runtime_root() / "coordinator.lock")
    with lock:
        journal = LifecycleJournal(lock=lock)
        active = journal.read_active()
        if active.value["cycle_id"] != cycle_id:
            raise ContractError("Validation cleanup cycle identity is not active")
        if active.value["state"] in {"CLEAN", "FAILED_CLEAN"}:
            return {
                "status": active.value["state"].casefold(),
                "cycle_id": cycle_id,
                "exact_clean": True,
            }
        context = _cleanup_context(active.value)
        controller = ValidationCycleController(
            journal,
            active=active,
        )
        invocations = []
        for shard_id in range(1, SHARD_COUNT + 1):
            root = (
                shard_root(
                    repository=active.value["repository"],
                    pr_number=active.value["pr_number"],
                    cycle_id=cycle_id,
                    shard_id=shard_id,
                )
            )
            if (root / "invocations.json").is_file():
                raw = _read_shard_ids(root / "invocations.json")
                invocations.append(
                    ValidationShardStore(
                        prepared=active.value,
                        shard_id=shard_id,
                        authority_ids=raw,
                    ).read_invocations()
                )
        import_shard_resources(
            controller,
            invocations,
            now=lambda: datetime.now(UTC),
        )
        backend = AzureValidationCleanupBackend(
            profile=context["profile"],
            runtime_topology=controller.active.value["runtime_topology"],
            resources=controller.active.value["resources"],
            token_provider=context["operator"].token_provider,
        )
        state = ValidationReconciler(
            journal=journal,
            cleanup=CleanupEngine(backend),
            policy=context["policy"],
        ).reconcile(alert=lambda _message: None)
        clean = journal.read_active()
        return {
            "status": state.casefold(),
            "cycle_id": cycle_id,
            "exact_clean": clean.value["cleanup"]["exact_clean"],
        }


def _load_prepared(
    cycle_id: str,
    shard_id: int | None = None,
    authority_ids: list[str] | None = None,
    *,
    partition_invoke_capacity: bool = False,
) -> dict[str, Any]:
    git = discover_local_git_context()
    policy = load_validation_policy()
    agents, issues = load_catalogs()
    authorities = authority_specs(agents, issues)
    plan = prepare_resumed_validation_plan(
        agents=agents,
        issues=issues,
        policy=policy,
        repository=git.repository,
        pr_number=git.pr_number,
        commit_sha=git.commit_sha,
        cycle_id=cycle_id,
    )
    validate_validation_plan(
        plan,
        agents=agents,
        issues=issues,
        policy=policy,
    )
    coordinator_lock = LocalValidationLock(
        validation_runtime_root() / "coordinator.lock"
    )
    prepared = LifecycleJournal(lock=coordinator_lock).read_active().value
    if (
        prepared["cycle_id"] != cycle_id
        or prepared["state"] not in {"VALIDATING", "FINAL_CHECKS"}
        or prepared["deployment"]["phase"] not in {"prepared", "complete"}
        or prepared["repository"] != git.repository
        or prepared["pr_number"] != git.pr_number
        or prepared["commit_sha"] != git.commit_sha
        or prepared["digests"]["validation_digest"] != plan["validation_digest"]
        or len(prepared["runtime_topology"]["agents"]) != 41
    ):
        raise ContractError("Prepared validation topology is not current")
    assigned = (
        validate_shard_assignment(
            int(shard_id),
            authority_ids or [],
            authorities,
        )
        if shard_id is not None
        else []
    )
    operator = local_azure_operator()
    base_profile = RuntimeProfile.from_env("staging", "g30")
    profile = validation_runtime_profile(
        plan["project_name"],
        cycle_id=cycle_id,
        base=base_profile,
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
    )
    return {
        "git": git,
        "policy": policy,
        "agents": agents,
        "authorities": authorities,
        "assigned": assigned,
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


def _load_shard_cleanup(
    cycle_id: str,
    shard_id: int,
    authority_ids: list[str],
) -> dict[str, Any]:
    coordinator_lock = LocalValidationLock(
        validation_runtime_root() / "coordinator.lock"
    )
    active = LifecycleJournal(lock=coordinator_lock).read_active().value
    if active["cycle_id"] != cycle_id:
        raise ContractError("Validation cleanup cycle identity is not active")
    agents, issues = load_catalogs()
    validate_shard_assignment(
        shard_id,
        authority_ids,
        authority_specs(agents, issues),
    )
    context = _cleanup_context(active)
    return {
        **context,
        "active": active,
        "store": ValidationShardStore(
            prepared=active,
            shard_id=shard_id,
            authority_ids=authority_ids,
        ),
    }


def _runner(
    context: dict[str, Any],
    *,
    record_resource: Any,
) -> FoundryScenarioAttemptRunner:
    return FoundryScenarioAttemptRunner(
        LiveRuntime(
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
        record_resource=record_resource,
    )


def _shard_targets(context: dict[str, Any]) -> list[Any]:
    ids = {item.authority_id for item in context["assigned"]}
    ids.update(
        context["paired_baselines"][item.canonical_agent]
        for item in context["assigned"]
    )
    return [context["deployed"][authority_id] for authority_id in sorted(ids)]


def _prepared_result(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "prepared",
        "cycle_id": value["cycle_id"],
        "commit_sha": value["commit_sha"],
        "authority_ids": [
            item["authority_id"] for item in value["runtime_topology"]["agents"]
        ],
        "invoke_shard_concurrency": 8,
        "verify_shard_concurrency": 4,
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


def _cleanup_context(active: dict[str, Any]) -> dict[str, Any]:
    policy = load_validation_policy()
    operator = local_azure_operator()
    substrate = active["substrate"]
    required = {
        "account_name",
        "account_resource_id",
        "registry_name",
        "storage_account_name",
        "telemetry_resource_id",
    }
    if not required.issubset(substrate) or not all(
        substrate[key] for key in required
    ):
        raise ContractError("Persisted validation cleanup substrate is incomplete")
    project_name = str(active["project"]["name"])
    account_name = str(substrate["account_name"])
    endpoint = (
        f"https://{account_name}.services.ai.azure.com/api/projects/{project_name}"
    )
    persisted_profile = RuntimeProfile(
        name="staging",
        project_name=project_name,
        project_endpoint=endpoint,
        insights_endpoint=endpoint,
        application_insights_resource_id=substrate["telemetry_resource_id"],
        registry_path=validation_runtime_root()
        / str(active["cycle_id"])
        / "deployment-registry.json",
        account_name=account_name,
        container_registry_name=substrate["registry_name"],
        registry_storage_account_name=substrate["storage_account_name"],
        account_resource_id=substrate["account_resource_id"],
        telemetry_resource_set="g30",
        environment_id="swedencentral-g30",
        location="swedencentral",
    )
    return {
        "policy": policy,
        "operator": operator,
        "profile": validation_runtime_profile(
            str(active["project"]["name"]),
            cycle_id=str(active["cycle_id"]),
            base=persisted_profile,
        ),
    }


def _read_shard_ids(path: Path) -> list[str]:
    from agent_insights_quality.util import read_json

    value = read_json(path)
    authority_ids = value.get("authority_ids")
    if not isinstance(authority_ids, list) or not all(
        isinstance(item, str) for item in authority_ids
    ):
        raise ContractError("Validation shard authority assignment is invalid")
    return authority_ids
