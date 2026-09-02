from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from agent_insights_quality.util import (
    ROOT,
    ContractError,
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
from agent_insights_quality.validation_invocations import (
    load_bound_invocation_receipt,
)
from agent_insights_quality.validation_lifecycle import validation_runtime_root
from agent_insights_quality.validation_runtime import AuthoritySpec

AUTHORITY_RESULT_SCHEMA = (
    ROOT / "schemas" / "test-agent-validation-authority-result.schema.json"
)
_RESULT_KIND = "test-agent-validation-authority-result"


def write_authority_verification_result(
    *,
    prepared: Mapping[str, Any],
    plan: Mapping[str, Any],
    authority: AuthoritySpec,
    runtime: Mapping[str, Any],
    invocation_reference: Mapping[str, str],
    authority_evidence: Mapping[str, Any] | None,
    outcome: str,
    started_at: datetime,
    completed_at: datetime,
    query_stage: str | None,
    error_code: str | None,
    query_diagnostics: Mapping[str, int] | None,
    fence: Callable[[], None],
    root: Path | None = None,
) -> dict[str, str]:
    fence()
    assignment = verification_assignment(prepared, authority.authority_id)
    value = {
        "schema_version": "1.0.0",
        "kind": _RESULT_KIND,
        "repository": prepared["repository"],
        "pr_number": prepared["pr_number"],
        "origin_run_id": prepared["run_id"],
        "origin_commit_sha": prepared["commit_sha"],
        "authority_id": authority.authority_id,
        "outcome": outcome,
        "started_at": started_at.astimezone(UTC).isoformat(),
        "completed_at": completed_at.astimezone(UTC).isoformat(),
        "query_stage": query_stage,
        "error_code": error_code,
        "query_diagnostics": (
            copy.deepcopy(dict(query_diagnostics))
            if query_diagnostics is not None
            else None
        ),
        "binding": {
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
            "verifier_digest": prepared["digests"]["shared_validation_digest"],
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
            "invocation_receipt_digest": invocation_reference["receipt_digest"],
            "invocation_digest": invocation_reference["invocation_digest"],
            "assignment_digest": assignment["assignment_digest"],
        },
        "authority_contract": _authority_contract(authority, runtime),
        "invocation_receipt": copy.deepcopy(dict(invocation_reference)),
        "authority_evidence": (
            copy.deepcopy(dict(authority_evidence))
            if authority_evidence is not None
            else None
        ),
        "artifact_digest": "",
    }
    value["artifact_digest"] = digest_without_field(value, "artifact_digest")
    validate_authority_verification_result(value)
    runtime_root = (root or validation_runtime_root()).resolve()
    path = _current_result_path(runtime_root, value)
    immutable_json(path, value)
    persisted = read_json(path)
    if persisted != value:
        raise ContractError("Immutable authority verification result changed")
    return _result_reference(value, path=path, root=runtime_root)


def load_authority_verification_result(
    reference: Mapping[str, str],
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    runtime_root = (root or validation_runtime_root()).resolve()
    path = (runtime_root / str(reference.get("path") or "")).resolve()
    if runtime_root not in path.parents:
        raise ContractError("Authority verification result path escapes runtime root")
    value = read_json(path)
    validate_authority_verification_result(value)
    if (
        value["authority_id"] != reference.get("authority_id")
        or value["artifact_digest"] != reference.get("authority_result_digest")
        or (
            value.get("authority_evidence") is not None
            and value["authority_evidence"]["authority_evidence_digest"]
            != reference.get("authority_evidence_digest")
        )
    ):
        raise ContractError("Authority verification result reference changed")
    return value


def load_bound_authority_verification_result(
    reference: Mapping[str, str],
    *,
    authority: AuthoritySpec,
    paired_v0_authority: AuthoritySpec,
    runtime: Mapping[str, Any],
    paired_v0_runtime: Mapping[str, Any],
    prepared: Mapping[str, Any],
    plan: Mapping[str, Any],
    require_current_generation: bool,
    root: Path | None = None,
) -> dict[str, Any]:
    value = load_authority_verification_result(reference, root=root)
    invocation = load_bound_invocation_receipt(
        value["invocation_receipt"],
        authority=authority,
        paired_v0_authority=paired_v0_authority,
        runtime=runtime,
        paired_v0_runtime=paired_v0_runtime,
        prepared=prepared,
        plan=plan,
        root=root,
    )
    expected_assignment = verification_assignment(prepared, authority.authority_id)
    binding = value["binding"]
    expected_contract = _authority_contract(authority, runtime)
    if (
        value["repository"] != prepared["repository"]
        or value["pr_number"] != prepared["pr_number"]
        or value["authority_id"] != authority.authority_id
        or value["authority_contract"] != expected_contract
        or binding["shared_validation_digest"]
        != prepared["digests"]["shared_validation_digest"]
        or binding["execution_matrix_digest"]
        != prepared["digests"]["execution_matrix_digest"]
        or binding["runtime_topology_digest"]
        != prepared["digests"]["runtime_topology_digest"]
        or binding["verifier_digest"]
        != prepared["digests"]["shared_validation_digest"]
        or binding["environment_id"] != plan["environment_id"]
        or binding["location"] != plan["location"]
        or binding["project_name"] != prepared["project"]["name"]
        or binding["project_reference"]
        != content_hash({"project_id": prepared["project"]["provider_id"]})
        or binding["telemetry_resource_set"]
        != prepared["runtime_topology"]["telemetry_resource_set"]
        or binding["telemetry_resource_reference"]
        != content_hash(
            {
                "account_reference": prepared["runtime_topology"][
                    "account_reference"
                ],
                "telemetry_resource_set": prepared["runtime_topology"][
                    "telemetry_resource_set"
                ],
            }
        )
        or binding["runtime_mapping_digest"] != runtime_mapping_digest(runtime)
        or binding["invocation_receipt_digest"] != invocation["receipt_digest"]
        or binding["invocation_digest"] != invocation["invocation_digest"]
        or (
            require_current_generation
            and (
                value["origin_run_id"] != prepared["run_id"]
                or value["origin_commit_sha"] != prepared["commit_sha"]
                or binding["verifier_commit_sha"] != prepared["commit_sha"]
                or binding["validation_digest"]
                != prepared["digests"]["validation_digest"]
                or binding["quota_plan_digest"]
                != prepared["digests"]["quota_plan_digest"]
                or binding["assignment_digest"]
                != expected_assignment["assignment_digest"]
            )
        )
    ):
        raise ContractError("Authority verification result binding is stale")
    return value


def current_authority_verification_results(
    *,
    prepared: Mapping[str, Any],
    authority_ids: Sequence[str],
    root: Path | None = None,
) -> dict[str, dict[str, str]]:
    runtime_root = (root or validation_runtime_root()).resolve()
    results: dict[str, dict[str, str]] = {}
    for authority_id in authority_ids:
        path = _result_path(
            runtime_root,
            repository=str(prepared["repository"]),
            pr_number=int(prepared["pr_number"]),
            run_id=str(prepared["run_id"]),
            authority_id=authority_id,
        )
        if not path.is_file():
            continue
        value = read_json(path)
        validate_authority_verification_result(value)
        if (
            value["repository"] != prepared["repository"]
            or value["pr_number"] != prepared["pr_number"]
            or value["origin_run_id"] != prepared["run_id"]
            or value["authority_id"] != authority_id
        ):
            raise ContractError(
                "Current authority verification result path binding is invalid"
            )
        results[authority_id] = _result_reference(
            value,
            path=path,
            root=runtime_root,
        )
    return results


def reusable_authority_verification_results(
    *,
    authorities: Sequence[AuthoritySpec],
    runtime_topology: Mapping[str, Any],
    prepared: Mapping[str, Any],
    plan: Mapping[str, Any],
    root: Path | None = None,
) -> dict[str, dict[str, str] | None]:
    runtime_root = (root or validation_runtime_root()).resolve()
    owner, name = str(prepared["repository"]).split("/", 1)
    result_root = (
        runtime_root
        / "authority-verifications"
        / owner
        / name
        / str(prepared["pr_number"])
    )
    runtime_by_id = {
        item["authority_id"]: item for item in runtime_topology["agents"]
    }
    authority_by_id = {item.authority_id: item for item in authorities}
    paired = {
        item.canonical_agent: item
        for item in authorities
        if item.authority_kind == "baseline"
    }
    candidates: dict[str, list[tuple[str, Path, dict[str, Any]]]] = {}
    if result_root.is_dir():
        for path in result_root.rglob("*.json"):
            try:
                value = read_json(path)
                validate_authority_verification_result(value)
                authority = authority_by_id[value["authority_id"]]
                reference = _result_reference(
                    value,
                    path=path,
                    root=runtime_root,
                )
                load_bound_authority_verification_result(
                    reference,
                    authority=authority,
                    paired_v0_authority=paired[authority.canonical_agent],
                    runtime=runtime_by_id[authority.authority_id],
                    paired_v0_runtime=runtime_by_id[
                        paired[authority.canonical_agent].authority_id
                    ],
                    prepared=prepared,
                    plan=plan,
                    require_current_generation=False,
                    root=runtime_root,
                )
                completed = datetime.fromisoformat(
                    str(value["completed_at"]).replace("Z", "+00:00")
                ).isoformat()
            except (ContractError, KeyError, OSError, ValueError):
                continue
            candidates.setdefault(authority.authority_id, []).append(
                (completed, path, value)
            )
    selected: dict[str, dict[str, str] | None] = {}
    for authority in authorities:
        matching = sorted(candidates.get(authority.authority_id, []))
        if not matching:
            continue
        latest_completed = matching[-1][0]
        latest = [item for item in matching if item[0] == latest_completed]
        digests = {item[2]["artifact_digest"] for item in latest}
        if len(digests) != 1:
            selected[authority.authority_id] = None
            continue
        _, path, value = latest[-1]
        selected[authority.authority_id] = (
            _result_reference(value, path=path, root=runtime_root)
            if value["outcome"] == "PASS"
            else None
        )
    return selected


def validate_authority_verification_result(value: Mapping[str, Any]) -> None:
    errors = sorted(
        Draft202012Validator(
            read_json(AUTHORITY_RESULT_SCHEMA),
            format_checker=FormatChecker(),
        ).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ContractError(
            f"Authority verification result schema error at {location}: "
            f"{error.message}"
        )
    if value["artifact_digest"] != digest_without_field(value, "artifact_digest"):
        raise ContractError("Authority verification result digest is stale")
    try:
        started_at = datetime.fromisoformat(
            str(value["started_at"]).replace("Z", "+00:00")
        )
        completed_at = datetime.fromisoformat(
            str(value["completed_at"]).replace("Z", "+00:00")
        )
    except ValueError as error:
        raise ContractError("Authority verification result time is invalid") from error
    if (
        completed_at < started_at
        or value["invocation_receipt"]["authority_id"] != value["authority_id"]
        or value["binding"]["invocation_receipt_digest"]
        != value["invocation_receipt"]["receipt_digest"]
        or value["binding"]["invocation_digest"]
        != value["invocation_receipt"]["invocation_digest"]
    ):
        raise ContractError("Authority verification result binding is invalid")
    diagnostics = value["query_diagnostics"]
    if diagnostics is not None and (
        diagnostics["matched_reference_count"]
        + diagnostics["missing_reference_count"]
        != diagnostics["expected_reference_count"]
    ):
        raise ContractError("Authority verification query diagnostics are invalid")
    evidence = value["authority_evidence"]
    if value["outcome"] == "INCOMPLETE":
        if isinstance(evidence, Mapping):
            validate_authority_evidence(evidence)
            if (
                evidence["authority_id"] != value["authority_id"]
                or evidence["validated_commit_sha"]
                != value["binding"]["verifier_commit_sha"]
                or evidence["evidence_complete"] is True
            ):
                raise ContractError(
                    "Incomplete authority result contains success-shaped evidence"
                )
        return
    if evidence is None:
        raise ContractError("Completed authority result lacks evidence")
    validate_authority_evidence(evidence)
    if (
        evidence["authority_id"] != value["authority_id"]
        or evidence["validated_commit_sha"] != value["binding"]["verifier_commit_sha"]
        or evidence["evidence_complete"] is not True
        or (value["outcome"] == "PASS") != (evidence["pass"] is True)
    ):
        raise ContractError("Authority verification outcome differs from its evidence")


def sanitize_verification_error(error: BaseException) -> tuple[str, str]:
    stage = str(getattr(error, "stage", "") or "authority_assertion")
    if stage not in {
        "invocation_binding",
        "telemetry_discovery",
        "trace_output_stability",
        "telemetry_identity",
        "authority_assertion",
    }:
        stage = "authority_assertion"
    raw_code = str(getattr(error, "code", "") or type(error).__name__)
    code = re.sub(
        r"[^a-z0-9]+",
        "_",
        re.sub(r"(?<!^)(?=[A-Z])", "_", raw_code).casefold(),
    ).strip("_")
    return stage, code[:64] or "unknown_error"


def verification_query_diagnostics(
    error: BaseException,
) -> dict[str, int] | None:
    values = {
        field: getattr(error, field, None)
        for field in (
            "matched_reference_count",
            "expected_reference_count",
            "missing_reference_count",
        )
    }
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in values.values()
    ):
        return None
    if (
        values["expected_reference_count"] < 1
        or values["matched_reference_count"] + values["missing_reference_count"]
        != values["expected_reference_count"]
    ):
        return None
    return values


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


def _current_result_path(root: Path, value: Mapping[str, Any]) -> Path:
    return _result_path(
        root,
        repository=str(value["repository"]),
        pr_number=int(value["pr_number"]),
        run_id=str(value["origin_run_id"]),
        authority_id=str(value["authority_id"]),
    )


def _result_path(
    root: Path,
    *,
    repository: str,
    pr_number: int,
    run_id: str,
    authority_id: str,
) -> Path:
    owner, name = repository.split("/", 1)
    safe_authority = authority_id.replace("/", "--")
    return (
        root
        / "authority-verifications"
        / owner
        / name
        / str(pr_number)
        / run_id
        / f"{safe_authority}.json"
    )


def _result_reference(
    value: Mapping[str, Any],
    *,
    path: Path,
    root: Path,
) -> dict[str, str]:
    reference = {
        "authority_id": str(value["authority_id"]),
        "path": path.resolve().relative_to(root).as_posix(),
        "authority_result_digest": str(value["artifact_digest"]),
    }
    evidence = value.get("authority_evidence")
    if isinstance(evidence, Mapping):
        reference["authority_evidence_digest"] = str(
            evidence["authority_evidence_digest"]
        )
    return reference
