from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_insights_quality.catalogs import catalog_hashes
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    canonical_bytes,
    content_hash,
    file_hash,
    read_json,
)
from agent_insights_quality.validation_policy import ValidationPolicy
from agent_insights_quality.validation_quota import EndpointCost
from agent_insights_quality.validation_runtime import (
    AuthoritySpec,
    opaque_cycle_suffix,
    plan_runtime_topology,
    validation_project_name,
)


def authority_specs(
    agents: Mapping[str, Any],
    issues: Mapping[str, Any],
) -> list[AuthoritySpec]:
    issue_by_id = {item["id"]: item for item in issues["issues"]}
    values: list[AuthoritySpec] = []
    for agent in agents["agents"]:
        values.append(_authority(agent, None))
        values.extend(
            _authority(agent, issue_by_id[issue_id])
            for issue_id in agent["issue_ids"]
        )
    if len(values) != 41:
        raise ContractError("Validation authority manifest must contain exactly 41 items")
    return values


def prepare_validation_plan(
    *,
    agents: Mapping[str, Any],
    issues: Mapping[str, Any],
    policy: ValidationPolicy,
    repository: str,
    pr_number: int,
    commit_sha: str,
    local_run_id: str,
) -> dict[str, Any]:
    if repository != policy.repository:
        raise ContractError("Repository does not match validation policy")
    if (
        pr_number < 1
        or not _git_sha(commit_sha)
        or not local_run_id
    ):
        raise ContractError("Local validation identity is invalid")
    authorities = authority_specs(agents, issues)
    suffix = opaque_cycle_suffix(
        repository=repository,
        pr_number=pr_number,
        commit_sha=commit_sha,
        run_id=local_run_id,
    )
    topology = plan_runtime_topology(
        authorities,
        cycle_suffix=suffix,
        policy=policy,
    )
    execution_digests = {
        item.authority_id: item.execution_digest for item in authorities
    }
    costs = validation_endpoint_costs(authorities)
    attempt_count = sum(
        len(scenario["attempts"])
        * (2 if authority.authority_kind == "issue" else 1)
        for authority in authorities
        for scenario in authority.validation_rules["scenarios"]
    )
    validation_digest = current_validation_digest(agents, issues)
    return {
        "schema_version": "1.0.0",
        "kind": "test-agent-validation-plan",
        "cycle_id": f"validation-{suffix}",
        "repository": repository,
        "pr_number": pr_number,
        "commit_sha": commit_sha,
        "project_name": validation_project_name(suffix, policy=policy),
        "telemetry_resource_set": policy.telemetry_resource_set,
        "test_agent_model": policy.test_agent_model,
        "validation_digest": validation_digest,
        "execution_matrix_digest": content_hash(execution_digests),
        "planned_topology_digest": content_hash(
            [
                {
                    "authority_id": item.authority_id,
                    "runtime_agent_name": item.runtime_agent_name,
                    "runtime_kind": item.runtime_kind,
                    "framework": item.framework,
                }
                for item in topology
            ]
        ),
        "endpoint_envelope": {
            "attempts": attempt_count,
            "requests": sum(item.requests for item in costs),
            "worst_case_tokens": max(item.tokens for item in costs),
            "worst_case_inner_model_calls": max(
                item.inner_model_calls for item in costs
            ),
        },
        "authorities": [
            {
                "authority_id": item.authority_id,
                "authority_kind": authority.authority_kind,
                "canonical_agent": item.canonical_agent,
                "logical_version": item.logical_version,
                "runtime_kind": item.runtime_kind,
                "framework": item.framework,
                "runtime_agent_name": item.runtime_agent_name,
                "source_content_digest": authority.source_content_digest,
                "execution_digest": authority.execution_digest,
                "validation_mode": authority.validation_mode,
            }
            for item, authority in zip(topology, authorities, strict=True)
        ],
    }


def validate_validation_plan(
    value: Mapping[str, Any],
    *,
    agents: Mapping[str, Any],
    issues: Mapping[str, Any],
    policy: ValidationPolicy,
) -> None:
    current = {
        item.authority_id: item for item in authority_specs(agents, issues)
    }
    authorities = value.get("authorities")
    if (
        value.get("kind") != "test-agent-validation-plan"
        or value.get("repository") != policy.repository
        or value.get("telemetry_resource_set") != "g29"
        or not isinstance(authorities, list)
        or len(authorities) != 41
        or {
            item.get("authority_id")
            for item in authorities
            if isinstance(item, Mapping)
        }
        != set(current)
    ):
        raise ContractError("Local validation plan inventory is invalid")
    for item in authorities:
        authority_id = item["authority_id"]
        expected = current[authority_id]
        if (
            item["source_content_digest"] != expected.source_content_digest
            or item["execution_digest"] != expected.execution_digest
            or item["validation_mode"] != expected.validation_mode
            or item["runtime_kind"] != expected.runtime_kind
            or item["framework"] != expected.framework
        ):
            raise ContractError(
                f"{authority_id} plan no longer matches repository"
            )
    execution = {
        authority_id: item.execution_digest
        for authority_id, item in current.items()
    }
    if value.get("execution_matrix_digest") != content_hash(execution):
        raise ContractError("Local validation execution matrix is stale")
    if value.get("validation_digest") != current_validation_digest(agents, issues):
        raise ContractError("Local validation contract is stale")


def current_validation_digest(
    agents: Mapping[str, Any],
    issues: Mapping[str, Any],
) -> str:
    sources = {
        item.authority_id: item.source_content_digest
        for item in authority_specs(agents, issues)
    }
    return content_hash(
        {
            "contracts": {
                path.relative_to(ROOT).as_posix(): file_hash(path)
                for path in _validation_contract_files()
            },
            "catalog_hashes": catalog_hashes(dict(agents), dict(issues)),
            "source_content_digests": sources,
        }
    )


def _validation_contract_files() -> list[Path]:
    return [
        ROOT / "config" / "test-agent-validation.yaml",
        *sorted(ROOT.glob("schemas/test-agent-validation-*.schema.json")),
        ROOT / "schemas" / "agent-catalog.schema.json",
        ROOT / "schemas" / "issue-catalog.schema.json",
        ROOT / "schemas" / "prompt-traffic.schema.json",
        ROOT / "src" / "agent_insights_quality" / "live.py",
        ROOT / "src" / "agent_insights_quality" / "provisioning.py",
        ROOT / "src" / "agent_insights_quality" / "profiles.py",
        ROOT / "src" / "agent_insights_quality" / "util.py",
        *sorted(
            (ROOT / "src" / "agent_insights_quality").glob("validation_*.py")
        ),
        ROOT / "infra" / "modules" / "validation-project.bicep",
    ]


def validation_endpoint_costs(
    authorities: list[AuthoritySpec],
) -> list[EndpointCost]:
    costs: list[EndpointCost] = []
    for authority in authorities:
        for scenario in authority.validation_rules["scenarios"]:
            for attempt in scenario["attempts"]:
                steps = [*attempt["setup_steps"], *attempt["probe_steps"]]
                for step in steps:
                    cost = validation_step_cost(authority.framework, step)
                    costs.append(cost)
                    if authority.authority_kind == "issue":
                        costs.append(cost)
    return costs


def validation_authority_cost(authority: AuthoritySpec) -> EndpointCost:
    costs = [
        validation_step_cost(authority.framework, step)
        for scenario in authority.validation_rules["scenarios"]
        for attempt in scenario["attempts"]
        for step in [*attempt["setup_steps"], *attempt["probe_steps"]]
    ]
    if not costs:
        raise ContractError("Validation authority has no endpoint request costs")
    return EndpointCost(
        requests=1,
        tokens=max(item.tokens for item in costs),
        inner_model_calls=max(item.inner_model_calls for item in costs),
    )


def validation_step_cost(
    framework: str,
    step: Mapping[str, Any],
) -> EndpointCost:
    fanout = {
        "foundry_prompt": 1,
        "microsoft_agent_framework": 4,
        "langgraph": 1,
        "custom_responses": 1,
    }.get(framework)
    if fanout is None:
        raise ContractError("Validation framework has no reviewed cost model")
    body = step["request"]["body"]
    maximum_output = int(body.get("max_output_tokens", 400))
    if maximum_output <= 0:
        raise ContractError("Validation output token budget must be positive")
    input_budget = len(canonical_bytes(body))
    return EndpointCost(
        requests=1,
        tokens=(input_budget + maximum_output) * fanout,
        inner_model_calls=fanout,
    )


def _authority(
    agent: Mapping[str, Any],
    issue: Mapping[str, Any] | None,
) -> AuthoritySpec:
    root = ROOT / (
        agent["baseline_path"] if issue is None else issue["implementation"]
    )
    traffic = read_json(root / "traffic.json")
    rules = traffic["validation_rules"]
    logical_version = "v0" if issue is None else issue["id"]
    authority_id = (
        f"{agent['name']}/v0" if issue is None else issue["id"]
    )
    return AuthoritySpec(
        authority_id=authority_id,
        authority_kind="baseline" if issue is None else "issue",
        canonical_agent=agent["name"],
        logical_version=logical_version,
        runtime_kind=agent["type"],
        framework=agent["framework"],
        source_content_digest=source_content_digest(root, agent["type"]),
        execution_digest=rules["execution_digest"],
        validation_mode=(
            agent["baseline_contract"]["validation_mode"]
            if issue is None
            else issue["validation_mode"]
        ),
        validation_rules=rules,
    )


def source_content_digest(root: Path, runtime_kind: str) -> str:
    if runtime_kind == "prompt":
        return content_hash(read_json(root / "definition.json"))
    source = root / "source"
    files = {
        path.relative_to(root).as_posix(): file_hash(path)
        for path in sorted(source.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix.casefold() not in {".pyc", ".pyo"}
    }
    baseline = root if root.name == "v0" else root.parents[1] / "v0"
    for name in (
        "requirements.txt",
        "host.yaml",
        "container.yaml",
        "Dockerfile",
    ):
        path = baseline / name
        if path.is_file():
            files[f"deployment/{name}"] = file_hash(path)
    return content_hash(files)


def _git_sha(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdef" for character in value)
