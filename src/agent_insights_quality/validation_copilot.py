from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from agent_insights_quality.util import (
    ROOT,
    ContractError,
    atomic_json,
    content_hash,
    immutable_json,
    read_json,
)
from agent_insights_quality.validation_assignments import verification_assignment
from agent_insights_quality.validation_evidence import (
    digest_without_field,
    runtime_mapping_digest,
    validate_authority_evidence,
)
from agent_insights_quality.validation_lifecycle import (
    LocalValidationLock,
    validation_runtime_root,
)
from agent_insights_quality.validation_runtime import AuthoritySpec, DeployedRuntime

COPILOT_MODEL = "gpt-5.6-sol"
EVALUATION_SCHEMA = (
    ROOT / "schemas" / "test-agent-validation-copilot-evaluation.schema.json"
)
EVALUATION_PROMPT = (
    ROOT
    / "src"
    / "agent_insights_quality"
    / "prompts"
    / "test_agent_validation.md"
)
_PACKAGE_KIND = "test-agent-validation-private-package"
_EVALUATION_KIND = "test-agent-validation-copilot-evaluation"


def evaluation_root(root: Path | None = None) -> Path:
    return (
        root
        or validation_runtime_root() / "copilot-authority-evaluations"
    ).resolve()


def evaluation_lock(root: Path | None = None) -> LocalValidationLock:
    return LocalValidationLock(evaluation_root(root) / "evaluation.lock")


def assessment_path(
    package_hash: str,
    *,
    root: Path | None = None,
) -> Path:
    return (
        evaluation_root(root)
        / "assessments"
        / f"{package_hash.removeprefix('sha256:')}.json"
    )


def write_private_package(
    *,
    prepared: Mapping[str, Any],
    plan: Mapping[str, Any],
    authority: AuthoritySpec,
    runtime: Mapping[str, Any],
    paired_v0_runtime: Mapping[str, Any],
    deployed: DeployedRuntime,
    paired_v0_deployed: DeployedRuntime,
    invocation_reference: Mapping[str, str],
    invocation_receipt: Mapping[str, Any],
    collector: Any,
    scheduler: Any,
    started_at: datetime,
    fence: Callable[[], None],
    root: Path | None = None,
) -> dict[str, Any]:
    fence()
    targets = _collect_targets(
        authority=authority,
        deployed=deployed,
        paired_v0_deployed=paired_v0_deployed,
        invocation=invocation_receipt["invocation"],
        collector=collector,
        scheduler=scheduler,
    )
    fence()
    package = {
        "schema_version": "1.0.0",
        "kind": _PACKAGE_KIND,
        "model": COPILOT_MODEL,
        "repository": prepared["repository"],
        "pr_number": prepared["pr_number"],
        "origin_run_id": prepared["run_id"],
        "origin_commit_sha": prepared["commit_sha"],
        "authority_id": authority.authority_id,
        "created_at": started_at.astimezone(UTC).isoformat(),
        "prompt_digest": content_hash(
            EVALUATION_PROMPT.read_text(encoding="utf-8")
        ),
        "binding": _package_binding(
            prepared=prepared,
            plan=plan,
            authority=authority,
            runtime=runtime,
            paired_v0_runtime=paired_v0_runtime,
            invocation_reference=invocation_reference,
        ),
        "authority_contract": _authority_contract(authority, runtime),
        "validation_rules": copy.deepcopy(authority.validation_rules),
        "invocation_receipt": {
            "reference": copy.deepcopy(dict(invocation_reference)),
            "invocation": copy.deepcopy(invocation_receipt["invocation"]),
        },
        "targets": targets,
        "package_hash": "",
    }
    package["package_hash"] = digest_without_field(package, "package_hash")
    validate_private_package(package)
    private_root = evaluation_root(root)
    path = (
        private_root
        / "packages"
        / f"{package['package_hash'].removeprefix('sha256:')}.json"
    )
    immutable_json(path, package)
    persisted = read_json(path)
    if persisted != package:
        raise ContractError("Immutable validation private package changed")
    return {
        "package_hash": package["package_hash"],
        "path": path,
        "assessment_path": assessment_path(package["package_hash"], root=root),
    }


def validate_private_package(package: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "kind",
        "model",
        "repository",
        "pr_number",
        "origin_run_id",
        "origin_commit_sha",
        "authority_id",
        "created_at",
        "prompt_digest",
        "binding",
        "authority_contract",
        "validation_rules",
        "invocation_receipt",
        "targets",
        "package_hash",
    }
    if (
        set(package) != required
        or package.get("schema_version") != "1.0.0"
        or package.get("kind") != _PACKAGE_KIND
        or package.get("model") != COPILOT_MODEL
        or package.get("prompt_digest")
        != content_hash(EVALUATION_PROMPT.read_text(encoding="utf-8"))
        or package.get("package_hash")
        != digest_without_field(package, "package_hash")
    ):
        raise ContractError("Validation private package integrity is invalid")
    targets = package.get("targets")
    rules = package.get("validation_rules")
    receipt = package.get("invocation_receipt")
    if (
        not isinstance(targets, list)
        or not targets
        or not isinstance(rules, Mapping)
        or not isinstance(receipt, Mapping)
        or not isinstance(receipt.get("reference"), Mapping)
        or not isinstance(receipt.get("invocation"), Mapping)
    ):
        raise ContractError("Validation private package coverage is invalid")


def load_bound_private_package(
    pointer: Mapping[str, Any],
    *,
    prepared: Mapping[str, Any],
    plan: Mapping[str, Any],
    authority: AuthoritySpec,
    runtime: Mapping[str, Any],
    paired_v0_runtime: Mapping[str, Any],
    invocation_reference: Mapping[str, str],
    invocation_receipt: Mapping[str, Any],
    root: Path | None = None,
) -> dict[str, Any]:
    private_root = evaluation_root(root)
    path = (private_root / str(pointer.get("package_path") or "")).resolve()
    if private_root not in path.parents:
        raise ContractError("Validation private package path escapes its runtime root")
    package = read_json(path)
    validate_private_package(package)
    expected_binding = _package_binding(
        prepared=prepared,
        plan=plan,
        authority=authority,
        runtime=runtime,
        paired_v0_runtime=paired_v0_runtime,
        invocation_reference=invocation_reference,
    )
    if (
        pointer.get("package_hash") != package["package_hash"]
        or pointer.get("origin_run_id") != prepared["run_id"]
        or pointer.get("authority_id") != authority.authority_id
        or package["repository"] != prepared["repository"]
        or package["pr_number"] != prepared["pr_number"]
        or package["origin_run_id"] != prepared["run_id"]
        or package["origin_commit_sha"] != prepared["commit_sha"]
        or package["authority_id"] != authority.authority_id
        or package["binding"] != expected_binding
        or package["authority_contract"] != _authority_contract(authority, runtime)
        or package["validation_rules"] != authority.validation_rules
        or package["invocation_receipt"]
        != {
            "reference": dict(invocation_reference),
            "invocation": invocation_receipt["invocation"],
        }
    ):
        raise ContractError("Stale Copilot validation package is fenced")
    expected_targets = [
        (
            scenario["id"],
            "baseline" if authority.authority_kind == "baseline" else "issue",
            runtime["authority_id"],
        )
        for scenario in authority.validation_rules["scenarios"]
    ]
    if authority.authority_kind == "issue":
        expected_targets.extend(
            (
                scenario["id"],
                "paired_v0",
                paired_v0_runtime["authority_id"],
            )
            for scenario in authority.validation_rules["scenarios"]
        )
    observed_targets = [
        (
            item.get("scenario_id"),
            item.get("role"),
            item.get("runtime", {}).get("authority_id"),
        )
        for item in package["targets"]
        if isinstance(item, Mapping)
        and isinstance(item.get("runtime"), Mapping)
    ]
    if observed_targets != expected_targets:
        raise ContractError("Validation private package target binding is stale")
    for target in package["targets"]:
        expected_runtime = (
            paired_v0_runtime
            if target["role"] == "paired_v0"
            else runtime
        )
        if target["runtime"] != _runtime_payload(expected_runtime):
            raise ContractError("Validation private package runtime binding is stale")
    return package


def write_active_pointer(
    package_record: Mapping[str, Any],
    *,
    prepared: Mapping[str, Any],
    authority_id: str,
    root: Path | None = None,
) -> dict[str, Any]:
    private_root = evaluation_root(root)
    package_path = Path(package_record["path"]).resolve()
    draft_path = Path(package_record["assessment_path"]).resolve()
    if private_root not in package_path.parents or private_root not in draft_path.parents:
        raise ContractError("Copilot validation paths escape their private runtime root")
    pointer = {
        "schema_version": "1.0.0",
        "kind": "test-agent-validation-active-copilot-package",
        "origin_run_id": prepared["run_id"],
        "origin_commit_sha": prepared["commit_sha"],
        "authority_id": authority_id,
        "package_hash": package_record["package_hash"],
        "package_path": package_path.relative_to(private_root).as_posix(),
        "assessment_path": draft_path.relative_to(private_root).as_posix(),
        "pointer_digest": "",
    }
    pointer["pointer_digest"] = digest_without_field(pointer, "pointer_digest")
    atomic_json(private_root / "active.json", pointer)
    return pointer


def load_active_pointer(*, root: Path | None = None) -> dict[str, Any]:
    pointer = read_json(evaluation_root(root) / "active.json")
    required = {
        "schema_version",
        "kind",
        "origin_run_id",
        "origin_commit_sha",
        "authority_id",
        "package_hash",
        "package_path",
        "assessment_path",
        "pointer_digest",
    }
    if (
        set(pointer) != required
        or pointer["schema_version"] != "1.0.0"
        or pointer["kind"] != "test-agent-validation-active-copilot-package"
        or pointer["pointer_digest"]
        != digest_without_field(pointer, "pointer_digest")
    ):
        raise ContractError("Active Copilot validation package pointer is invalid")
    return pointer


def pointer_paths(
    pointer: Mapping[str, Any],
    *,
    root: Path | None = None,
) -> tuple[Path, Path]:
    private_root = evaluation_root(root)
    package = (private_root / str(pointer["package_path"])).resolve()
    assessment = (private_root / str(pointer["assessment_path"])).resolve()
    if private_root not in package.parents or private_root not in assessment.parents:
        raise ContractError("Copilot validation pointer escapes its private runtime root")
    return package, assessment


def load_copilot_evaluation(
    path: Path,
    *,
    package: Mapping[str, Any],
    root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    value = read_json(path)
    schema = read_json(EVALUATION_SCHEMA)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ContractError(
            f"Copilot validation evaluation schema error at {location}"
        )
    if (
        value["kind"] != _EVALUATION_KIND
        or value["model"] != COPILOT_MODEL
        or value["package_hash"] != package["package_hash"]
        or value["authority_id"] != package["authority_id"]
    ):
        raise ContractError("Copilot validation evaluation binding is stale")
    _validate_evaluation_coverage(value, package)
    digest = content_hash(value)
    immutable_json(
        evaluation_root(root)
        / "imports"
        / f"{digest.removeprefix('sha256:')}.json",
        value,
    )
    return value, {
        "model": COPILOT_MODEL,
        "package_hash": package["package_hash"],
        "prompt_digest": package["prompt_digest"],
        "evaluation_digest": digest,
    }


def authority_evidence_from_evaluation(
    *,
    package: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    authority: AuthoritySpec,
    runtime: Mapping[str, Any],
    validated_commit_sha: str,
) -> dict[str, Any]:
    evaluations = {
        item["scenario_id"]: item for item in evaluation["scenarios"]
    }
    targets = {
        (item["scenario_id"], item["role"]): item
        for item in package["targets"]
    }
    scenarios = []
    for rule in authority.validation_rules["scenarios"]:
        scenario_id = rule["id"]
        assessed = evaluations[scenario_id]
        issue_role = (
            "baseline" if authority.authority_kind == "baseline" else "issue"
        )
        issue_attempts = _evaluated_attempts(
            authority=authority,
            scenario=rule,
            target=targets[(scenario_id, issue_role)],
            assessments=assessed["issue_attempts"],
            role=issue_role,
        )
        v0_attempts = (
            []
            if authority.authority_kind == "baseline"
            else _evaluated_attempts(
                authority=authority,
                scenario=rule,
                target=targets[(scenario_id, "paired_v0")],
                assessments=assessed["v0_attempts"],
                role="paired_v0",
            )
        )
        n = int(rule["n"])
        k = int(rule["k"])
        complete_count = sum(item["complete"] is True for item in issue_attempts)
        paired_complete_count = sum(
            item["complete"] is True for item in v0_attempts
        )
        observation_count = sum(
            item["observation"] is True for item in issue_attempts
        )
        paired_observation_count = sum(
            item["observation"] is True for item in v0_attempts
        )
        evidence_complete = complete_count == n and (
            authority.authority_kind == "baseline"
            or paired_complete_count == n
        )
        scenarios.append(
            {
                "scenario_id": scenario_id,
                "execution_digest": rule["execution_digest"],
                "validation_mode": rule["validation_mode"],
                "n": n,
                "k": k,
                "complete_count": complete_count,
                "paired_complete_count": paired_complete_count,
                "observation_count": observation_count,
                "paired_observation_count": paired_observation_count,
                "evidence_complete": evidence_complete,
                "pass": evidence_complete
                and observation_count >= k
                and (
                    authority.authority_kind == "baseline"
                    or paired_observation_count == 0
                ),
                "issue_attempts": issue_attempts,
                "v0_attempts": v0_attempts,
            }
        )
    result = {
        "authority_id": authority.authority_id,
        "authority_kind": authority.authority_kind,
        "canonical_agent": authority.canonical_agent,
        "logical_version": authority.logical_version,
        "runtime_agent_name": runtime["runtime_agent_name"],
        "runtime_agent_version": runtime["runtime_agent_version"],
        "provider_agent_version_reference": content_hash(
            {
                "provider_agent_id": runtime["provider_agent_id"],
                "provider_agent_version_id": runtime[
                    "provider_agent_version_id"
                ],
            }
        ),
        "runtime_mapping_digest": runtime_mapping_digest(runtime),
        "provider_content_digest": runtime["provider_content_digest"],
        "source_content_digest": authority.source_content_digest,
        "execution_digest": authority.execution_digest,
        "validated_commit_sha": validated_commit_sha,
        "n": sum(item["n"] for item in scenarios),
        "k": sum(item["k"] for item in scenarios),
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
    validate_authority_evidence(result)
    return result


def incomplete_authority_evidence_from_invocation(
    *,
    authority: AuthoritySpec,
    runtime: Mapping[str, Any],
    paired_v0_runtime: Mapping[str, Any],
    invocation: Mapping[str, Any],
    validated_commit_sha: str,
    error_code: str,
) -> dict[str, Any]:
    persisted_scenarios = invocation.get("scenarios")
    if not isinstance(persisted_scenarios, list):
        raise ContractError("Validation invocation scenario coverage is invalid")
    by_scenario = {
        item.get("scenario_id"): item
        for item in persisted_scenarios
        if isinstance(item, Mapping)
    }
    if len(by_scenario) != len(persisted_scenarios):
        raise ContractError("Validation invocation scenarios collide")
    scenarios = []
    for rule in authority.validation_rules["scenarios"]:
        persisted = by_scenario.get(rule["id"])
        if not isinstance(persisted, Mapping):
            raise ContractError("Validation invocation scenario is missing")
        issue_invocations = persisted.get("issue_invocations")
        v0_invocations = persisted.get("v0_invocations")
        attempts = rule["attempts"]
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
            raise ContractError("Validation invocation attempt coverage is invalid")
        issue_role = (
            "baseline" if authority.authority_kind == "baseline" else "issue"
        )
        issue_attempts = _incomplete_attempt_evidence(
            authority=authority,
            scenario=rule,
            invocations=issue_invocations,
            target_authority_id=authority.authority_id,
            role=issue_role,
            error_code=error_code,
        )
        paired_attempts = (
            []
            if authority.authority_kind == "baseline"
            else _incomplete_attempt_evidence(
                authority=authority,
                scenario=rule,
                invocations=v0_invocations,
                target_authority_id=paired_v0_runtime["authority_id"],
                role="paired_v0",
                error_code=error_code,
            )
        )
        scenarios.append(
            {
                "scenario_id": rule["id"],
                "execution_digest": rule["execution_digest"],
                "validation_mode": rule["validation_mode"],
                "n": rule["n"],
                "k": rule["k"],
                "complete_count": 0,
                "paired_complete_count": 0,
                "observation_count": 0,
                "paired_observation_count": 0,
                "evidence_complete": False,
                "pass": False,
                "issue_attempts": issue_attempts,
                "v0_attempts": paired_attempts,
            }
        )
    result = {
        "authority_id": authority.authority_id,
        "authority_kind": authority.authority_kind,
        "canonical_agent": authority.canonical_agent,
        "logical_version": authority.logical_version,
        "runtime_agent_name": runtime["runtime_agent_name"],
        "runtime_agent_version": runtime["runtime_agent_version"],
        "provider_agent_version_reference": content_hash(
            {
                "provider_agent_id": runtime["provider_agent_id"],
                "provider_agent_version_id": runtime[
                    "provider_agent_version_id"
                ],
            }
        ),
        "runtime_mapping_digest": runtime_mapping_digest(runtime),
        "provider_content_digest": runtime["provider_content_digest"],
        "source_content_digest": authority.source_content_digest,
        "execution_digest": authority.execution_digest,
        "validated_commit_sha": validated_commit_sha,
        "n": sum(item["n"] for item in scenarios),
        "k": sum(item["k"] for item in scenarios),
        "complete_count": 0,
        "paired_complete_count": 0,
        "observation_count": 0,
        "paired_observation_count": 0,
        "evidence_complete": False,
        "pass": False,
        "scenarios": scenarios,
        "authority_evidence_digest": "",
    }
    result["authority_evidence_digest"] = digest_without_field(
        result,
        "authority_evidence_digest",
    )
    validate_authority_evidence(result)
    return result


def incomplete_result_requires_fresh_invocation(
    result: Mapping[str, Any],
    *,
    invocation: Mapping[str, Any] | None = None,
) -> bool:
    if result.get("outcome") != "INCOMPLETE":
        return False
    evidence = result.get("authority_evidence")
    if not isinstance(evidence, Mapping):
        if not isinstance(invocation, Mapping):
            return False
        return any(
            usable is not True
            for scenario in invocation.get("scenarios", [])
            if isinstance(scenario, Mapping)
            for attempt in [
                *scenario.get("issue_invocations", []),
                *scenario.get("v0_invocations", []),
            ]
            if isinstance(attempt, Mapping)
            for usable in attempt.get("usable_results", [])
        )
    steps = [
        step
        for scenario in evidence.get("scenarios", [])
        if isinstance(scenario, Mapping)
        for attempt in [
            *scenario.get("issue_attempts", []),
            *scenario.get("v0_attempts", []),
        ]
        if isinstance(attempt, Mapping)
        for step in [
            *attempt.get("setup_steps", []),
            *attempt.get("probe_steps", []),
        ]
        if isinstance(step, Mapping)
    ]
    return any(step.get("endpoint_pass") is not True for step in steps)


def _collect_targets(
    *,
    authority: AuthoritySpec,
    deployed: DeployedRuntime,
    paired_v0_deployed: DeployedRuntime,
    invocation: Mapping[str, Any],
    collector: Any,
    scheduler: Any,
) -> list[dict[str, Any]]:
    persisted_scenarios = invocation.get("scenarios")
    if not isinstance(persisted_scenarios, list):
        raise ContractError("Validation invocation scenario coverage is invalid")
    by_scenario = {
        item.get("scenario_id"): item
        for item in persisted_scenarios
        if isinstance(item, Mapping)
    }
    if len(by_scenario) != len(persisted_scenarios):
        raise ContractError("Validation invocation scenarios collide")
    targets = []
    for scenario in authority.validation_rules["scenarios"]:
        persisted = by_scenario.get(scenario["id"])
        if not isinstance(persisted, Mapping):
            raise ContractError("Validation invocation scenario is missing")
        issue_role = (
            "baseline" if authority.authority_kind == "baseline" else "issue"
        )
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
            raise ContractError("Validation invocation attempt coverage is invalid")
        targets.append(
            {
                "scenario_id": scenario["id"],
                "role": issue_role,
                "runtime": _runtime_payload(vars(deployed)),
                "attempts": collector.collect_attempts(
                    target=deployed,
                    executing_authority_id=authority.authority_id,
                    conversation_role=issue_role,
                    scenario=scenario,
                    attempts=list(attempts),
                    invocations=list(issue_invocations),
                    scheduler=scheduler,
                ),
            }
        )
        if authority.authority_kind == "issue":
            targets.append(
                {
                    "scenario_id": scenario["id"],
                    "role": "paired_v0",
                    "runtime": _runtime_payload(vars(paired_v0_deployed)),
                    "attempts": collector.collect_attempts(
                        target=paired_v0_deployed,
                        executing_authority_id=authority.authority_id,
                        conversation_role="paired_v0",
                        scenario=scenario,
                        attempts=list(attempts),
                        invocations=list(v0_invocations),
                        scheduler=scheduler,
                    ),
                }
            )
    return targets


def _incomplete_attempt_evidence(
    *,
    authority: AuthoritySpec,
    scenario: Mapping[str, Any],
    invocations: Sequence[Mapping[str, Any]],
    target_authority_id: str,
    role: str,
    error_code: str,
) -> list[dict[str, Any]]:
    values = []
    for contract, invocation in zip(
        scenario["attempts"],
        invocations,
        strict=True,
    ):
        step_contracts = [
            *contract["setup_steps"],
            *contract["probe_steps"],
        ]
        response_ids = invocation.get("response_ids")
        usable_results = invocation.get("usable_results")
        session_id = invocation.get("session_id")
        if (
            not isinstance(response_ids, list)
            or not isinstance(usable_results, list)
            or len(response_ids) != len(step_contracts)
            or len(usable_results) != len(step_contracts)
            or not all(isinstance(item, str) and item for item in response_ids)
            or not all(isinstance(item, bool) for item in usable_results)
            or (session_id is not None and not isinstance(session_id, str))
        ):
            raise ContractError("Persisted validation invocation is invalid")
        steps = [
            {
                "index": index,
                "step_id": step["id"],
                "request_digest": content_hash(step["request"]),
                "response_reference": content_hash(
                    {"response_reference": response_id}
                ),
                "operation_reference": content_hash(
                    {
                        "unavailable_operation_for_response": response_id,
                        "role": role,
                    }
                ),
                "complete": False,
                "endpoint_pass": usable is True,
                "identity_pass": False,
                "semantic_pass": False,
                "trace_pass": False,
            }
            for index, (step, response_id, usable) in enumerate(
                zip(
                    step_contracts,
                    response_ids,
                    usable_results,
                    strict=True,
                ),
                start=1,
            )
        ]
        setup_count = len(contract["setup_steps"])
        execution_scope = {
            "executing_authority_id": authority.authority_id,
            "target_authority_id": target_authority_id,
            "conversation_role": role,
            "scenario_id": scenario["id"],
            "conversation_group": contract["conversation_group"],
            "attempt": contract["index"],
        }
        values.append(
            {
                "index": contract["index"],
                "conversation_reference": content_hash(
                    {
                        **execution_scope,
                        "runtime_agent": target_authority_id,
                    }
                ),
                "session_reference": content_hash(
                    {**execution_scope, "session_id": session_id}
                ),
                "response_references": [
                    item["response_reference"] for item in steps
                ],
                "operation_references": [
                    item["operation_reference"] for item in steps
                ],
                "setup_steps": steps[:setup_count],
                "probe_steps": steps[setup_count:],
                "complete": False,
                "observation": False,
                "error_code": (
                    "endpoint_response_incomplete"
                    if any(not item["endpoint_pass"] for item in steps)
                    else error_code
                ),
            }
        )
    return values


def _package_binding(
    *,
    prepared: Mapping[str, Any],
    plan: Mapping[str, Any],
    authority: AuthoritySpec,
    runtime: Mapping[str, Any],
    paired_v0_runtime: Mapping[str, Any],
    invocation_reference: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "validation_digest": prepared["digests"]["validation_digest"],
        "shared_validation_digest": prepared["digests"][
            "shared_validation_digest"
        ],
        "execution_matrix_digest": prepared["digests"][
            "execution_matrix_digest"
        ],
        "runtime_topology_digest": prepared["digests"][
            "runtime_topology_digest"
        ],
        "quota_plan_digest": prepared["digests"]["quota_plan_digest"],
        "verifier_commit_sha": prepared["commit_sha"],
        "verifier_digest": prepared["digests"]["verifier_digest"],
        "assignment": verification_assignment(prepared, authority.authority_id),
        "environment_id": plan["environment_id"],
        "location": plan["location"],
        "project_name": prepared["project"]["name"],
        "project_reference": content_hash(
            {"project_id": prepared["project"]["provider_id"]}
        ),
        "telemetry_resource_set": prepared["runtime_topology"][
            "telemetry_resource_set"
        ],
        "telemetry_resource_reference": content_hash(
            {
                "account_reference": prepared["runtime_topology"][
                    "account_reference"
                ],
                "telemetry_resource_set": prepared["runtime_topology"][
                    "telemetry_resource_set"
                ],
            }
        ),
        "runtime_mapping_digest": runtime_mapping_digest(runtime),
        "paired_v0_runtime_mapping_digest": runtime_mapping_digest(
            paired_v0_runtime
        ),
        "invocation_receipt_digest": invocation_reference["receipt_digest"],
        "invocation_digest": invocation_reference["invocation_digest"],
    }


def _authority_contract(
    authority: AuthoritySpec,
    runtime: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "authority_kind": authority.authority_kind,
        "canonical_agent": authority.canonical_agent,
        "logical_version": authority.logical_version,
        "source_content_digest": authority.source_content_digest,
        "execution_digest": authority.execution_digest,
        "validation_mode": authority.validation_mode,
        "provider_content_digest": runtime["provider_content_digest"],
    }


def _validate_evaluation_coverage(
    evaluation: Mapping[str, Any],
    package: Mapping[str, Any],
) -> None:
    expected_scenarios = package["validation_rules"]["scenarios"]
    observed_scenarios = evaluation["scenarios"]
    if [item["scenario_id"] for item in observed_scenarios] != [
        item["id"] for item in expected_scenarios
    ]:
        raise ContractError("Copilot evaluation scenario coverage is incomplete")
    targets = {
        (item["scenario_id"], item["role"]): item
        for item in package["targets"]
    }
    for rule, assessed in zip(
        expected_scenarios,
        observed_scenarios,
        strict=True,
    ):
        issue_role = (
            "baseline"
            if package["authority_contract"]["authority_kind"] == "baseline"
            else "issue"
        )
        _validate_attempt_coverage(
            assessed["issue_attempts"],
            targets[(rule["id"], issue_role)]["attempts"],
        )
        v0 = assessed["v0_attempts"]
        if issue_role == "baseline":
            if v0:
                raise ContractError("Baseline Copilot evaluation has a v0 control")
        else:
            _validate_attempt_coverage(
                v0,
                targets[(rule["id"], "paired_v0")]["attempts"],
            )


def _validate_attempt_coverage(
    assessments: Sequence[Mapping[str, Any]],
    attempts: Sequence[Mapping[str, Any]],
) -> None:
    if [item["index"] for item in assessments] != [
        item["index"] for item in attempts
    ]:
        raise ContractError("Copilot evaluation attempt coverage is incomplete")
    for assessed, package_attempt in zip(assessments, attempts, strict=True):
        package_steps = package_attempt["steps"]
        assessed_steps = assessed["steps"]
        if [item["step_id"] for item in assessed_steps] != [
            item["step_id"] for item in package_steps
        ]:
            raise ContractError("Copilot evaluation step coverage is incomplete")
        for step_evaluation, package_step in zip(
            assessed_steps,
            package_steps,
            strict=True,
        ):
            expected = package_step["expected"]
            expected_semantic = list(expected["semantic_assertions"])
            expected_trace = [
                item["name"] for item in expected["trace_assertions"]
            ]
            if [
                item["assertion"] for item in step_evaluation["semantic_assertions"]
            ] != expected_semantic or [
                item["assertion"] for item in step_evaluation["trace_assertions"]
            ] != expected_trace:
                raise ContractError(
                    "Copilot evaluation assertion coverage is incomplete"
                )


def _evaluated_attempts(
    *,
    authority: AuthoritySpec,
    scenario: Mapping[str, Any],
    target: Mapping[str, Any],
    assessments: Sequence[Mapping[str, Any]],
    role: str,
) -> list[dict[str, Any]]:
    results = []
    for package_attempt, assessed in zip(
        target["attempts"],
        assessments,
        strict=True,
    ):
        steps = []
        for package_step, step_assessment in zip(
            package_attempt["steps"],
            assessed["steps"],
            strict=True,
        ):
            semantic_evaluations = step_assessment["semantic_assertions"]
            trace_evaluations = step_assessment["trace_assertions"]
            endpoint_pass = bool(
                package_step["response_id"]
                and package_step["usable_response"] is True
            )
            terminal_output_present = _terminal_output_present(package_step)
            identity_pass = package_step["identity_pass"] is True
            evaluation_complete = (
                terminal_output_present
                and step_assessment["evidence_sufficient"] is True
                and all(
                    item["evidence_sufficient"] is True
                    for item in [
                        *semantic_evaluations,
                        *trace_evaluations,
                    ]
                )
            )
            steps.append(
                {
                    "index": package_step["index"],
                    "step_id": package_step["step_id"],
                    "request_digest": content_hash(package_step["request"]),
                    "response_reference": content_hash(
                        {"response_reference": package_step["response_id"]}
                    ),
                    "operation_reference": content_hash(
                        {
                            "operation_reference": package_step["operation_id"],
                            "response_reference": package_step["response_id"],
                            "invoke_agent_anchor_span_id": package_step[
                                "invoke_agent_anchor_span_id"
                            ],
                        }
                    ),
                    "complete": endpoint_pass
                    and identity_pass
                    and evaluation_complete,
                    "endpoint_pass": endpoint_pass,
                    "identity_pass": identity_pass,
                    "semantic_pass": all(
                        item["passed"] is True
                        for item in semantic_evaluations
                    ),
                    "trace_pass": all(
                        item["passed"] is True for item in trace_evaluations
                    ),
                }
            )
        setup_count = len(scenario["attempts"][package_attempt["index"] - 1][
            "setup_steps"
        ])
        setup_steps = steps[:setup_count]
        probe_steps = steps[setup_count:]
        complete = (
            assessed["evidence_sufficient"] is True
            and all(item["complete"] for item in steps)
        )
        observation = assessed["observation"] is True
        if observation and not complete:
            raise ContractError(
                "Copilot evaluation cannot observe behavior from incomplete evidence"
            )
        execution_scope = {
            "executing_authority_id": authority.authority_id,
            "target_authority_id": target["runtime"]["authority_id"],
            "conversation_role": role,
            "scenario_id": scenario["id"],
            "conversation_group": package_attempt["conversation_group"],
            "attempt": package_attempt["index"],
        }
        results.append(
            {
                "index": package_attempt["index"],
                "conversation_reference": content_hash(
                    {
                        **execution_scope,
                        "runtime_agent": target["runtime"][
                            "runtime_agent_name"
                        ],
                    }
                ),
                "session_reference": content_hash(
                    {
                        **execution_scope,
                        "session_id": package_attempt["session_id"],
                    }
                ),
                "response_references": [
                    item["response_reference"] for item in steps
                ],
                "operation_references": [
                    item["operation_reference"] for item in steps
                ],
                "setup_steps": setup_steps,
                "probe_steps": probe_steps,
                "complete": complete,
                "observation": observation,
                "error_code": (
                    None
                    if complete
                    else "endpoint_response_incomplete"
                    if any(not item["endpoint_pass"] for item in steps)
                    else "terminal_output_evidence_incomplete"
                    if any(
                        not _terminal_output_present(item)
                        for item in package_attempt["steps"]
                    )
                    else "telemetry_identity_mismatch"
                    if any(not item["identity_pass"] for item in steps)
                    else assessed["error_code"]
                    or "copilot_evidence_insufficient"
                ),
            }
        )
    return results


def _terminal_output_present(step: Mapping[str, Any]) -> bool:
    anchors = [
        row
        for row in step["trace_rows"]
        if row.get("span_id") == step["invoke_agent_anchor_span_id"]
        and row.get("operation_name") == "invoke_agent"
        and row.get("matched_reference") == step["response_id"]
    ]
    return (
        len(anchors) == 1
        and anchors[0].get("output_messages_present") is True
        and anchors[0].get("output_messages_nonempty") is True
    )


def _runtime_payload(runtime: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: (
            list(runtime[field])
            if field == "connection_ids"
            else runtime[field]
        )
        for field in (
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
            "telemetry_identity_id",
            "connection_ids",
        )
    }
