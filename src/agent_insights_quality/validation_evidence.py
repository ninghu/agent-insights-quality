from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator, FormatChecker

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.validation_trace_gap_policy import (
    target_evidence_decided,
    trace_unknown_acceptance,
)
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    content_hash,
    immutable_json,
    read_json,
    read_yaml,
)
from agent_insights_quality.validation_lifecycle import (
    LocalRecord,
    validation_runtime_root,
)
if TYPE_CHECKING:
    from agent_insights_quality.validation_runtime import AuthoritySpec
from agent_insights_quality.validation_rules import (
    validate_validation_rules,
    validation_matrix,
)

EVIDENCE_SCHEMA = ROOT / "schemas" / "test-agent-validation-evidence.schema.json"
EXPECTED_BASELINE_AUTHORITIES = {
    "finance-agent/v0",
    "healthcare-agent/v0",
    "support-ticket-agent/v0",
    "travel-agent/v0",
    "weather-agent/v0",
}
EXPECTED_ISSUE_AUTHORITIES = {
    f"issue-{number:03d}" for number in range(1, 37)
}


def validate_evidence(
    value: Mapping[str, Any],
    *,
    runtime_topology: Mapping[str, Any] | None = None,
    resources: list[Mapping[str, Any]] | None = None,
    repository_root: Path = ROOT,
) -> None:
    validate_evidence_integrity(value)
    reviewed_contracts = _reviewed_execution_contracts(repository_root)
    if value["execution_matrix_digest"] != content_hash(
        {
            authority_id: contract["execution_digest"]
            for authority_id, contract in reviewed_contracts.items()
        }
    ):
        raise ContractError("Validation evidence execution matrix digest is stale")
    current_specs: dict[str, AuthoritySpec] = {}
    if repository_root.resolve() == ROOT.resolve():
        agents, issues = load_catalogs()
        from agent_insights_quality.validation_manifest import (
            authority_specs,
            current_shared_validation_digest,
            current_validation_digest,
        )

        current_specs = {
            item.authority_id: item for item in authority_specs(agents, issues)
        }
        if value["validation_digest"] != current_validation_digest(agents, issues):
            raise ContractError("Validation evidence contract digest is stale")
        if value["shared_validation_digest"] != current_shared_validation_digest():
            raise ContractError("Validation evidence shared contract digest is stale")
    for authority in value["authorities"]:
        _validate_reviewed_execution_contract(
            authority,
            reviewed_contracts[authority["authority_id"]],
        )
        current = current_specs.get(authority["authority_id"])
        if current is not None and (
            authority["source_content_digest"] != current.source_content_digest
            or authority["execution_digest"] != current.execution_digest
        ):
            raise ContractError(
                f"{authority['authority_id']} evidence content binding is stale"
            )
    if runtime_topology is not None:
        _validate_runtime_topology_binding(value, runtime_topology)
    if resources is not None and value["resource_inventory_digest"] != content_hash(
        resources
    ):
        raise ContractError("Validation evidence resource inventory digest is stale")


def validate_evidence_integrity(value: Mapping[str, Any]) -> None:
    schema = read_json(EVIDENCE_SCHEMA)
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ContractError(
            f"Test Agent validation evidence schema error at {location}: "
            f"{error.message}"
        )
    authorities = value["authorities"]
    authority_ids = [item["authority_id"] for item in authorities]
    if len(authority_ids) != len(set(authority_ids)):
        raise ContractError("Validation evidence authority IDs must be unique")
    if set(authority_ids) != EXPECTED_BASELINE_AUTHORITIES | EXPECTED_ISSUE_AUTHORITIES:
        raise ContractError("Validation evidence must contain the exact 41 authorities")
    for authority in authorities:
        expected_agent, expected_mode = _expected_authority_contract(
            authority["authority_id"]
        )
        if (
            authority["canonical_agent"] != expected_agent
            or authority["scenarios"][0]["validation_mode"] != expected_mode
        ):
            raise ContractError(
                f"{authority['authority_id']} evidence changed its reviewed contract"
            )
        _validate_authority(authority)
    if any(item["evidence_complete"] is not True for item in authorities):
        raise ContractError("Final validation evidence contains an incomplete authority")
    _validate_global_attempt_references(authorities)
    expected_result = (
        "PASS" if all(item["pass"] for item in authorities) else "FAIL"
    )
    if value["result"] != expected_result:
        raise ContractError("Validation evidence aggregate result is invalid")
    expected_digest = digest_without_field(value, "evidence_digest")
    if value["evidence_digest"] != expected_digest:
        raise ContractError("Validation evidence digest is stale")


def stamp_evidence_digests(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    authorities = result.get("authorities")
    if not isinstance(authorities, list):
        raise ContractError("Validation evidence authorities must be an array")
    for authority in authorities:
        if not isinstance(authority, dict):
            raise ContractError("Validation authority evidence must be an object")
        authority["authority_evidence_digest"] = digest_without_field(
            authority,
            "authority_evidence_digest",
        )
    result["evidence_digest"] = digest_without_field(result, "evidence_digest")
    return result


def persist_evidence(
    value: Mapping[str, Any],
    *,
    repository: str,
    pr_number: int,
    run_id: str,
    root: Path | None = None,
) -> LocalRecord:
    validate_evidence(value)
    if (
        value["repository"] != repository
        or value["pr_number"] != pr_number
        or value["run_id"] != run_id
    ):
        raise ContractError("Local evidence path context does not match its content")
    owner, repository_name = repository.split("/", 1)
    path = (
        root
        or validation_runtime_root() / "evidence"
    ) / (
        f"{owner}/{repository_name}/{pr_number}/{run_id}/"
        f"{str(value['evidence_digest']).removeprefix('sha256:')}.json"
    )
    immutable_json(path, dict(value))
    persisted = read_json(path)
    if persisted.get("evidence_digest") != value["evidence_digest"]:
        raise ContractError("Immutable local evidence has a different digest")
    return LocalRecord(
        path=path,
        value=persisted,
        digest=str(persisted["evidence_digest"]),
    )


def load_reused_authority_evidence(
    reference: Mapping[str, str],
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    from agent_insights_quality.validation_authority_results import (
        load_authority_verification_result,
    )

    if "authority_result_digest" not in reference:
        raise ContractError("Reusable authority evidence must reference a result")
    result = load_authority_verification_result(reference, root=root)
    authority = result["authority_evidence"]
    if (
        result["outcome"] not in {"PASS", "FAIL"}
        or not isinstance(authority, dict)
    ):
        raise ContractError("Reused authority result is not definitive evidence")
    return copy.deepcopy(authority)


def validate_authority_evidence(value: Mapping[str, Any]) -> None:
    _validate_authority(value)


def runtime_mapping_digest(runtime: Mapping[str, Any]) -> str:
    return content_hash(
        {
            field: runtime.get(field)
            for field in (
                "runtime_agent_name",
                "runtime_agent_version",
                "provider_agent_id",
                "provider_agent_version_id",
                "hosted_identity_id",
                "hosted_blueprint_id",
                "hosted_deployment_id",
                "runtime_principal_id",
                "telemetry_identity_id",
                "connection_ids",
            )
        }
    )


def digest_without_field(value: Mapping[str, Any], field: str) -> str:
    payload = copy.deepcopy(dict(value))
    payload.pop(field, None)
    return content_hash(payload)


def attempt_observation(
    scenario: Mapping[str, Any],
    probe_steps: list[Mapping[str, Any]],
) -> bool:
    predicate = scenario["defect_predicate"]
    if predicate["kind"] == "never":
        return all(
            item["complete"] and item["semantic_pass"] and item["trace_pass"]
            for item in probe_steps
        )
    required_ids = set(predicate["step_ids"])
    required_surfaces = set(predicate["required_surfaces"])
    selected = [item for item in probe_steps if item["step_id"] in required_ids]
    return bool(selected) and all(
        item["complete"]
        and ("semantic" not in required_surfaces or item["semantic_pass"])
        and ("trace" not in required_surfaces or item["trace_pass"])
        for item in selected
    )


def scenario_evidence_complete(
    *,
    authority_kind: str,
    n: int,
    k: int,
    complete_count: int,
    paired_complete_count: int,
    observation_count: int,
    paired_observation_count: int,
    primary_trace_unknown_accepted: bool = False,
    paired_trace_unknown_accepted: bool = False,
) -> bool:
    primary_decided = complete_count == n or (
        observation_count >= k and primary_trace_unknown_accepted
    )
    control_decided = (
        authority_kind == "baseline"
        or paired_complete_count == n
        or (
            paired_observation_count == 0
            and paired_trace_unknown_accepted
        )
    )
    return primary_decided and control_decided


def _validate_authority(authority: Mapping[str, Any]) -> None:
    authority_id = authority["authority_id"]
    expected_digest = digest_without_field(
        authority,
        "authority_evidence_digest",
    )
    if authority["authority_evidence_digest"] != expected_digest:
        raise ContractError(f"{authority_id} authority evidence digest is stale")
    scenario_ids: set[str] = set()
    for scenario in authority["scenarios"]:
        scenario_id = scenario["scenario_id"]
        if scenario_id in scenario_ids:
            raise ContractError(f"{authority_id} scenario evidence IDs must be unique")
        scenario_ids.add(scenario_id)
        _validate_scenario(
            scenario,
            authority_kind=authority["authority_kind"],
            authority_id=authority_id,
        )
    complete_count = sum(item["complete_count"] for item in authority["scenarios"])
    paired_complete_count = sum(
        item["paired_complete_count"] for item in authority["scenarios"]
    )
    observation_count = sum(
        item["observation_count"] for item in authority["scenarios"]
    )
    paired_observation_count = sum(
        item["paired_observation_count"] for item in authority["scenarios"]
    )
    if (
        authority["n"] != sum(item["n"] for item in authority["scenarios"])
        or authority["k"] != sum(item["k"] for item in authority["scenarios"])
        or authority["complete_count"] != complete_count
        or authority["paired_complete_count"] != paired_complete_count
        or authority["observation_count"] != observation_count
        or authority["paired_observation_count"] != paired_observation_count
        or authority["evidence_complete"]
        is not all(item["evidence_complete"] for item in authority["scenarios"])
    ):
        raise ContractError(f"{authority_id} aggregate complete count is invalid")
    expected_pass = all(item["pass"] for item in authority["scenarios"])
    if authority["pass"] is not expected_pass:
        raise ContractError(f"{authority_id} aggregate pass cannot hide a failed scenario")


def _validate_scenario(
    scenario: Mapping[str, Any],
    *,
    authority_kind: str,
    authority_id: str,
) -> None:
    mode = scenario["validation_mode"]
    n, k = validation_matrix(mode)
    if scenario["n"] != n or scenario["k"] != k:
        raise ContractError(
            f"{authority_id}/{scenario['scenario_id']} attempt count changed"
        )
    issue_attempts = scenario["issue_attempts"]
    v0_attempts = scenario["v0_attempts"]
    if len(issue_attempts) != n:
        raise ContractError(
            f"{authority_id}/{scenario['scenario_id']} issue attempt count is invalid"
        )
    if authority_kind == "baseline":
        if mode != "baseline" or v0_attempts:
            raise ContractError(f"{authority_id} baseline evidence has a v0 control")
    elif mode == "baseline" or len(v0_attempts) != n:
        raise ContractError(f"{authority_id} issue evidence requires an exact v0 control")

    _validate_attempts(
        issue_attempts,
        n=n,
        authority_id=authority_id,
    )
    if v0_attempts:
        _validate_attempts(
            v0_attempts,
            n=n,
            authority_id=authority_id,
        )
    complete_count = sum(attempt["complete"] is True for attempt in issue_attempts)
    paired_complete_count = sum(
        attempt["complete"] is True for attempt in v0_attempts
    )
    observation_count = sum(
        attempt["observation"] is True for attempt in issue_attempts
    )
    paired_observation_count = sum(
        attempt["observation"] is True for attempt in v0_attempts
    )
    primary_acceptance = scenario.get("primary_trace_unknown_acceptance")
    expected_primary_acceptance = _trace_unknown_acceptance(
        authority_kind=authority_kind,
        authority_id=authority_id,
        scenario=scenario,
        target_role=(
            "baseline" if authority_kind == "baseline" else "issue"
        ),
        attempts=issue_attempts,
        acceptance=primary_acceptance,
    )
    if primary_acceptance != expected_primary_acceptance:
        raise ContractError(
            f"{authority_id} primary trace unknown acceptance is invalid"
        )
    paired_acceptance = scenario.get("paired_trace_unknown_acceptance")
    expected_paired_acceptance = (
        None
        if authority_kind == "baseline"
        else _trace_unknown_acceptance(
            authority_kind=authority_kind,
            authority_id=authority_id,
            scenario=scenario,
            target_role="paired_v0",
            attempts=v0_attempts,
            acceptance=paired_acceptance,
        )
    )
    if paired_acceptance != expected_paired_acceptance:
        raise ContractError(
            f"{authority_id} paired trace unknown acceptance is invalid"
        )
    full_attempt_coverage = complete_count == n and (
        authority_kind == "baseline" or paired_complete_count == n
    )
    decision_complete = scenario_evidence_complete(
        authority_kind=authority_kind,
        n=n,
        k=k,
        complete_count=complete_count,
        paired_complete_count=paired_complete_count,
        observation_count=observation_count,
        paired_observation_count=paired_observation_count,
        primary_trace_unknown_accepted=primary_acceptance is not None,
        paired_trace_unknown_accepted=paired_acceptance is not None,
    )
    if (
        scenario["complete_count"] != complete_count
        or scenario["paired_complete_count"] != paired_complete_count
        or scenario["observation_count"] != observation_count
        or scenario["paired_observation_count"] != paired_observation_count
        or (
            scenario["evidence_complete"] is True
            and decision_complete is not True
        )
        or (
            scenario["evidence_complete"] is False
            and full_attempt_coverage is True
        )
    ):
        raise ContractError(f"{authority_id} scenario complete count is invalid")
    expected_pass = (
        scenario["evidence_complete"]
        and target_evidence_decided(
            target_role=(
                "baseline" if authority_kind == "baseline" else "issue"
            ),
            n=n,
            complete_count=complete_count,
            observation_count=observation_count,
            k=k,
            trace_unknown_acceptance=primary_acceptance,
        )
        and (
            authority_kind == "baseline"
            or target_evidence_decided(
                target_role="paired_v0",
                n=n,
                k=k,
                complete_count=paired_complete_count,
                observation_count=paired_observation_count,
                trace_unknown_acceptance=paired_acceptance,
            )
        )
    )
    if scenario["pass"] is not expected_pass:
        raise ContractError(
            f"{authority_id} mechanical evidence result is invalid"
        )
def _trace_unknown_acceptance(
    *,
    authority_kind: str,
    authority_id: str,
    scenario: Mapping[str, Any],
    target_role: str,
    attempts: list[dict[str, Any]],
    acceptance: Any,
) -> dict[str, Any] | None:
    required_surfaces = sorted(
        _reviewed_required_surfaces(
            authority_id,
            str(scenario["scenario_id"]),
        )
    )
    if authority_kind == "baseline":
        required_surfaces = ["semantic", "trace"]
    maturity_digest = (
        str(acceptance.get("maturity_proof_digest") or "")
        if isinstance(acceptance, Mapping)
        else None
    )
    return trace_unknown_acceptance(
        target_role=target_role,
        validation_mode=str(scenario["validation_mode"]),
        n=int(scenario["n"]),
        k=int(scenario["k"]),
        required_surfaces=required_surfaces,
        attempts=[_trace_gap_attempt_payload(attempt) for attempt in attempts],
        maturity_proof_digest=maturity_digest,
    )


def _trace_gap_attempt_payload(
    attempt: Mapping[str, Any],
) -> dict[str, Any]:
    steps = [*attempt["setup_steps"], *attempt["probe_steps"]]
    return {
        "index": int(attempt["index"]),
        "complete": attempt["complete"],
        "observation": attempt["observation"],
        "error_code": attempt["error_code"],
        "endpoint_complete": all(
            step["endpoint_pass"] is True for step in steps
        ),
        "identity_complete": all(
            step["identity_pass"] is True for step in steps
        ),
        "semantic_evidence_complete": all(
            step.get("semantic_evidence_complete", step["complete"])
            is True
            for step in steps
        ),
        "trace_evidence_complete": all(
            step.get("trace_evidence_complete", step["complete"])
            is True
            for step in steps
        ),
        "assertions_contradicted": any(
            (
                step.get("semantic_evidence_complete", step["complete"])
                is True
                and step["semantic_pass"] is not True
            )
            or (
                step.get("trace_evidence_complete", step["complete"])
                is True
                and step["trace_pass"] is not True
            )
            for step in steps
        ),
    }


def _validate_attempts(
    attempts: list[dict[str, Any]],
    *,
    n: int,
    authority_id: str,
) -> None:
    if [attempt["index"] for attempt in attempts] != list(range(1, n + 1)):
        raise ContractError(f"{authority_id} attempt evidence is not ordered")
    conversation_references = {
        attempt["conversation_reference"] for attempt in attempts
    }
    if len(conversation_references) != n:
        raise ContractError(f"{authority_id} attempt conversations are not isolated")
    for attempt in attempts:
        steps = [*attempt["setup_steps"], *attempt["probe_steps"]]
        step_ids = [step["step_id"] for step in steps]
        if len(step_ids) != len(set(step_ids)):
            raise ContractError(f"{authority_id} attempt step IDs are not unique")
        complete = all(
            step["complete"]
            and step["endpoint_pass"]
            and step["identity_pass"]
            for step in steps
        )
        if attempt["complete"] is not complete:
            raise ContractError(
                f"{authority_id} attempt completion is not independently supported"
            )
        response_references = set(attempt["response_references"])
        operation_references = set(attempt["operation_references"])
        if (
            len(response_references) != len(steps)
            or len(operation_references) != len(steps)
            or {step["response_reference"] for step in steps}
            != response_references
            or {step["operation_reference"] for step in steps}
            != operation_references
        ):
            raise ContractError(
                f"{authority_id} step references do not match the attempt mapping"
            )


def _reviewed_required_surfaces(
    authority_id: str,
    scenario_id: str,
) -> set[str]:
    if authority_id.endswith("/v0"):
        return set()
    _, issue_catalog = load_catalogs()
    issue = next(
        (
            item
            for item in issue_catalog["issues"]
            if item["id"] == authority_id
        ),
        None,
    )
    if issue is None:
        raise ContractError(f"{authority_id} reviewed issue contract is missing")
    traffic = read_json(ROOT / str(issue["implementation"]) / "traffic.json")
    scenario = next(
        (
            item
            for item in traffic["validation_rules"]["scenarios"]
            if item["id"] == scenario_id
        ),
        None,
    )
    if scenario is None:
        raise ContractError(f"{authority_id} reviewed scenario contract is missing")
    return set(scenario["defect_predicate"].get("required_surfaces", []))


def _validate_global_attempt_references(
    authorities: list[Mapping[str, Any]],
) -> None:
    seen: dict[str, set[str]] = {
        "conversation": set(),
        "session": set(),
        "response": set(),
        "operation": set(),
    }
    for authority in authorities:
        for scenario in authority["scenarios"]:
            for attempt in [
                *scenario["issue_attempts"],
                *scenario["v0_attempts"],
            ]:
                values = {
                    "conversation": [attempt["conversation_reference"]],
                    "session": [attempt["session_reference"]],
                    "response": attempt["response_references"],
                    "operation": attempt["operation_references"],
                }
                for label, references in values.items():
                    duplicates = seen[label].intersection(references)
                    if duplicates:
                        raise ContractError(
                            f"Validation evidence reuses a global {label} reference"
                        )
                    seen[label].update(references)


def _validate_runtime_topology_binding(
    evidence: Mapping[str, Any],
    topology: Mapping[str, Any],
) -> None:
    agents = topology.get("agents")
    if not isinstance(agents, list) or len(agents) != 41:
        raise ContractError("Validation evidence runtime topology is incomplete")
    by_authority = {item["authority_id"]: item for item in agents}
    if len(by_authority) != 41:
        raise ContractError("Validation evidence runtime topology collides")
    for authority in evidence["authorities"]:
        runtime = by_authority.get(authority["authority_id"])
        if runtime is None:
            raise ContractError("Validation authority is absent from runtime topology")
        if authority["provider_content_digest"] != runtime["provider_content_digest"]:
            raise ContractError(
                f"{authority['authority_id']} evidence provider content is stale"
            )


def _reviewed_execution_contracts(
    repository_root: Path,
) -> dict[str, dict[str, Any]]:
    root = repository_root.resolve()
    agents = read_yaml(root / "catalogs" / "AGENT_CATALOG.yaml")
    issues = read_yaml(root / "catalogs" / "ISSUE_CATALOG.yaml")
    agent_by_name = {
        str(agent["name"]): agent for agent in agents.get("agents", [])
    }
    issue_items = issues.get("issues")
    if len(agent_by_name) != 5 or not isinstance(issue_items, list) or len(
        issue_items
    ) != 36:
        raise ContractError("Reviewed execution catalog inventory is invalid")
    contracts: dict[str, dict[str, Any]] = {}

    def add(
        *,
        authority_id: str,
        authority_kind: str,
        agent: Mapping[str, Any],
        logical_version: str,
        reviewed_mode: str,
        relative_path: str,
    ) -> None:
        path = (root / relative_path / "traffic.json").resolve()
        if root not in path.parents:
            raise ContractError(
                "Reviewed execution contract path escapes repository"
            )
        rules = read_json(path)["validation_rules"]
        validate_validation_rules(
            rules,
            authority_id=authority_id,
            authority_kind=authority_kind,
            canonical_agent=str(agent["name"]),
            logical_version=logical_version,
            runtime_kind=str(agent["type"]),
            framework=str(agent["framework"]),
            model_contract=agents["models"]["test_agents"],
            reviewed_mode=reviewed_mode,
        )
        contracts[authority_id] = {
            "canonical_agent": str(agent["name"]),
            "logical_version": logical_version,
            "execution_digest": rules["execution_digest"],
            "scenarios": {
                scenario["id"]: {
                    "execution_digest": scenario["execution_digest"],
                    "defect_predicate": copy.deepcopy(
                        scenario["defect_predicate"]
                    ),
                    "attempts": [
                        {
                            "index": attempt["index"],
                            "setup_steps": [
                                {
                                    "step_id": step["id"],
                                    "request_digest": content_hash(step["request"]),
                                }
                                for step in attempt["setup_steps"]
                            ],
                            "probe_steps": [
                                {
                                    "step_id": step["id"],
                                    "request_digest": content_hash(step["request"]),
                                }
                                for step in attempt["probe_steps"]
                            ],
                        }
                        for attempt in scenario["attempts"]
                    ],
                }
                for scenario in rules["scenarios"]
            },
        }

    for agent in agent_by_name.values():
        add(
            authority_id=f"{agent['name']}/v0",
            authority_kind="baseline",
            agent=agent,
            logical_version="v0",
            reviewed_mode="baseline",
            relative_path=str(agent["baseline_path"]),
        )
    for issue in issue_items:
        agent = agent_by_name[str(issue["agent"])]
        add(
            authority_id=str(issue["id"]),
            authority_kind="issue",
            agent=agent,
            logical_version=str(issue["id"]),
            reviewed_mode=str(issue["validation_mode"]),
            relative_path=str(issue["implementation"]),
        )
    if set(contracts) != EXPECTED_BASELINE_AUTHORITIES | EXPECTED_ISSUE_AUTHORITIES:
        raise ContractError("Reviewed execution contracts are incomplete")
    return contracts


def _validate_reviewed_execution_contract(
    authority: Mapping[str, Any],
    reviewed: Mapping[str, Any],
) -> None:
    authority_id = authority["authority_id"]
    if (
        authority["canonical_agent"] != reviewed["canonical_agent"]
        or authority["logical_version"] != reviewed["logical_version"]
        or authority["execution_digest"] != reviewed["execution_digest"]
    ):
        raise ContractError(
            f"{authority_id} canonical authority execution digest is stale"
        )
    scenarios = reviewed["scenarios"]
    if {item["scenario_id"] for item in authority["scenarios"]} != set(scenarios):
        raise ContractError(
            f"{authority_id} canonical scenario inventory is stale"
        )
    for scenario in authority["scenarios"]:
        expected = scenarios[scenario["scenario_id"]]
        if scenario["execution_digest"] != expected["execution_digest"]:
            raise ContractError(
                f"{authority_id}/{scenario['scenario_id']} canonical execution "
                "contract is stale"
            )
        _validate_attempt_request_contract(
            authority_id,
            scenario["issue_attempts"],
            expected["attempts"],
        )
        _validate_attempt_observations(
            authority_id,
            scenario["issue_attempts"],
            expected["defect_predicate"],
        )
        if scenario["v0_attempts"]:
            _validate_attempt_request_contract(
                authority_id,
                scenario["v0_attempts"],
                expected["attempts"],
            )
            _validate_attempt_observations(
                authority_id,
                scenario["v0_attempts"],
                expected["defect_predicate"],
            )
            for issue_attempt, v0_attempt in zip(
                scenario["issue_attempts"],
                scenario["v0_attempts"],
                strict=True,
            ):
                if _attempt_request_shape(issue_attempt) != _attempt_request_shape(
                    v0_attempt
                ):
                    raise ContractError(
                        f"{authority_id} issue/v0 request parity is stale"
                    )


def _validate_attempt_observations(
    authority_id: str,
    attempts: list[Mapping[str, Any]],
    predicate: Mapping[str, Any],
) -> None:
    del predicate
    for attempt in attempts:
        if attempt["complete"] is not True and attempt["observation"] is True:
            raise ContractError(
                f"{authority_id} incomplete attempt cannot assert an observation"
            )


def _validate_attempt_request_contract(
    authority_id: str,
    observed: list[Mapping[str, Any]],
    expected: list[Mapping[str, Any]],
) -> None:
    if len(observed) != len(expected):
        raise ContractError(f"{authority_id} canonical attempt inventory is stale")
    for attempt, contract in zip(observed, expected, strict=True):
        if (
            attempt["index"] != contract["index"]
            or _attempt_request_shape(attempt)
            != {
                "setup_steps": contract["setup_steps"],
                "probe_steps": contract["probe_steps"],
            }
        ):
            raise ContractError(f"{authority_id} canonical request contract is stale")


def _attempt_request_shape(attempt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        role: [
            {
                "step_id": step["step_id"],
                "request_digest": step["request_digest"],
            }
            for step in attempt[role]
        ]
        for role in ("setup_steps", "probe_steps")
    }


def _expected_authority_contract(authority_id: str) -> tuple[str, str]:
    if authority_id.endswith("/v0"):
        return authority_id.removesuffix("/v0"), "baseline"
    number = int(authority_id.removeprefix("issue-"))
    agent = (
        "weather-agent"
        if number <= 6
        else "healthcare-agent"
        if number <= 12
        else "finance-agent"
        if number <= 20
        else "travel-agent"
        if number <= 28
        else "support-ticket-agent"
    )
    mode = (
        "model_mediated"
        if number <= 12 or number in {19, 21, 25, 26}
        else "deterministic"
    )
    return agent, mode
