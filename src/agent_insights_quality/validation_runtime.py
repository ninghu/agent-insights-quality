from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from agent_insights_quality.util import (
    ContractError,
    SharedRuntimeError,
    content_hash,
)
from agent_insights_quality.validation_evidence import (
    digest_without_field,
    role_pass_attempt_payload,
    scenario_evidence_complete,
)
from agent_insights_quality.validation_trace_gap_policy import (
    role_pass_summary,
    target_evidence_decided,
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
    intent_namespace: str = ""
    version_ordinal: int = 0


@dataclass(frozen=True)
class DeployedRuntime:
    authority_id: str
    runtime_kind: str
    runtime_agent_name: str
    runtime_agent_version: str
    provider_agent_id: str
    provider_agent_version_id: str
    provider_content_digest: str
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

    def invoke(
        self,
        *,
        target: DeployedRuntime,
        executing_authority_id: str,
        conversation_role: str,
        scenario: Mapping[str, Any],
        attempt: Mapping[str, Any],
        scheduler: ValidationScheduler,
    ) -> dict[str, Any]: ...

    def verify(
        self,
        *,
        target: DeployedRuntime,
        executing_authority_id: str,
        conversation_role: str,
        scenario: Mapping[str, Any],
        attempt: Mapping[str, Any],
        invocation: Mapping[str, Any],
        scheduler: ValidationScheduler,
    ) -> dict[str, Any]: ...

    def verify_attempts(
        self,
        *,
        target: DeployedRuntime,
        executing_authority_id: str,
        conversation_role: str,
        scenario: Mapping[str, Any],
        attempts: list[Mapping[str, Any]],
        invocations: list[Mapping[str, Any]],
        scheduler: ValidationScheduler,
    ) -> list[dict[str, Any]]: ...


def opaque_run_suffix(
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
    run_suffix: str,
    *,
    policy: ValidationPolicy,
) -> str:
    del run_suffix
    return _bounded_name(
        policy.project_name,
        maximum=policy.project_name_policy.maximum_length,
        pattern=policy.project_name_policy.pattern,
    )


def validation_agent_name(
    *,
    canonical_agent: str,
    logical_version: str,
    run_suffix: str,
    policy: ValidationPolicy,
) -> str:
    del run_suffix
    qualifier = (
        "baseline"
        if logical_version == "v0"
        else logical_version
    )
    return _bounded_name(
        f"{canonical_agent}-{qualifier}",
        maximum=policy.agent_name_policy.maximum_length,
        pattern=policy.agent_name_policy.pattern,
    )


def plan_runtime_topology(
    authorities: Sequence[AuthoritySpec],
    *,
    run_suffix: str,
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
                run_suffix=run_suffix,
                policy=policy,
            ),
            intent_namespace=run_suffix,
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
    def deploy(authority: AuthoritySpec) -> DeployedRuntime:
        target = by_id[authority.authority_id]
        intents = deployment_intents(authority, target)
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
        del concurrency
        pending = [
            authority
            for authority in stage
            if authority.authority_id not in deployed
        ]
        while pending:
            failures: list[tuple[AuthoritySpec, ContractError]] = []
            for authority in pending:
                try:
                    value = deploy(authority)
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

    deploy_stage(authorities, concurrency=maximum_concurrency)
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


def deployment_intents(
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
    events = []
    for kind in kinds:
        identity: dict[str, Any] = {
            "authority_id": authority.authority_id,
            "runtime_agent_name": planned.runtime_agent_name,
            "kind": kind,
        }
        if kind != "provider_agent":
            identity.update(
                {
                    "intent_namespace": planned.intent_namespace,
                    "version_ordinal": planned.version_ordinal,
                }
            )
        intent_reference = content_hash(identity)
        events.append(
            {
            "state": "create_intent",
            "kind": kind,
            "intent_reference": intent_reference,
            "deterministic_name": (
                f"{planned.runtime_agent_name}/{authority.logical_version}"
                if kind == "provider_agent_version"
                else planned.runtime_agent_name
            ),
            "runtime_kind": authority.runtime_kind,
            "discovery_key": (
                f"{planned.runtime_agent_name}|{authority.logical_version}|{kind}|"
                f"{intent_reference}"
                if kind != "provider_agent"
                else planned.runtime_agent_name
            ),
            "authority_id": authority.authority_id,
            "parent_id": None,
            "retention": "retained",
            }
        )
    return events


def deployment_resource_events(
    authority: AuthoritySpec,
    planned: PlannedRuntime,
    runtime: DeployedRuntime,
) -> list[dict[str, Any]]:
    events = deployment_intents(authority, planned)
    observed = {
        "provider_agent": runtime.provider_agent_id,
        "provider_agent_version": runtime.provider_agent_version_id,
        "hosted_identity": runtime.hosted_identity_id,
        "hosted_blueprint": runtime.hosted_blueprint_id,
        "hosted_deployment": runtime.hosted_deployment_id,
        "runtime_principal": runtime.runtime_principal_id,
    }
    result: list[dict[str, Any]] = []
    for event in events:
        result.append(event)
        provider_id = observed[event["kind"]]
        if provider_id is None:
            if authority.runtime_kind != "prompt":
                raise ContractError(
                    "Hosted validation deployment resource is missing"
                )
            continue
        result.append(
            {
                **event,
                "state": "created",
                "provider_id": provider_id,
                "deterministic_name": (
                    f"{runtime.runtime_agent_name}/"
                    f"{runtime.runtime_agent_version}"
                    if event["kind"] == "provider_agent_version"
                    else runtime.runtime_agent_name
                ),
            }
        )
    return result


def invoke_validation_shard(
    authorities: Sequence[AuthoritySpec],
    deployed: Mapping[str, DeployedRuntime],
    *,
    runner: ScenarioAttemptRunner,
    scheduler: ValidationScheduler,
    model_contract: Mapping[str, Any],
    paired_baselines: Mapping[str, str],
    record_authority: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    if not authorities:
        raise ContractError("Validation shard authority assignment is empty")
    results: list[dict[str, Any]] = []
    for authority in authorities:
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
        baseline_id = paired_baselines.get(authority.canonical_agent)
        if (
            authority.authority_id not in deployed
            or baseline_id is None
            or baseline_id not in deployed
        ):
            raise ContractError(
                "Validation shard requires its exact prepared Agent topology"
            )
        scenarios = []
        for scenario in authority.validation_rules["scenarios"]:
            issue_invocations = [
                runner.invoke(
                    target=deployed[authority.authority_id],
                    executing_authority_id=authority.authority_id,
                    conversation_role=(
                        "baseline"
                        if authority.authority_kind == "baseline"
                        else "issue"
                    ),
                    scenario=scenario,
                    attempt=attempt,
                    scheduler=scheduler,
                )
                for attempt in scenario["attempts"]
            ]
            paired_invocations = (
                []
                if authority.authority_kind == "baseline"
                else [
                    runner.invoke(
                        target=deployed[baseline_id],
                        executing_authority_id=authority.authority_id,
                        conversation_role="paired_v0",
                        scenario=scenario,
                        attempt=attempt,
                        scheduler=scheduler,
                    )
                    for attempt in scenario["attempts"]
                ]
            )
            scenarios.append(
                {
                    "scenario_id": scenario["id"],
                    "issue_invocations": issue_invocations,
                    "v0_invocations": paired_invocations,
                }
            )
        result = {
            "authority_id": authority.authority_id,
            "scenarios": scenarios,
        }
        results.append(result)
        if record_authority is not None:
            record_authority(result)
    return results


def verify_validation_shard(
    authorities: Sequence[AuthoritySpec],
    deployed: Mapping[str, DeployedRuntime],
    invocations: Sequence[Mapping[str, Any]],
    *,
    runner: ScenarioAttemptRunner,
    scheduler: ValidationScheduler,
    model_contract: Mapping[str, Any],
    validated_commit_sha: str,
    paired_baselines: Mapping[str, str],
) -> list[dict[str, Any]]:
    by_authority = {
        str(item.get("authority_id") or ""): item for item in invocations
    }
    if (
        len(by_authority) != len(invocations)
        or set(by_authority) != {item.authority_id for item in authorities}
    ):
        raise ContractError("Validation shard invocation authority coverage is invalid")
    return [
        _verify_invoked_authority(
            authority,
            deployed=deployed,
            invocation=by_authority[authority.authority_id],
            paired_v0_id=paired_baselines[authority.canonical_agent],
            runner=runner,
            scheduler=scheduler,
            model_contract=model_contract,
            validated_commit_sha=validated_commit_sha,
        )
        for authority in authorities
    ]


def _verify_invoked_authority(
    authority: AuthoritySpec,
    *,
    deployed: Mapping[str, DeployedRuntime],
    invocation: Mapping[str, Any],
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
    scenario_invocations = invocation.get("scenarios")
    if not isinstance(scenario_invocations, list):
        raise ContractError("Validation shard invocation scenarios are invalid")
    by_scenario = {
        str(item.get("scenario_id") or ""): item
        for item in scenario_invocations
        if isinstance(item, Mapping)
    }
    if len(by_scenario) != len(scenario_invocations):
        raise ContractError("Validation shard invocation scenarios collide")
    scenarios = []
    for scenario in authority.validation_rules["scenarios"]:
        persisted = by_scenario.get(str(scenario["id"]))
        if persisted is None:
            raise ContractError("Validation shard invocation scenario is missing")
        issue_invocations = persisted.get("issue_invocations")
        v0_invocations = persisted.get("v0_invocations")
        attempts = scenario["attempts"]
        if (
            not isinstance(issue_invocations, list)
            or not isinstance(v0_invocations, list)
            or len(issue_invocations) != len(attempts)
            or (
                authority.authority_kind == "baseline"
                and v0_invocations
            )
            or (
                authority.authority_kind == "issue"
                and len(v0_invocations) != len(attempts)
            )
        ):
            raise ContractError("Validation shard invocation attempt coverage is invalid")
        verify_attempts = getattr(runner, "verify_attempts", None)
        issue_attempts = (
            verify_attempts(
                target=deployed[authority.authority_id],
                executing_authority_id=authority.authority_id,
                conversation_role=(
                    "baseline" if authority.authority_kind == "baseline" else "issue"
                ),
                scenario=scenario,
                attempts=list(attempts),
                invocations=list(issue_invocations),
                scheduler=scheduler,
            )
            if callable(verify_attempts)
            else [
                runner.verify(
                    target=deployed[authority.authority_id],
                    executing_authority_id=authority.authority_id,
                    conversation_role=(
                        "baseline"
                        if authority.authority_kind == "baseline"
                        else "issue"
                    ),
                    scenario=scenario,
                    attempt=attempt,
                    invocation=persisted_invocation,
                    scheduler=scheduler,
                )
                for attempt, persisted_invocation in zip(
                    attempts,
                    issue_invocations,
                    strict=True,
                )
            ]
        )
        paired_attempts = (
            []
            if authority.authority_kind == "baseline"
            else (
                verify_attempts(
                    target=deployed[paired_v0_id],
                    executing_authority_id=authority.authority_id,
                    conversation_role="paired_v0",
                    scenario=scenario,
                    attempts=list(attempts),
                    invocations=list(v0_invocations),
                    scheduler=scheduler,
                )
                if callable(verify_attempts)
                else [
                    runner.verify(
                        target=deployed[paired_v0_id],
                        executing_authority_id=authority.authority_id,
                        conversation_role="paired_v0",
                        scenario=scenario,
                        attempt=attempt,
                        invocation=persisted_invocation,
                        scheduler=scheduler,
                    )
                    for attempt, persisted_invocation in zip(
                        attempts,
                        v0_invocations,
                        strict=True,
                    )
                ]
            )
        )
        n = int(scenario["n"])
        k = int(scenario["k"])
        complete_count = sum(item["complete"] is True for item in issue_attempts)
        paired_complete_count = sum(
            item["complete"] is True for item in paired_attempts
        )
        observation_count = sum(
            item.get("observation", item["complete"]) is True
            for item in issue_attempts
        )
        paired_observation_count = sum(
            item.get("observation", False) is True for item in paired_attempts
        )
        primary_role = (
            "baseline" if authority.authority_kind == "baseline" else "issue"
        )
        primary_summary = role_pass_summary(
            target_role=primary_role,
            n=n,
            k=k,
            attempts=[
                role_pass_attempt_payload(
                    {
                        **item,
                        "index": index,
                        "observation": item.get(
                            "observation",
                            item["complete"],
                        ),
                    }
                )
                for index, item in enumerate(issue_attempts, start=1)
            ],
        )
        paired_summary = (
            None
            if authority.authority_kind == "baseline"
            else role_pass_summary(
                target_role="paired_v0",
                n=n,
                k=k,
                attempts=[
                    role_pass_attempt_payload(
                        {
                            **item,
                            "index": index,
                            "observation": item.get(
                                "observation",
                                item["complete"],
                            ),
                        }
                    )
                    for index, item in enumerate(paired_attempts, start=1)
                ],
            )
        )
        if primary_summary is None or (
            authority.authority_kind != "baseline" and paired_summary is None
        ):
            raise ContractError(
                f"{authority.authority_id}/{scenario['id']} role-pass "
                "evidence is invalid"
            )
        role_pass_count = int(primary_summary["pass_count"])
        paired_role_pass_count = (
            0 if paired_summary is None else int(paired_summary["pass_count"])
        )
        evidence_complete = scenario_evidence_complete(
            authority_kind=authority.authority_kind,
            n=n,
            k=k,
            complete_count=complete_count,
            paired_complete_count=paired_complete_count,
            role_pass_count=role_pass_count,
            paired_role_pass_count=paired_role_pass_count,
        )
        scenarios.append(
            {
                "scenario_id": scenario["id"],
                "execution_digest": scenario["execution_digest"],
                "validation_mode": scenario["validation_mode"],
                "n": n,
                "k": k,
                "complete_count": complete_count,
                "paired_complete_count": paired_complete_count,
                "observation_count": observation_count,
                "paired_observation_count": paired_observation_count,
                "role_pass_count": role_pass_count,
                "paired_role_pass_count": paired_role_pass_count,
                "evidence_complete": evidence_complete,
                "pass": evidence_complete
                and target_evidence_decided(
                    n=n,
                    k=k,
                    role_pass_count=role_pass_count,
                )
                and (
                    authority.authority_kind == "baseline"
                    or target_evidence_decided(
                        n=n,
                        k=k,
                        role_pass_count=paired_role_pass_count,
                    )
                ),
                "primary_role_pass_summary": primary_summary,
                "paired_role_pass_summary": paired_summary,
                "issue_attempts": issue_attempts,
                "v0_attempts": paired_attempts,
            }
        )
    n = sum(item["n"] for item in scenarios)
    k = sum(item["k"] for item in scenarios)
    result = {
        "authority_id": authority.authority_id,
        "authority_kind": authority.authority_kind,
        "canonical_agent": authority.canonical_agent,
        "logical_version": authority.logical_version,
        "runtime_agent_name": deployed[authority.authority_id].runtime_agent_name,
        "runtime_agent_version": deployed[
            authority.authority_id
        ].runtime_agent_version,
        "provider_agent_version_reference": content_hash(
            {
                "provider_agent_id": deployed[authority.authority_id].provider_agent_id,
                "provider_agent_version_id": deployed[
                    authority.authority_id
                ].provider_agent_version_id,
            }
        ),
        "runtime_mapping_digest": content_hash(
            {
                "runtime_agent_name": deployed[
                    authority.authority_id
                ].runtime_agent_name,
                "runtime_agent_version": deployed[
                    authority.authority_id
                ].runtime_agent_version,
                "provider_agent_id": deployed[
                    authority.authority_id
                ].provider_agent_id,
                "provider_agent_version_id": deployed[
                    authority.authority_id
                ].provider_agent_version_id,
                "hosted_identity_id": deployed[
                    authority.authority_id
                ].hosted_identity_id,
                "hosted_blueprint_id": deployed[
                    authority.authority_id
                ].hosted_blueprint_id,
                "hosted_deployment_id": deployed[
                    authority.authority_id
                ].hosted_deployment_id,
                "runtime_principal_id": deployed[
                    authority.authority_id
                ].runtime_principal_id,
                "telemetry_identity_id": deployed[
                    authority.authority_id
                ].telemetry_identity_id,
                "connection_ids": list(
                    deployed[authority.authority_id].connection_ids
                ),
            }
        ),
        "provider_content_digest": deployed[
            authority.authority_id
        ].provider_content_digest,
        "source_content_digest": authority.source_content_digest,
        "execution_digest": authority.execution_digest,
        "validated_commit_sha": validated_commit_sha,
        "n": n,
        "k": k,
        "complete_count": sum(item["complete_count"] for item in scenarios),
        "paired_complete_count": sum(
            item["paired_complete_count"] for item in scenarios
        ),
        "observation_count": sum(
            item["observation_count"] for item in scenarios
        ),
        "paired_observation_count": sum(
            item["paired_observation_count"] for item in scenarios
        ),
        "role_pass_count": sum(item["role_pass_count"] for item in scenarios),
        "paired_role_pass_count": sum(
            item["paired_role_pass_count"] for item in scenarios
        ),
        "evidence_complete": all(
            item["evidence_complete"] for item in scenarios
        ),
        "pass": all(item["pass"] for item in scenarios),
        "scenarios": scenarios,
        "authority_evidence_digest": "",
    }
    result["authority_evidence_digest"] = digest_without_field(
        result,
        "authority_evidence_digest",
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
