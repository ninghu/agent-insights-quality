from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.live import LiveRuntime
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    atomic_json,
    content_hash,
    read_json,
)
from agent_insights_quality.validation_cleanup import (
    CleanupEngine,
    cleanup_failure_summary,
)
from agent_insights_quality.validation_cleanup_azure import (
    AzureValidationCleanupBackend,
)
from agent_insights_quality.validation_credentials import (
    LocalAzureOperator,
    local_azure_operator,
)
from agent_insights_quality.validation_cycle import (
    ValidationCycleController,
    initial_lifecycle,
)
from agent_insights_quality.validation_execution import (
    cleanup_validation_cycle,
    execute_validation_plan,
)
from agent_insights_quality.validation_evidence import validate_evidence
from agent_insights_quality.validation_lifecycle import (
    LifecycleJournal,
    LocalValidationLock,
    read_bound_local_record,
    validation_runtime_root,
    validate_lifecycle,
)
from agent_insights_quality.validation_live import FoundryScenarioAttemptRunner
from agent_insights_quality.validation_judge import (
    FoundryJudgeClient,
    ValidationJudge,
)
from agent_insights_quality.validation_manifest import (
    authority_specs,
    prepare_validation_plan,
    prepare_resumed_validation_plan,
    validate_validation_plan,
    validation_authority_cost,
    validation_endpoint_costs,
)
from agent_insights_quality.validation_policy import (
    ValidationPolicy,
    load_validation_policy,
)
from agent_insights_quality.validation_permissions import (
    assert_validation_permissions,
)
from agent_insights_quality.validation_provisioning import (
    FoundryAuthorityDeployer,
    ProjectDeployment,
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


@dataclass(frozen=True)
class LocalGitContext:
    repository: str
    pr_number: int
    commit_sha: str


def discover_github_user() -> str:
    value = _run_json(["gh", "api", "user"], "GitHub user")
    login = str(value.get("login") or "")
    if not login:
        raise ContractError("Authenticated GitHub user identity is missing")
    return login


def discover_local_git_context() -> LocalGitContext:
    commit_sha = current_clean_commit()
    repository_value = _run_json(
        ["gh", "repo", "view", "--json", "nameWithOwner"],
        "GitHub repository",
    )
    repository = str(repository_value.get("nameWithOwner") or "")
    if (
        len(repository.split("/")) != 2
        or any(not part or part.strip() != part for part in repository.split("/"))
    ):
        raise ContractError("GitHub repository identity is invalid")
    matching_pulls: list[dict[str, Any]] = []
    seen_numbers: set[int] = set()
    page = 1
    while True:
        pulls = _run_json_array(
            [
                "gh",
                "api",
                "--method",
                "GET",
                (
                    f"repos/{repository}/commits/{commit_sha}/pulls"
                    f"?per_page=100&page={page}"
                ),
                "--header",
                "Accept: application/vnd.github+json",
                "--header",
                "X-GitHub-Api-Version: 2022-11-28",
            ],
            "GitHub pull requests",
        )
        for pull in pulls:
            number = pull.get("number")
            state = pull.get("state")
            head = pull.get("head")
            head_sha = head.get("sha") if isinstance(head, dict) else None
            if (
                not isinstance(number, int)
                or isinstance(number, bool)
                or number < 1
                or state not in {"open", "closed"}
                or not isinstance(head, dict)
                or not isinstance(head_sha, str)
                or not _git_sha(head_sha)
                or number in seen_numbers
            ):
                raise ContractError("GitHub pull request response is invalid")
            seen_numbers.add(number)
            if state == "open" and head_sha == commit_sha:
                matching_pulls.append(pull)
        if len(pulls) < 100:
            break
        page += 1
    if len(matching_pulls) != 1:
        raise ContractError(
            "Current clean commit must be the exact head of exactly one open "
            "pull request"
        )
    pr_number = matching_pulls[0]["number"]
    return LocalGitContext(
        repository=repository,
        pr_number=pr_number,
        commit_sha=commit_sha,
    )


def current_clean_commit() -> str:
    _assert_repository_root()
    status = _run_text(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        "Git worktree status",
    )
    if status:
        raise ContractError("Local Test Agent Validation requires a clean worktree")
    commit_sha = _run_text(["git", "rev-parse", "HEAD"], "Git commit")
    if not _git_sha(commit_sha):
        raise ContractError("Local Git commit identity is invalid")
    return commit_sha


def run_test_agent_validation(
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    total_started = monotonic()
    durations = {
        "lock_preflight_seconds": 0.0,
        "project_connections_seconds": 0.0,
        "support_images_seconds": 0.0,
        "phase_1_deployment_seconds": 0.0,
        "phase_1_clean_interval_seconds": 0.0,
        "phase_2_deployment_seconds": 0.0,
        "phase_2_clean_interval_seconds": 0.0,
        "endpoint_model_seconds": 0.0,
        "ingestion_kql_seconds": 0.0,
        "cleanup_seconds": 0.0,
        "total_seconds": 0.0,
    }
    duration_lock = threading.Lock()
    duration_path: Path | None = None

    def record_duration(stage: str, value: float) -> None:
        with duration_lock:
            if stage not in durations:
                raise ContractError(
                    "Validation duration stage is not reviewed"
                )
            durations[stage] += max(0.0, value)
            if duration_path is not None:
                _persist_durations(
                    duration_path,
                    cycle_id=plan["cycle_id"],
                    durations=durations,
                )

    lock = LocalValidationLock()
    with lock:
        git = discover_local_git_context()
        policy = load_validation_policy()
        agents, issues = load_catalogs()
        local_run_id = uuid.uuid4().hex
        new_plan = prepare_validation_plan(
            agents=agents,
            issues=issues,
            policy=policy,
            repository=git.repository,
            pr_number=git.pr_number,
            commit_sha=git.commit_sha,
            local_run_id=local_run_id,
        )
        validate_validation_plan(
            new_plan,
            agents=agents,
            issues=issues,
            policy=policy,
        )
        operator = local_azure_operator()
        base_profile = RuntimeProfile.from_env("staging", "g29")
        journal = LifecycleJournal(lock=lock)
        previous = journal.read_optional()
        plan = new_plan
        resume_deployment = False
        if previous is not None and previous.value["state"] not in {
            "CLEAN",
            "FAILED_CLEAN",
        }:
            resumed_plan = prepare_resumed_validation_plan(
                agents=agents,
                issues=issues,
                policy=policy,
                repository=git.repository,
                pr_number=git.pr_number,
                commit_sha=git.commit_sha,
                cycle_id=str(previous.value["cycle_id"]),
            )
            validate_validation_plan(
                resumed_plan,
                agents=agents,
                issues=issues,
                policy=policy,
            )
            resume_deployment = _deployment_resume_allowed(
                previous.value,
                git=git,
                plan=resumed_plan,
                operator=operator,
                base_profile=base_profile,
                now=now(),
            )
            if resume_deployment:
                plan = resumed_plan
            else:
                _recover_incomplete(
                    journal=journal,
                    policy=policy,
                    base_profile=base_profile,
                    operator=operator,
                    now=now,
                )
                previous = journal.read_active()
        LiveRuntime(
            base_profile,
            token_provider=operator.token_provider,
        ).assert_telemetry_read_access()
        if (
            previous is not None
            and previous.value["state"] == "CLEAN"
            and previous.value["commit_sha"] == git.commit_sha
            and previous.value["digests"]["validation_digest"]
            == plan["validation_digest"]
        ):
            _verify_existing_clean_result(
                journal=journal,
                active=previous.value,
                commit_sha=git.commit_sha,
                validation_digest=plan["validation_digest"],
            )
            return {
                "status": "already_clean",
                "commit_sha": git.commit_sha,
                "authority_count": 41,
            }

        profile = validation_runtime_profile(
            plan["project_name"],
            cycle_id=plan["cycle_id"],
            base=base_profile,
        )
        duration_path = (
            validation_runtime_root()
            / "durations"
            / git.repository.replace("/", "--")
            / str(git.pr_number)
            / plan["cycle_id"]
            / "durations.json"
        )
        if resume_deployment and duration_path.is_file():
            previous_durations = read_json(duration_path).get("stages")
            if not isinstance(previous_durations, dict) or set(
                previous_durations
            ) != set(durations):
                raise ContractError(
                    "Retained validation durations are invalid"
                )
            durations.update(
                {
                    key: float(value)
                    for key, value in previous_durations.items()
                }
            )
        assert_validation_permissions(base_profile, operator)
        authorities = authority_specs(agents, issues)
        measurement = measure_test_agent_capacity(profile, now=now)
        costs = validation_endpoint_costs(authorities)
        measured_capacity = build_capacity_plan(
            measurement,
            policy=policy,
            costs=costs,
        )
        capacity = (
            _capacity_from_lifecycle(previous.value)
            if resume_deployment and previous is not None
            else measured_capacity
        )
        if resume_deployment and (
            measured_capacity.available_rpm < capacity.available_rpm
            or measured_capacity.available_tpm < capacity.available_tpm
        ):
            _recover_incomplete(
                journal=journal,
                policy=policy,
                base_profile=base_profile,
                operator=operator,
                now=now,
            )
            raise ContractError(
                "Retained validation capacity headroom is no longer available"
            )
        scheduler = ValidationScheduler(
            capacity,
            WeightedTokenBucket(
                request_capacity=capacity.available_rpm,
                token_capacity=capacity.available_tpm,
            ),
        )
        project_provisioner = ValidationProjectProvisioner(
            profile,
            local_operator_id=operator.object_id,
            policy=policy,
        )
        if resume_deployment:
            assert previous is not None
            active = previous
        else:
            project_provisioner.assert_project_absent(plan["project_name"])
            session_id = uuid.uuid4().hex
            initial = initial_lifecycle(
                plan,
                policy=policy,
                ownership_nonce=uuid.uuid4().hex,
                holder_session_reference=content_hash(
                    {"session_id": session_id}
                ),
                holder_operator_reference=operator.operator_reference,
                holder_run_reference=content_hash({"run_id": local_run_id}),
                substrate=_substrate(operator, base_profile),
                now=now(),
            )
            active = journal.begin_cycle(initial)
        record_duration(
            "lock_preflight_seconds",
            monotonic() - total_started,
        )
        controller = ValidationCycleController(journal, active=active)
        support_agent = next(
            item
            for item in agents["agents"]
            if item["name"] == "support-ticket-agent"
        )

        def resource_event(event: dict[str, Any]) -> None:
            controller.dynamic_resource_event(event, now=now())

        def support_image_factory() -> dict[str, str]:
            stored_images = controller.active.value["deployment"][
                "support_images"
            ]
            if stored_images:
                return {
                    item["logical_version"]: item["image"]
                    for item in stored_images
                }
            support_started = monotonic()
            support = prepare_validation_support_images(
                profile,
                support_agent,
                cycle_id=plan["cycle_id"],
                record_resource=resource_event,
            )
            controller.support_images_ready(
                support.images,
                now=now(),
            )
            record_duration(
                "support_images_seconds",
                monotonic() - support_started,
            )
            return support.images

        def deployer_factory(project: ProjectDeployment) -> FoundryAuthorityDeployer:
            return FoundryAuthorityDeployer(
                profile=profile,
                agent_catalog=agents,
                issue_catalog=issues,
                token_provider=operator.token_provider,
                project=project,
                support_images={},
            )

        runtime = LiveRuntime(profile, token_provider=operator.token_provider)
        runner = FoundryScenarioAttemptRunner(
            runtime,
            endpoint_costs={
                item.authority_id: validation_authority_cost(item)
                for item in authorities
            },
            stabilization_seconds=180,
            record_resource=resource_event,
            record_duration=record_duration,
            now=now,
        )
        judge = ValidationJudge(
            client=FoundryJudgeClient(
                endpoint=profile.project_endpoint,
                request_json=runtime._json_request,
                max_output_tokens=policy.judge.max_output_tokens,
            ),
            issues={item["id"]: item for item in issues["issues"]},
            baseline_output_messages={
                item["name"]: item["baseline_contract"]["output_messages"]
                for item in agents["agents"]
            },
            repository=plan["repository"],
            pr_number=plan["pr_number"],
            cycle_id=plan["cycle_id"],
            commit_sha=plan["commit_sha"],
            validation_digest=plan["validation_digest"],
            runtime_topology_digest=plan["planned_topology_digest"],
            maximum_concurrency=policy.judge.maximum_concurrency,
        )

        def assert_commit() -> None:
            if discover_local_git_context() != git:
                raise ContractError(
                    "Local commit or pull request changed during validation"
                )

        execution_error: BaseException | None = None
        try:
            execute_validation_plan(
                plan=plan,
                authorities=authorities,
                capacity_plan=capacity,
                controller=controller,
                project_provisioner=project_provisioner,
                deployer_factory=deployer_factory,
                support_image_factory=support_image_factory,
                runner=runner,
                judge=judge,
                scheduler=scheduler,
                policy=policy,
                model_contract=agents["models"]["test_agents"],
                assert_commit=assert_commit,
                record_duration=record_duration,
                monotonic=monotonic,
                sleep=time.sleep,
                now=now,
            )
        except (ContractError, OSError, RuntimeError) as error:
            execution_error = error

        cleanup_backend = AzureValidationCleanupBackend(
            profile=profile,
            runtime_topology=controller.active.value["runtime_topology"],
            resources=controller.active.value["resources"],
            token_provider=operator.token_provider,
        )
        cleanup_started = monotonic()
        try:
            clean = cleanup_validation_cycle(
                controller=controller,
                backend=cleanup_backend,
                policy=policy,
                failed_cycle=execution_error is not None,
                now=now,
            )
        except (ContractError, OSError, RuntimeError) as cleanup_error:
            if controller.active.value["state"] == "CLEANING":
                controller.mark_cleanup_blocked(
                    failure=cleanup_failure_summary(cleanup_error),
                    now=now(),
                )
            raise ContractError(
                "Local Test Agent Validation cleanup is blocked"
            ) from cleanup_error
        record_duration(
            "cleanup_seconds",
            monotonic() - cleanup_started,
        )
        record_duration(
            "total_seconds",
            monotonic() - total_started,
        )
        if execution_error is not None:
            raise ContractError(
                "Local Test Agent Validation failed and cleaned exactly"
            ) from execution_error
        return {
            "status": "clean",
            "commit_sha": git.commit_sha,
            "authority_count": len(clean.value["runtime_topology"]["agents"]),
        }


def _persist_durations(
    path: Path,
    *,
    cycle_id: str,
    durations: dict[str, float],
) -> None:
    atomic_json(
        path,
        {
            "schema_version": "1.0.0",
            "kind": "test-agent-validation-raw-durations",
            "cycle_id": cycle_id,
            "percentiles_calculated": False,
            "stages": durations,
        },
    )


def _deployment_resume_allowed(
    active: dict[str, Any],
    *,
    git: LocalGitContext,
    plan: dict[str, Any],
    operator: LocalAzureOperator,
    base_profile: RuntimeProfile,
    now: datetime,
) -> bool:
    expires = datetime.fromisoformat(
        str(active["absolute_expires_at"]).replace("Z", "+00:00")
    ).astimezone(UTC)
    planned_names = {
        item["authority_id"]: item["runtime_agent_name"]
        for item in plan["authorities"]
    }
    ready_agents = active["runtime_topology"]["agents"]
    try:
        _assert_recovery_substrate(
            active["substrate"],
            operator,
            base_profile,
        )
    except ContractError:
        return False
    return (
        active["state"] == "CREATING"
        and active["failure"] is None
        and active["cleanup"]["status"] == "not_started"
        and active["project"]["state"] == "created"
        and active["project"]["name"] == plan["project_name"]
        and active["repository"] == git.repository
        and active["pr_number"] == git.pr_number
        and active["commit_sha"] == git.commit_sha
        and active["operator"]["operator_reference"]
        == operator.operator_reference
        and active["digests"]["validation_digest"]
        == plan["validation_digest"]
        and active["digests"]["execution_matrix_digest"]
        == plan["execution_matrix_digest"]
        and active["digests"]["runtime_topology_digest"]
        == plan["planned_topology_digest"]
        and active["capacity"] is not None
        and active["deployment"]["phase"] == "phase_1_deployment"
        and active["deployment"]["traffic_started"] is False
        and not active["deployment"]["failures"]
        and now < expires
        and all(
            planned_names.get(item["authority_id"])
            == item["runtime_agent_name"]
            for item in ready_agents
        )
    )


def _capacity_from_lifecycle(active: dict[str, Any]) -> CapacityPlan:
    value = active.get("capacity")
    if not isinstance(value, dict):
        raise ContractError(
            "Retained validation capacity plan is missing"
        )
    try:
        plan = CapacityPlan(**value)
    except TypeError as error:
        raise ContractError(
            "Retained validation capacity plan is invalid"
        ) from error
    if plan.plan_digest != active["digests"]["quota_plan_digest"]:
        raise ContractError(
            "Retained validation capacity digest changed"
        )
    return plan


def _recover_incomplete(
    *,
    journal: LifecycleJournal,
    policy: ValidationPolicy,
    base_profile: RuntimeProfile,
    operator: LocalAzureOperator,
    now: Callable[[], datetime],
) -> None:
    current = journal.read_active()
    persisted_profile = _profile_for_substrate(
        base_profile,
        current.value["substrate"],
    )
    profile = validation_runtime_profile(
        str(current.value["project"]["name"]),
        cycle_id=str(current.value["cycle_id"]),
        base=persisted_profile,
    )
    backend = AzureValidationCleanupBackend(
        profile=profile,
        runtime_topology=current.value["runtime_topology"],
        resources=current.value["resources"],
        token_provider=operator.token_provider,
    )
    from agent_insights_quality.validation_reconciler import ValidationReconciler

    state = ValidationReconciler(
        journal=journal,
        cleanup=CleanupEngine(backend),
        policy=policy,
    ).reconcile(
        alert=lambda _: None,
        now=now(),
    )
    if state == "CLEANUP_BLOCKED":
        raise ContractError(
            "Prior local validation cleanup remains blocked; no new cycle started"
        )


def _profile_for_substrate(
    base: RuntimeProfile,
    substrate: dict[str, str],
) -> RuntimeProfile:
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
        raise ContractError(
            "Persisted validation cleanup substrate is incomplete"
        )
    return RuntimeProfile(
        name=base.name,
        project_name=base.project_name,
        project_endpoint=base.project_endpoint,
        insights_endpoint=base.insights_endpoint,
        application_insights_resource_id=substrate[
            "telemetry_resource_id"
        ],
        registry_path=base.registry_path,
        account_name=substrate["account_name"],
        container_registry_name=substrate["registry_name"],
        registry_storage_account_name=substrate[
            "storage_account_name"
        ],
        account_resource_id=substrate["account_resource_id"],
        telemetry_resource_set="g29",
    )


def _verify_existing_clean_result(
    *,
    journal: LifecycleJournal,
    active: dict[str, Any],
    commit_sha: str,
    validation_digest: str,
) -> None:
    clean = read_bound_local_record(
        journal.root,
        active["clean_reference"],
        digest_field="journal_digest",
        label="CLEAN",
    ).value
    evidence = read_bound_local_record(
        validation_runtime_root(),
        active["evidence_reference"],
        digest_field="evidence_digest",
        label="evidence",
    ).value
    validate_lifecycle(clean)
    validate_evidence(evidence)
    if (
        clean["state"] != "CLEAN"
        or clean["repository"] != active["repository"]
        or clean["pr_number"] != active["pr_number"]
        or clean["cycle_id"] != active["cycle_id"]
        or clean["commit_sha"] != commit_sha
        or clean["cleanup"]["exact_clean"] is not True
        or evidence["repository"] != active["repository"]
        or evidence["pr_number"] != active["pr_number"]
        or evidence["cycle_id"] != active["cycle_id"]
        or evidence["commit_sha"] != commit_sha
        or evidence["validation_digest"] != validation_digest
        or evidence["runtime_topology_digest"]
        != clean["digests"]["runtime_topology_digest"]
        or evidence["resource_inventory_digest"]
        != clean["digests"]["evidence_resource_inventory_digest"]
        or clean["digests"]["clean_resource_inventory_digest"]
        != content_hash(clean["resources"])
        or active["digests"]["evidence_digest"] != evidence["evidence_digest"]
    ):
        raise ContractError("Existing local CLEAN result is not current")


def _substrate(
    operator: LocalAzureOperator,
    profile: RuntimeProfile,
) -> dict[str, str]:
    values = {
        "tenant_id": operator.tenant_id,
        "subscription_id": operator.subscription_id,
        "account_name": profile.account_name,
        "account_resource_id": profile.account_resource_id,
        "registry_name": profile.container_registry_name,
        "storage_account_name": profile.registry_storage_account_name,
        "telemetry_resource_id": profile.application_insights_resource_id,
    }
    if not all(values.values()):
        raise ContractError("Validation Azure substrate identity is incomplete")
    subscription_prefix = f"/subscriptions/{operator.subscription_id}/".casefold()
    if not all(
        str(values[field]).casefold().startswith(subscription_prefix)
        for field in ("account_resource_id", "telemetry_resource_id")
    ):
        raise ContractError(
            "Validation resources do not belong to the active Azure subscription"
        )
    return values


def _assert_recovery_substrate(
    expected: dict[str, str],
    operator: LocalAzureOperator,
    profile: RuntimeProfile,
) -> None:
    if expected != _substrate(operator, profile):
        raise ContractError(
            "Current Azure context does not match the interrupted validation substrate"
        )


def _run_text(arguments: list[str], label: str) -> str:
    process = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=ROOT,
    )
    if process.returncode != 0:
        raise ContractError(f"{label} could not be queried")
    return process.stdout.strip()


def _run_json(arguments: list[str], label: str) -> dict[str, Any]:
    raw = _run_text(arguments, label)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ContractError(f"{label} response is invalid") from error
    if not isinstance(value, dict):
        raise ContractError(f"{label} response is not an object")
    return value


def _run_json_array(arguments: list[str], label: str) -> list[dict[str, Any]]:
    raw = _run_text(arguments, label)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ContractError(f"{label} response is invalid") from error
    if not isinstance(value, list) or any(
        not isinstance(item, dict) for item in value
    ):
        raise ContractError(f"{label} response is not an object array")
    return value


def _git_sha(value: str) -> bool:
    return (
        len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _assert_repository_root() -> None:
    expected = ROOT.resolve()
    root = _run_text(
        ["git", "rev-parse", "--show-toplevel"],
        "Git repository root",
    )
    if Path(root).resolve() != expected:
        raise ContractError("Imported repository root does not match Git")
    ambient = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if (
        ambient.returncode != 0
        or Path(ambient.stdout.strip()).resolve() != expected
    ):
        raise ContractError(
            "Current worktree does not match the imported repository root"
        )
