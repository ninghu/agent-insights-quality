from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, Protocol

from jsonschema import Draft202012Validator, FormatChecker

from agent_insights_quality.util import (
    ROOT,
    ContractError,
    content_hash,
    immutable_json,
    read_json,
    runtime_root,
)
from agent_insights_quality.validation_blob import BlobRecord
from agent_insights_quality.validation_rules import validation_matrix

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


class EvidenceBlobStore(Protocol):
    def create_once(
        self,
        container: str,
        name: str,
        value: dict[str, Any],
    ) -> BlobRecord: ...


def validate_evidence(value: Mapping[str, Any]) -> None:
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
        if authority["validated_head_sha"] != value["candidate_head_sha"]:
            raise ContractError(
                f"{authority['authority_id']} evidence is not bound to candidate head"
            )
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
    store: EvidenceBlobStore,
    value: Mapping[str, Any],
) -> BlobRecord:
    validate_evidence(value)
    owner, repository = value["repository"].split("/", 1)
    name = (
        f"evidence/{owner}/{repository}/{value['pr_number']}/"
        f"{value['cycle_id']}/e{value['epoch']}/"
        "test-agent-validation-evidence.json"
    )
    record = store.create_once(
        "test-agent-validation-snapshots",
        name,
        dict(value),
    )
    if record.value.get("evidence_digest") != value["evidence_digest"]:
        raise ContractError("Immutable evidence retry found a different digest")
    immutable_json(
        runtime_root() / "test-agent-validation" / name,
        dict(value),
    )
    return record


def digest_without_field(value: Mapping[str, Any], field: str) -> str:
    payload = copy.deepcopy(dict(value))
    payload.pop(field, None)
    return content_hash(payload)


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
    observed = sum(item["observed"] for item in authority["scenarios"])
    if authority["complete_count"] != complete_count:
        raise ContractError(f"{authority_id} aggregate complete count is invalid")
    if authority["observed"] != observed:
        raise ContractError(f"{authority_id} aggregate observation count is invalid")
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
            f"{authority_id}/{scenario['scenario_id']} thresholds were downgraded"
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

    for attempts in (issue_attempts, v0_attempts):
        if attempts:
            _validate_attempts(attempts, n=n, authority_id=authority_id)
    complete_count = sum(attempt["complete"] is True for attempt in issue_attempts)
    observed = sum(
        attempt["defect_observed"] is True for attempt in issue_attempts
    )
    if scenario["complete_count"] != complete_count:
        raise ContractError(f"{authority_id} scenario complete count is invalid")
    if scenario["observed"] != observed:
        raise ContractError(f"{authority_id} scenario observation count is invalid")

    if authority_kind == "baseline":
        expected_pass = (
            complete_count == n
            and observed == 0
            and all(
                attempt["expected_observation_pass"] is True
                for attempt in issue_attempts
            )
        )
    else:
        control_complete = sum(
            attempt["complete"] is True for attempt in v0_attempts
        )
        control_observed = sum(
            attempt["defect_observed"] is True for attempt in v0_attempts
        )
        expected_pass = (
            complete_count == n
            and observed >= k
            and control_complete == n
            and control_observed == 0
            and all(
                attempt["expected_observation_pass"] is True
                for attempt in v0_attempts
            )
        )
        if any(
            attempt["expected_observation_pass"]
            is not (attempt["defect_observed"] is True)
            for attempt in issue_attempts
        ):
            raise ContractError(
                f"{authority_id} issue observation expectations are invalid"
            )
        if any(
            attempt["expected_observation_pass"]
            is not (attempt["defect_observed"] is False)
            for attempt in v0_attempts
        ):
            raise ContractError(
                f"{authority_id} v0 observation expectations are invalid"
            )
    if scenario["pass"] is not expected_pass:
        raise ContractError(f"{authority_id} scenario pass result is invalid")


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
        if not all(
            step["complete"]
            and step["endpoint_pass"]
            and step["semantic_pass"]
            and step["trace_pass"]
            and step["identity_pass"]
            for step in attempt["setup_steps"]
        ):
            raise ContractError(f"{authority_id} setup step did not pass")
        complete = all(
            step["complete"]
            and step["endpoint_pass"]
            and step["identity_pass"]
            and isinstance(step["semantic_pass"], bool)
            and isinstance(step["trace_pass"], bool)
            for step in steps
        )
        if attempt["complete"] is not complete:
            raise ContractError(
                f"{authority_id} attempt completion is not independently supported"
            )
        if not complete and attempt["defect_observed"] is not None:
            raise ContractError(
                f"{authority_id} incomplete attempt cannot claim an observation"
            )
        if complete and not isinstance(attempt["defect_observed"], bool):
            raise ContractError(
                f"{authority_id} complete attempt requires an observation result"
            )
        response_references = set(attempt["response_references"])
        operation_references = set(attempt["operation_references"])
        if any(
            step["response_reference"] not in response_references
            or step["operation_reference"] not in operation_references
            for step in steps
        ):
            raise ContractError(
                f"{authority_id} step references do not match the attempt mapping"
            )


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
        if number <= 12 or number in {21, 25, 26}
        else "deterministic"
    )
    return agent, mode
