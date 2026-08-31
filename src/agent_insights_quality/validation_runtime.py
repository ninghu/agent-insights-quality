from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from agent_insights_quality.util import (
    ContractError,
    SharedRuntimeError,
    content_hash,
)
from agent_insights_quality.validation_evidence import (
    authority_predicate_contract_digest,
    digest_without_field,
    scenario_predicate_contract_digest,
)
from agent_insights_quality.validation_policy import ValidationPolicy
from agent_insights_quality.validation_quota import ValidationScheduler
from agent_insights_quality.validation_rules import validate_validation_rules


@dataclass(frozen=True)
class AuthoritySpec:
    authority_id: str
    authority_kind: str
    canonical_agent: str
    logical_version: str
    runtime_kind: str
    framework: str
    source_content_digest: str
    execution_digest: str
    validation_mode: str
    validation_rules: dict[str, Any]


@dataclass(frozen=True)
class PlannedRuntime:
    authority_id: str
    canonical_agent: str
    logical_version: str
    runtime_kind: str
    framework: str
    runtime_agent_name: str


@dataclass(frozen=True)
class DeployedRuntime:
    authority_id: str
    runtime_kind: str
    runtime_agent_name: str
    runtime_agent_version: str
    provider_agent_id: str
    provider_agent_version_id: str
    hosted_identity_id: str | None
    hosted_blueprint_id: str | None
    hosted_deployment_id: str | None
    runtime_principal_id: str | None
    telemetry_identity_id: str
    connection_ids: tuple[str, ...]


class AgentDeploymentIncomplete(ContractError):
    def __init__(self, failures: list[dict[str, Any]]) -> None:
        super().__init__("One or more Agent deployment lanes are incomplete")
        self.failures = failures


class AgentExecutionIncomplete(ContractError):
    def __init__(
        self,
        failures: list[dict[str, Any]],
        partial_results: list[dict[str, Any]],
    ) -> None:
        super().__init__("One or more Agent traffic lanes are incomplete")
        self.failures = failures
        self.partial_results = partial_results


class AuthorityDeployer(Protocol):
    def deploy(self, authority: AuthoritySpec, planned: PlannedRuntime) -> DeployedRuntime:
        ...

    def assert_ready(
        self,
        authority: AuthoritySpec,
        deployed: DeployedRuntime,
    ) -> None:
        ...


class ScenarioAttemptRunner(Protocol):
    def prepare_hosted_routes(self, targets: list[DeployedRuntime]) -> None: ...

    def run(
        self,
        *,
        target: DeployedRuntime,
        executing_authority_id: str,
        conversation_role: str,
        scenario: Mapping[str, Any],
        attempt: Mapping[str, Any],
        expect_defect: bool,
        scheduler: ValidationScheduler,
    ) -> dict[str, Any]: ...


def opaque_cycle_suffix(
    *,
    repository: str,
    pr_number: int,
    commit_sha: str,
    run_id: str,
) -> str:
    digest = hashlib.sha256(
        f"{repository}\0{pr_number}\0{commit_sha}\0{run_id}".encode(
            "ascii"
        )
    ).hexdigest()
    return digest[:12]


def validation_project_name(
    cycle_suffix: str,
    *,
    policy: ValidationPolicy,
) -> str:
    return _bounded_name(
        f"aiq-validation-{cycle_suffix}",
        maximum=policy.project_name_policy.maximum_length,
        pattern=policy.project_name_policy.pattern,
    )


def validation_agent_name(
    *,
    canonical_agent: str,
    logical_version: str,
    cycle_suffix: str,
    policy: ValidationPolicy,
) -> str:
    qualifier = (
        "baseline"
        if logical_version == "v0"
        else logical_version.replace("issue-", "issue-")
    )
    return _bounded_name(
        f"{canonical_agent}-{qualifier}-{cycle_suffix}",
        maximum=policy.agent_name_policy.maximum_length,
        pattern=policy.agent_name_policy.pattern,
    )


def plan_runtime_topology(
    authorities: Sequence[AuthoritySpec],
    *,
    cycle_suffix: str,
    policy: ValidationPolicy,
) -> tuple[PlannedRuntime, ...]:
    if len(authorities) != policy.authority_count:
        raise ContractError("Validation topology requires exactly 41 authorities")
    authority_ids = [item.authority_id for item in authorities]
    if len(authority_ids) != len(set(authority_ids)):
        raise ContractError("Validation topology authority IDs must be unique")
    planned = tuple(
        PlannedRuntime(
            authority_id=authority.authority_id,
            canonical_agent=authority.canonical_agent,
            logical_version=authority.logical_version,
            runtime_kind=authority.runtime_kind,
            framework=authority.framework,
            runtime_agent_name=validation_agent_name(
                canonical_agent=authority.canonical_agent,
                logical_version=authority.logical_version,
                cycle_suffix=cycle_suffix,
                policy=policy,
            ),
        )
        for authority in authorities
    )
    names = [item.runtime_agent_name for item in planned]
    if len(names) != len(set(names)):
        raise ContractError("Generated validation Agent names collide")
    return planned


def deploy_all_authorities(
    authorities: Sequence[AuthoritySpec],
    planned: Sequence[PlannedRuntime],
    *,
    deployer: AuthorityDeployer,
    maximum_concurrency: int,
    record_resource: Callable[[dict[str, Any]], None] | None = None,
    existing_deployed: Mapping[str, DeployedRuntime] | None = None,
    retry_counts: Mapping[str, int] | None = None,
    max_recovery_versions_per_agent: int = 3,
    record_ready: Callable[[AuthoritySpec, DeployedRuntime], None]
    | None = None,
    record_recovery: Callable[[AuthoritySpec, str, int, str], None]
    | None = None,
    record_failure: Callable[[dict[str, Any]], None] | None = None,
    require_architecture_canaries: bool = True,
    prior_recovered_authorities: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, DeployedRuntime]:
    if maximum_concurrency < 1 or maximum_concurrency > 8:
        raise ContractError("Validation provisioning concurrency must be between 1 and 8")
    by_id = {item.authority_id: item for item in planned}
    if set(by_id) != {item.authority_id for item in authorities}:
        raise ContractError("Planned validation topology is incomplete")
    authority_by_id = {item.authority_id: item for item in authorities}
    deployed: dict[str, DeployedRuntime] = dict(existing_deployed or {})
    retries = dict(retry_counts or {})
    recovered_by_agent = {
        agent: set(authority_ids)
        for agent, authority_ids in (
            prior_recovered_authorities or {}
        ).items()
    }
    if (
        not set(deployed).issubset(by_id)
        or not set(retries).issubset(by_id)
        or max_recovery_versions_per_agent != 3
    ):
        raise ContractError("Validation deployment resume state is invalid")
    for authority_id in retries:
        recovered_by_agent.setdefault(
            authority_by_id[authority_id].canonical_agent,
            set(),
        ).add(authority_id)
    lock = threading.Lock()
    def deploy(authority: AuthoritySpec) -> DeployedRuntime:
        target = by_id[authority.authority_id]
        intents = _deployment_intents(authority, target)
        if record_resource is not None:
            for event in intents:
                record_resource(event)
        try:
            value = deployer.deploy(authority, target)
        except ContractError:
            if record_resource is not None:
                for event in intents:
                    record_resource({**event, "state": "ambiguous_create"})
            raise
        if record_resource is not None:
            observed = {
                "provider_agent": value.provider_agent_id,
                "provider_agent_version": value.provider_agent_version_id,
                "hosted_identity": value.hosted_identity_id,
                "hosted_blueprint": value.hosted_blueprint_id,
                "hosted_deployment": value.hosted_deployment_id,
                "runtime_principal": value.runtime_principal_id,
            }
            for event in intents:
                provider_id = observed[event["kind"]]
                if provider_id is None:
                    if authority.runtime_kind != "prompt":
                        raise ContractError(
                            "Hosted validation deployment resource is missing"
                        )
                    continue
                record_resource(
                    {
                        **event,
                        "state": "created",
                        "provider_id": provider_id,
                        "deterministic_name": (
                            f"{value.runtime_agent_name}/"
                            f"{value.runtime_agent_version}"
                            if event["kind"] == "provider_agent_version"
                            else value.runtime_agent_name
                        ),
                    }
                )
        return value

    def accept(authority_id: str, value: DeployedRuntime) -> None:
        if (
            value.authority_id != authority_id
            or value.runtime_kind != by_id[authority_id].runtime_kind
            or value.runtime_agent_name != by_id[authority_id].runtime_agent_name
        ):
            raise ContractError("Deployed runtime identity differs from its plan")
        with lock:
            if authority_id in deployed:
                raise ContractError("Validation authority was deployed more than once")
            deployed[authority_id] = value
        if record_ready is not None:
            record_ready(authority_by_id[authority_id], value)

    def deploy_stage(
        stage: Sequence[AuthoritySpec],
        *,
        concurrency: int,
    ) -> None:
        pending = [
            authority
            for authority in stage
            if authority.authority_id not in deployed
        ]
        while pending:
            failures: list[tuple[AuthoritySpec, ContractError]] = []
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {
                    pool.submit(deploy, authority): authority
                    for authority in pending
                }
                for future in as_completed(futures):
                    authority = futures[future]
                    try:
                        value = future.result()
                    except ContractError as error:
                        failures.append((authority, error))
                    else:
                        accept(authority.authority_id, value)
            deterministic = [
                (authority, error)
                for authority, error in failures
                if getattr(error, "transient", False) is not True
            ]
            shared = [
                error
                for _, error in failures
                if isinstance(error, SharedRuntimeError)
            ]
            if shared:
                raise shared[0]
            if deterministic:
                summaries = [
                    _agent_failure_summary(
                        authority,
                        stage="deployment",
                        error=error,
                        request_accepted=False,
                    )
                    for authority, error in deterministic
                ]
                if record_failure is not None:
                    for summary in summaries:
                        record_failure(summary)
                raise AgentDeploymentIncomplete(summaries)
            if not failures:
                return
            next_pending = []
            for authority, _ in failures:
                with lock:
                    previous_count = retries.get(
                        authority.authority_id,
                        0,
                    )
                    recovered_versions = recovered_by_agent.setdefault(
                        authority.canonical_agent,
                        set(),
                    )
                    exhausted = (
                        previous_count
                        >= max_recovery_versions_per_agent
                        or (
                            authority.authority_id
                            not in recovered_versions
                            and len(recovered_versions)
                            >= max_recovery_versions_per_agent
                        )
                    )
                    retry_count = previous_count + 1
                    if not exhausted:
                        retries[authority.authority_id] = retry_count
                        recovered_versions.add(authority.authority_id)
                if exhausted:
                    summary = _agent_failure_summary(
                        authority,
                        stage="deployment",
                        error_code="recovery_exhausted",
                        request_accepted=False,
                    )
                    if record_failure is not None:
                        record_failure(summary)
                    raise AgentDeploymentIncomplete([summary])
                if record_recovery is not None:
                    record_recovery(
                        authority,
                        "failed",
                        retry_count,
                        "transient_provider_error",
                    )
                next_pending.append(authority)
            pending = next_pending

    canary_ids: set[str] = set()
    if require_architecture_canaries:
        prompt_canary, hosted_canary = _deployment_canaries(authorities)
        canary_ids = {
            prompt_canary.authority_id,
            hosted_canary.authority_id,
        }
        def deploy_canary(
            canary: AuthoritySpec,
        ) -> list[dict[str, Any]]:
            try:
                if canary.authority_id not in deployed:
                    deploy_stage([canary], concurrency=1)
                deployer.assert_ready(canary, deployed[canary.authority_id])
            except SharedRuntimeError:
                raise
            except AgentDeploymentIncomplete as error:
                return error.failures
            except ContractError as error:
                summary = _agent_failure_summary(
                    canary,
                    stage="readiness",
                    error=error,
                    request_accepted=False,
                )
                if record_failure is not None:
                    record_failure(summary)
                return [summary]
            return []

        canary_failures: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(deploy_canary, canary)
                for canary in (prompt_canary, hosted_canary)
            ]
            for future in as_completed(futures):
                try:
                    canary_failures.extend(future.result())
                except SharedRuntimeError:
                    for pending in futures:
                        pending.cancel()
                    raise
        if canary_failures:
            raise AgentDeploymentIncomplete(canary_failures)

    remaining = [
        authority
        for authority in authorities
        if authority.authority_id not in canary_ids
    ]
    lanes: dict[str, list[AuthoritySpec]] = {}
    for authority in remaining:
        lanes.setdefault(authority.canonical_agent, []).append(authority)
    lane_failures: list[dict[str, Any]] = []

    def deploy_lane(lane: Sequence[AuthoritySpec]) -> list[dict[str, Any]]:
        for authority in lane:
            try:
                deploy_stage([authority], concurrency=1)
            except AgentDeploymentIncomplete as error:
                return error.failures
        return []

    with ThreadPoolExecutor(
        max_workers=min(maximum_concurrency, len(lanes) or 1)
    ) as pool:
        futures = {
            pool.submit(deploy_lane, lane): agent
            for agent, lane in lanes.items()
        }
        for future in as_completed(futures):
            try:
                lane_failures.extend(future.result())
            except SharedRuntimeError:
                for pending in futures:
                    pending.cancel()
                raise
    if lane_failures:
        raise AgentDeploymentIncomplete(lane_failures)
    if len(deployed) != len(authorities):
        raise ContractError("Validation did not deploy every authority")
    return deployed


def _agent_failure_summary(
    authority: AuthoritySpec,
    *,
    stage: str,
    error: BaseException | None = None,
    error_code: str | None = None,
    request_accepted: bool | None,
) -> dict[str, Any]:
    raw_code = error_code or str(getattr(error, "code", "") or "")
    if not raw_code and error is not None:
        raw_code = type(error).__name__
    normalized = re.sub(
        r"[^a-z0-9]+",
        "_",
        re.sub(r"(?<!^)(?=[A-Z])", "_", raw_code).casefold(),
    ).strip("_")
    summary = {
        "canonical_agent": authority.canonical_agent,
        "authority_id": authority.authority_id,
        "stage": stage,
        "error_code": normalized[:64] or "unknown_error",
        "request_accepted": request_accepted,
    }
    correlation_counts = tuple(
        getattr(error, field, None)
        for field in (
            "matched_reference_count",
            "expected_reference_count",
            "missing_reference_count",
        )
    )
    if all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in correlation_counts
    ):
        matched, expected, missing = correlation_counts
        if matched + missing != expected:
            raise ContractError("Telemetry correlation failure counts are invalid")
        summary.update(
            {
                "matched_reference_count": matched,
                "expected_reference_count": expected,
                "missing_reference_count": missing,
            }
        )
    return summary


def _deployment_canaries(
    authorities: Sequence[AuthoritySpec],
) -> tuple[AuthoritySpec, AuthoritySpec]:
    prompt = sorted(
        (
            item
            for item in authorities
            if item.authority_kind == "baseline"
            and item.validation_mode == "baseline"
            and item.runtime_kind == "prompt"
        ),
        key=lambda item: item.authority_id,
    )
    hosted = sorted(
        (
            item
            for item in authorities
            if item.authority_kind == "baseline"
            and item.validation_mode == "baseline"
            and item.runtime_kind != "prompt"
        ),
        key=lambda item: item.authority_id,
    )
    if not prompt or not hosted:
        raise ContractError(
            "Validation deployment requires healthy Prompt and Hosted canaries"
        )
    return prompt[0], hosted[0]


def _deployment_intents(
    authority: AuthoritySpec,
    planned: PlannedRuntime,
) -> list[dict[str, Any]]:
    kinds = ["provider_agent", "provider_agent_version"]
    if authority.runtime_kind != "prompt":
        kinds.extend(
            [
                "hosted_identity",
                "hosted_blueprint",
                "hosted_deployment",
                "runtime_principal",
            ]
        )
    return [
        {
            "state": "create_intent",
            "kind": kind,
            "intent_reference": content_hash(
                {
                    "authority_id": authority.authority_id,
                    "runtime_agent_name": planned.runtime_agent_name,
                    "kind": kind,
                }
            ),
            "deterministic_name": (
                f"{planned.runtime_agent_name}/{authority.logical_version}"
                if kind == "provider_agent_version"
                else planned.runtime_agent_name
            ),
            "runtime_kind": authority.runtime_kind,
            "discovery_key": (
                f"{planned.runtime_agent_name}|{authority.logical_version}|{kind}"
            ),
            "authority_id": authority.authority_id,
            "parent_id": None,
            "cleanup_method": (
                "documented_project_cascade"
                if kind == "runtime_principal"
                else "explicit"
            ),
        }
        for kind in kinds
    ]


def execute_validation_matrix(
    authorities: Sequence[AuthoritySpec],
    deployed: Mapping[str, DeployedRuntime],
    *,
    runner: ScenarioAttemptRunner,
    scheduler: ValidationScheduler,
    model_contract: Mapping[str, Any],
    validated_commit_sha: str,
    record_failure: Callable[[dict[str, Any]], None] | None = None,
    paired_baselines: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    return execute_validation_phase(
        authorities,
        deployed,
        runner=runner,
        scheduler=scheduler,
        model_contract=model_contract,
        validated_commit_sha=validated_commit_sha,
        record_failure=record_failure,
        paired_baselines=paired_baselines,
    )


def execute_validation_phase(
    authorities: Sequence[AuthoritySpec],
    deployed: Mapping[str, DeployedRuntime],
    *,
    runner: ScenarioAttemptRunner,
    scheduler: ValidationScheduler,
    model_contract: Mapping[str, Any],
    validated_commit_sha: str,
    record_failure: Callable[[dict[str, Any]], None] | None = None,
    paired_baselines: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    by_id = {item.authority_id: item for item in authorities}
    local_baselines = {
        item.canonical_agent: item.authority_id
        for item in authorities
        if item.authority_kind == "baseline"
    }
    baseline_ids = dict(paired_baselines or local_baselines)
    phase_agents = {item.canonical_agent for item in authorities}
    if (
        not authorities
        or not phase_agents.issubset(baseline_ids)
        or not set(by_id).issubset(deployed)
        or any(
            baseline_ids[agent] not in deployed for agent in phase_agents
        )
    ):
        raise ContractError(
            "Validation phase requires its exact deployed Agent topology"
        )
    lanes: dict[str, list[AuthoritySpec]] = {}
    for authority in authorities:
        lanes.setdefault(authority.canonical_agent, []).append(authority)
    def execute_lane(
        lane: list[AuthoritySpec],
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        results = []
        for authority in lane:
            try:
                validate_validation_rules(
                    authority.validation_rules,
                    authority_id=authority.authority_id,
                    authority_kind=authority.authority_kind,
                    canonical_agent=authority.canonical_agent,
                    logical_version=authority.logical_version,
                    runtime_kind=authority.runtime_kind,
                    framework=authority.framework,
                    model_contract=model_contract,
                    reviewed_mode=authority.validation_mode,
                )
            except ContractError as error:
                return results, _agent_failure_summary(
                    authority,
                    stage="contract",
                    error=error,
                    request_accepted=False,
                )
            try:
                result = _execute_authority(
                    authority,
                    deployed=deployed,
                    paired_v0_id=baseline_ids[authority.canonical_agent],
                    runner=runner,
                    scheduler=scheduler,
                    model_contract=model_contract,
                    validated_commit_sha=validated_commit_sha,
                )
            except SharedRuntimeError:
                raise
            except (ContractError, OSError, RuntimeError) as error:
                return results, _agent_failure_summary(
                    authority,
                    stage="traffic",
                    error=error,
                    request_accepted=getattr(
                        error,
                        "request_accepted",
                        None,
                    ),
                )
            results.append(result)
            if not result["pass"]:
                return results, _result_failure_summary(authority, result)
        return results, None

    result_by_id: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(execute_lane, lane): agent_name
            for agent_name, lane in lanes.items()
        }
        for future in as_completed(futures):
            try:
                lane_results, failure = future.result()
            except SharedRuntimeError:
                for pending in futures:
                    pending.cancel()
                raise
            for result in lane_results:
                result_by_id[result["authority_id"]] = result
            if failure is not None:
                failures.append(failure)
                if record_failure is not None:
                    record_failure(failure)
    if failures:
        raise AgentExecutionIncomplete(
            failures,
            list(result_by_id.values()),
        )
    if len(result_by_id) != len(authorities):
        raise ContractError(
            "Validation execution did not complete every Agent lane"
        )
    return [result_by_id[item.authority_id] for item in authorities]


def _result_failure_summary(
    authority: AuthoritySpec,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    attempts = [
        attempt
        for scenario in result["scenarios"]
        for key in ("issue_attempts", "v0_attempts")
        for attempt in scenario[key]
    ]
    error_code = next(
        (
            str(item["error_code"])
            for item in attempts
            if item.get("error_code")
        ),
        "validation_contract_failed",
    )
    request_accepted = any(
        item.get("response_references") for item in attempts
    )
    return _agent_failure_summary(
        authority,
        stage="traffic",
        error_code=error_code,
        request_accepted=request_accepted,
    )


def _execute_authority(
    authority: AuthoritySpec,
    *,
    deployed: Mapping[str, DeployedRuntime],
    paired_v0_id: str,
    runner: ScenarioAttemptRunner,
    scheduler: ValidationScheduler,
    model_contract: Mapping[str, Any],
    validated_commit_sha: str,
) -> dict[str, Any]:
    validate_validation_rules(
        authority.validation_rules,
        authority_id=authority.authority_id,
        authority_kind=authority.authority_kind,
        canonical_agent=authority.canonical_agent,
        logical_version=authority.logical_version,
        runtime_kind=authority.runtime_kind,
        framework=authority.framework,
        model_contract=model_contract,
        reviewed_mode=authority.validation_mode,
    )
    scenarios = [
        _execute_scenario(
            authority,
            scenario,
            deployed=deployed,
            paired_v0_id=paired_v0_id,
            runner=runner,
            scheduler=scheduler,
        )
        for scenario in authority.validation_rules["scenarios"]
    ]
    n = sum(item["n"] for item in scenarios)
    k = sum(item["k"] for item in scenarios)
    authority_result = {
        "authority_id": authority.authority_id,
        "authority_kind": authority.authority_kind,
        "canonical_agent": authority.canonical_agent,
        "logical_version": authority.logical_version,
        "runtime_agent_name": deployed[
            authority.authority_id
        ].runtime_agent_name,
        "runtime_agent_version": deployed[
            authority.authority_id
        ].runtime_agent_version,
        "provider_agent_version_reference": content_hash(
            {
                "provider_agent_id": deployed[
                    authority.authority_id
                ].provider_agent_id,
                "provider_agent_version_id": deployed[
                    authority.authority_id
                ].provider_agent_version_id,
            }
        ),
        "source_content_digest": authority.source_content_digest,
        "execution_digest": authority.execution_digest,
        "predicate_contract_digest": "",
        "validated_commit_sha": validated_commit_sha,
        "n": n,
        "k": k,
        "complete_count": sum(item["complete_count"] for item in scenarios),
        "observed": sum(item["observed"] for item in scenarios),
        "pass": all(item["pass"] for item in scenarios),
        "scenarios": scenarios,
        "authority_evidence_digest": "",
    }
    authority_result["predicate_contract_digest"] = (
        authority_predicate_contract_digest(authority_result)
    )
    authority_result["authority_evidence_digest"] = digest_without_field(
        authority_result,
        "authority_evidence_digest",
    )
    return authority_result


def invalidated_authorities(
    *,
    current_cycle_id: str,
    previous_cycle_id: str,
    previous_contract_digest: str,
    current_contract_digest: str,
    previous_source_digests: Mapping[str, str],
    current_source_digests: Mapping[str, str],
) -> set[str]:
    if current_cycle_id != previous_cycle_id:
        return set(current_source_digests)
    if previous_contract_digest != current_contract_digest:
        return set(current_source_digests)
    if set(previous_source_digests) != set(current_source_digests):
        return set(current_source_digests)
    return {
        authority_id
        for authority_id, digest in current_source_digests.items()
        if previous_source_digests[authority_id] != digest
    }


def _execute_scenario(
    authority: AuthoritySpec,
    scenario: Mapping[str, Any],
    *,
    deployed: Mapping[str, DeployedRuntime],
    paired_v0_id: str,
    runner: ScenarioAttemptRunner,
    scheduler: ValidationScheduler,
) -> dict[str, Any]:
    issue_attempts = [
        runner.run(
            target=deployed[authority.authority_id],
            executing_authority_id=authority.authority_id,
            conversation_role=(
                "baseline" if authority.authority_kind == "baseline" else "issue"
            ),
            scenario=scenario,
            attempt=attempt,
            expect_defect=authority.authority_kind == "issue",
            scheduler=scheduler,
        )
        for attempt in scenario["attempts"]
    ]
    v0_attempts = (
        []
        if authority.authority_kind == "baseline"
        else [
            runner.run(
                target=deployed[paired_v0_id],
                executing_authority_id=authority.authority_id,
                conversation_role="paired_v0",
                scenario=scenario,
                attempt=attempt,
                expect_defect=False,
                scheduler=scheduler,
            )
            for attempt in scenario["attempts"]
        ]
    )
    n = int(scenario["n"])
    k = int(scenario["k"])
    complete_count = sum(item["complete"] is True for item in issue_attempts)
    observed = sum(item["defect_observed"] is True for item in issue_attempts)
    if authority.authority_kind == "baseline":
        passed = (
            complete_count == n
            and observed == 0
            and all(item["expected_observation_pass"] for item in issue_attempts)
        )
    else:
        passed = (
            complete_count == n
            and observed >= k
            and sum(item["complete"] is True for item in v0_attempts) == n
            and not any(item["defect_observed"] is True for item in v0_attempts)
            and all(item["expected_observation_pass"] for item in v0_attempts)
        )
    result = {
        "scenario_id": scenario["id"],
        "execution_digest": scenario["execution_digest"],
        "validation_mode": scenario["validation_mode"],
        "healthy_predicate": scenario["healthy_predicate"],
        "defect_predicate": scenario["defect_predicate"],
        "v0_control_predicate": scenario["v0_control_predicate"],
        "predicate_contract_digest": "",
        "n": n,
        "k": k,
        "complete_count": complete_count,
        "observed": observed,
        "pass": passed,
        "issue_attempts": issue_attempts,
        "v0_attempts": v0_attempts,
    }
    result["predicate_contract_digest"] = scenario_predicate_contract_digest(
        result
    )
    return result


def _bounded_name(value: str, *, maximum: int, pattern: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.casefold())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    if len(normalized) > maximum:
        suffix = hashlib.sha256(normalized.encode("ascii")).hexdigest()[:10]
        normalized = normalized[: maximum - len(suffix) - 1].rstrip("-")
        normalized = f"{normalized}-{suffix}"
    if re.fullmatch(pattern, normalized) is None:
        raise ContractError("Generated validation resource name violates policy")
    return normalized
