from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from agent_insights_quality.util import ContractError, content_hash
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
) -> dict[str, DeployedRuntime]:
    if maximum_concurrency < 1 or maximum_concurrency > 8:
        raise ContractError("Validation provisioning concurrency must be between 1 and 8")
    by_id = {item.authority_id: item for item in planned}
    if set(by_id) != {item.authority_id for item in authorities}:
        raise ContractError("Planned validation topology is incomplete")
    deployed: dict[str, DeployedRuntime] = {}
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

    prompt_canary, hosted_canary = _deployment_canaries(authorities)
    canary_ids = {
        prompt_canary.authority_id,
        hosted_canary.authority_id,
    }
    for canary in (prompt_canary, hosted_canary):
        value = deploy(canary)
        deployer.assert_ready(canary, value)
        accept(canary.authority_id, value)

    remaining = [
        authority
        for authority in authorities
        if authority.authority_id not in canary_ids
    ]
    with ThreadPoolExecutor(max_workers=maximum_concurrency) as pool:
        futures = {
            pool.submit(deploy, authority): authority.authority_id
            for authority in remaining
        }
        for future in as_completed(futures):
            authority_id = futures[future]
            value = future.result()
            accept(authority_id, value)
    if len(deployed) != len(authorities):
        raise ContractError("Validation did not deploy every authority")
    return deployed


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
) -> list[dict[str, Any]]:
    by_id = {item.authority_id: item for item in authorities}
    baseline_ids = {
        item.canonical_agent: item.authority_id
        for item in authorities
        if item.authority_kind == "baseline"
    }
    if len(by_id) != 41 or len(baseline_ids) != 5 or set(deployed) != set(by_id):
        raise ContractError("Validation execution requires the exact deployed topology")
    lanes: dict[str, list[AuthoritySpec]] = {}
    for authority in authorities:
        lanes.setdefault(authority.canonical_agent, []).append(authority)
    if len(lanes) != 5:
        raise ContractError("Validation execution requires five Agent lanes")

    def execute_lane(lane: list[AuthoritySpec]) -> list[dict[str, Any]]:
        return [
            _execute_authority(
                authority,
                deployed=deployed,
                paired_v0_id=baseline_ids[authority.canonical_agent],
                runner=runner,
                scheduler=scheduler,
                model_contract=model_contract,
                validated_commit_sha=validated_commit_sha,
            )
            for authority in lane
        ]

    result_by_id: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(execute_lane, lane): agent_name
            for agent_name, lane in lanes.items()
        }
        for future in as_completed(futures):
            for result in future.result():
                result_by_id[result["authority_id"]] = result
    if len(result_by_id) != 41:
        raise ContractError("Validation execution did not complete every Agent lane")
    return [result_by_id[item.authority_id] for item in authorities]


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
