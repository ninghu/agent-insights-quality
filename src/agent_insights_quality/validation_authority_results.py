from __future__ import annotations

import copy
import json
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
    assert_invocation_receipt_set_isolated,
    load_bound_invocation_receipt,
    load_invocation_receipt,
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
    copilot_evaluation: Mapping[str, str] | None = None,
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
            "verifier_digest": prepared["digests"]["verifier_digest"],
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
    if copilot_evaluation is not None:
        value["copilot_evaluation"] = copy.deepcopy(dict(copilot_evaluation))
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
    if path.read_bytes() != _canonical_document_bytes(value):
        raise ContractError(
            "Authority verification result bytes are not canonical"
        )
    validate_authority_verification_result(value)
    evaluation = value.get("copilot_evaluation")
    if isinstance(evaluation, Mapping):
        from agent_insights_quality.validation_copilot import (
            EVALUATION_SCHEMA,
            _validate_evaluation_coverage,
            validate_private_package,
        )

        package_path = (
            runtime_root
            / "copilot-authority-evaluations"
            / "packages"
            / f"{evaluation['package_hash'].removeprefix('sha256:')}.json"
        )
        import_path = (
            runtime_root
            / "copilot-authority-evaluations"
            / "imports"
            / f"{evaluation['evaluation_digest'].removeprefix('sha256:')}.json"
        )
        try:
            package = read_json(package_path)
            imported = read_json(import_path)
        except (ContractError, OSError) as error:
            raise ContractError(
                "Authority result Copilot evaluation artifact is unavailable"
            ) from error
        evaluation_errors = list(
            Draft202012Validator(
                read_json(EVALUATION_SCHEMA),
                format_checker=FormatChecker(),
            ).iter_errors(imported)
        )
        if evaluation_errors:
            raise ContractError(
                "Authority result Copilot evaluation artifact is invalid"
            )
        if (
            package_path.read_bytes() != _canonical_document_bytes(package)
            or import_path.read_bytes() != _canonical_document_bytes(imported)
        ):
            raise ContractError(
                "Authority result Copilot evaluation artifact bytes are not canonical"
            )
        try:
            validate_private_package(
                package,
                require_current_prompt=False,
            )
            _validate_evaluation_coverage(imported, package)
        except ContractError as error:
            raise ContractError(
                "Authority result Copilot evaluation artifact is invalid"
            ) from error
        if (
            package.get("package_hash") != evaluation["package_hash"]
            or digest_without_field(package, "package_hash")
            != evaluation["package_hash"]
            or package.get("prompt_digest") != evaluation["prompt_digest"]
            or content_hash(imported) != evaluation["evaluation_digest"]
            or imported.get("package_hash") != evaluation["package_hash"]
            or imported.get("model") != evaluation["model"]
            or package.get("repository") != value["repository"]
            or package.get("pr_number") != value["pr_number"]
            or package.get("origin_run_id") != value["origin_run_id"]
            or package.get("origin_commit_sha") != value["origin_commit_sha"]
            or package.get("authority_id") != value["authority_id"]
            or package.get("authority_contract") != value["authority_contract"]
            or package.get("invocation_receipt", {}).get("reference")
            != value["invocation_receipt"]
        ):
            raise ContractError(
                "Authority result Copilot evaluation reference changed"
            )
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
        or value["authority_id"] != authority.authority_id
        or value["authority_contract"] != expected_contract
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
                or binding["verifier_digest"]
                != prepared["digests"]["verifier_digest"]
                or binding["validation_digest"]
                != prepared["digests"]["validation_digest"]
                or binding["shared_validation_digest"]
                != prepared["digests"]["shared_validation_digest"]
                or binding["execution_matrix_digest"]
                != prepared["digests"]["execution_matrix_digest"]
                or binding["runtime_topology_digest"]
                != prepared["digests"]["runtime_topology_digest"]
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
    candidates: dict[
        str,
        list[tuple[str, Path, dict[str, Any], dict[str, Any]]],
    ] = {}
    if result_root.is_dir():
        for path in result_root.rglob("*.json"):
            try:
                value = read_json(path)
                validate_authority_verification_result(value)
                if path.resolve() != _current_result_path(
                    runtime_root,
                    value,
                ).resolve():
                    raise ContractError(
                        "Authority verification result path provenance is invalid"
                    )
                authority = authority_by_id[value["authority_id"]]
                reference = _result_reference(
                    value,
                    path=path,
                    root=runtime_root,
                )
                bound = load_bound_authority_verification_result(
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
                (
                    completed,
                    path,
                    value,
                    load_bound_invocation_receipt(
                        bound["invocation_receipt"],
                        authority=authority,
                        paired_v0_authority=paired[
                            authority.canonical_agent
                        ],
                        runtime=runtime_by_id[authority.authority_id],
                        paired_v0_runtime=runtime_by_id[
                            paired[authority.canonical_agent].authority_id
                        ],
                        prepared=prepared,
                        plan=plan,
                        root=runtime_root,
                    ),
                )
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
        _, path, value, _ = latest[-1]
        selected[authority.authority_id] = (
            _result_reference(value, path=path, root=runtime_root)
            if value["outcome"] == "PASS"
            or (
                value["outcome"] == "FAIL"
                and value["binding"]["verifier_digest"]
                == prepared["digests"]["verifier_digest"]
            )
            else None
        )
    assert_invocation_receipt_set_isolated(
        [
            matching[-1][3]
            for authority_id, reference in selected.items()
            if reference is not None
            for matching in [
                sorted(candidates[authority_id])
            ]
            if isinstance(matching[-1][3].get("invocation"), Mapping)
        ]
    )
    return selected


def has_prior_nonpass_result_for_invocation(
    result: Mapping[str, Any],
    *,
    prior_run_ids: Sequence[str],
    root: Path | None = None,
) -> bool:
    validate_authority_verification_result(result)
    return any(
        candidate["artifact_digest"] != result["artifact_digest"]
        and candidate["binding"]["invocation_receipt_digest"]
        == result["binding"]["invocation_receipt_digest"]
        for candidate, _ in _prior_nonpass_results(
            repository=str(result["repository"]),
            pr_number=int(result["pr_number"]),
            authority_id=str(result["authority_id"]),
            prior_run_ids=prior_run_ids,
            root=root,
        )
    )


def paired_trace_gap_history_digest(
    *,
    repository: str,
    pr_number: int,
    authority_id: str,
    invocation_receipt_digest: str,
    prior_run_ids: Sequence[str],
    root: Path | None = None,
) -> str | None:
    candidates = _prior_nonpass_results(
        repository=repository,
        pr_number=pr_number,
        authority_id=authority_id,
        prior_run_ids=prior_run_ids,
        root=root,
    )
    for index, (candidate, receipt) in enumerate(candidates):
        if (
            candidate["outcome"] != "INCOMPLETE"
            or candidate["binding"]["invocation_receipt_digest"]
            != invocation_receipt_digest
            or receipt["origin_run_id"] != candidate["origin_run_id"]
        ):
            continue
        older = next(
            (
                item[0]
                for item in candidates[index + 1 :]
                if item[0]["binding"]["invocation_receipt_digest"]
                != invocation_receipt_digest
            ),
            None,
        )
        if older is not None:
            return content_hash(
                {
                    "policy": "single_paired_trace_gap_after_fresh_verify_v1",
                    "fresh_receipt_result_digest": candidate["artifact_digest"],
                    "older_receipt_result_digest": older["artifact_digest"],
                }
            )
    return None


def latest_prior_nonpass_result(
    *,
    repository: str,
    pr_number: int,
    authority_id: str,
    prior_run_ids: Sequence[str],
    root: Path | None = None,
) -> dict[str, Any] | None:
    results = _prior_nonpass_results(
        repository=repository,
        pr_number=pr_number,
        authority_id=authority_id,
        prior_run_ids=prior_run_ids,
        root=root,
    )
    return results[0][0] if results else None


def _prior_nonpass_results(
    *,
    repository: str,
    pr_number: int,
    authority_id: str,
    prior_run_ids: Sequence[str],
    root: Path | None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    runtime_root = (root or validation_runtime_root()).resolve()
    results = []
    for run_id in dict.fromkeys(prior_run_ids):
        path = _result_path(
            runtime_root,
            repository=repository,
            pr_number=pr_number,
            run_id=str(run_id),
            authority_id=authority_id,
        )
        if not path.is_file():
            continue
        try:
            raw = read_json(path)
            candidate = load_authority_verification_result(
                _result_reference(raw, path=path, root=runtime_root),
                root=runtime_root,
            )
            receipt = load_invocation_receipt(
                candidate["invocation_receipt"],
                root=runtime_root,
            )
        except (ContractError, KeyError, OSError, ValueError):
            continue
        if (
            candidate["repository"] == repository
            and candidate["pr_number"] == pr_number
            and candidate["authority_id"] == authority_id
            and candidate["origin_run_id"] == run_id
            and candidate["outcome"] in {"FAIL", "INCOMPLETE"}
            and receipt["repository"] == repository
            and receipt["pr_number"] == pr_number
            and receipt["authority_id"] == authority_id
            and receipt["receipt_digest"]
            == candidate["binding"]["invocation_receipt_digest"]
        ):
            results.append((candidate, receipt))
    return results


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


def _canonical_document_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("ascii")


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
