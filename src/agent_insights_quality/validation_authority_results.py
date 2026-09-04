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
    authority_execution_identity,
    assert_invocation_receipt_set_isolated,
    invocation_receipt_execution_identity,
    load_bound_invocation_receipt,
    load_invocation_receipt,
)
from agent_insights_quality.validation_lifecycle import (
    LocalValidationLock,
    validation_runtime_root,
)
from agent_insights_quality.validation_runtime import AuthoritySpec

AUTHORITY_RESULT_SCHEMA = (
    ROOT / "schemas" / "test-agent-validation-authority-result.schema.json"
)
_RESULT_KIND = "test-agent-validation-authority-result"
_PUBLICATION_LOCK_WAIT_SECONDS = 35.0


def write_authority_verification_result(
    *,
    prepared: Mapping[str, Any],
    plan: Mapping[str, Any],
    authority: AuthoritySpec,
    runtime: Mapping[str, Any],
    paired_v0_authority: AuthoritySpec | None = None,
    paired_v0_runtime: Mapping[str, Any] | None = None,
    invocation_reference: Mapping[str, str],
    authority_evidence: Mapping[str, Any] | None,
    outcome: str,
    started_at: datetime,
    completed_at: datetime,
    query_stage: str | None,
    error_code: str | None,
    query_diagnostics: Mapping[str, Any] | None,
    fence: Callable[[], None],
    copilot_evaluation: Mapping[str, str] | None = None,
    coordinator_lock_held: bool = False,
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
    reference = _result_reference(value, path=path, root=runtime_root)
    def publish() -> None:
        fence()
        immutable_json(path, value)
        persisted = read_json(path)
        if persisted != value:
            raise ContractError(
                "Immutable authority verification result changed"
            )
    if coordinator_lock_held:
        publish()
    else:
        with LocalValidationLock(
            runtime_root / "coordinator.lock",
            wait_seconds=_PUBLICATION_LOCK_WAIT_SECONDS,
        ):
            publish()
    return reference


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
    historical_contract_fields = (
        "authority_kind",
        "canonical_agent",
        "logical_version",
        "source_content_digest",
        "execution_digest",
        "provider_content_digest",
    )
    if (
        value["repository"] != prepared["repository"]
        or value["authority_id"] != authority.authority_id
        or (
            require_current_generation
            and value["authority_contract"] != expected_contract
        )
        or (
            not require_current_generation
            and any(
                value["authority_contract"][field]
                != expected_contract[field]
                for field in historical_contract_fields
            )
        )
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
                or binding["environment_id"] != plan["environment_id"]
                or binding["location"] != plan["location"]
                or binding["project_name"] != prepared["project"]["name"]
                or binding["project_reference"]
                != content_hash(
                    {"project_id": prepared["project"]["provider_id"]}
                )
                or binding["telemetry_resource_set"]
                != prepared["runtime_topology"]["telemetry_resource_set"]
                or binding["telemetry_resource_reference"]
                != content_hash(
                    {
                        "account_reference": prepared["runtime_topology"][
                            "account_reference"
                        ],
                        "telemetry_resource_set": prepared[
                            "runtime_topology"
                        ]["telemetry_resource_set"],
                    }
                )
                or binding["runtime_mapping_digest"]
                != runtime_mapping_digest(runtime)
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
    prior_generations: Sequence[Mapping[str, Any]] = (),
    root: Path | None = None,
) -> dict[str, dict[str, str] | None]:
    runtime_root = (root or validation_runtime_root()).resolve()
    candidates = _result_candidates(
        runtime_root,
        prepared=prepared,
        prior_generations=prior_generations,
        authority_ids=[item.authority_id for item in authorities],
    )
    runtime_by_id = {
        item["authority_id"]: item for item in runtime_topology["agents"]
    }
    paired = {
        item.canonical_agent: item
        for item in authorities
        if item.authority_kind == "baseline"
    }
    selected: dict[str, dict[str, str] | None] = {}
    reusable_receipts: list[dict[str, Any]] = []
    for authority in authorities:
        current_identity = authority_execution_identity(
            authority=authority,
            paired_v0_authority=paired[authority.canonical_agent],
            runtime=runtime_by_id[authority.authority_id],
            paired_v0_runtime=runtime_by_id[
                paired[authority.canonical_agent].authority_id
            ],
        )
        matching: list[
            tuple[str, Path, dict[str, Any], dict[str, Any]]
        ] = []
        for completed, path, value in candidates.get(
            authority.authority_id,
            [],
        ):
            if value["execution_identity"] != current_identity:
                continue
            try:
                reference = _result_reference(
                    value["result"],
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
                receipt = load_bound_invocation_receipt(
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
                )
            except (ContractError, KeyError, OSError, ValueError):
                continue
            matching.append(
                (
                    completed,
                    path,
                    bound,
                    receipt,
                )
            )
        if not matching:
            selected[authority.authority_id] = None
            continue
        matching.sort(key=lambda item: (item[0], item[2]["artifact_digest"]))
        latest_completed = matching[-1][0]
        latest = [item for item in matching if item[0] == latest_completed]
        digests = {item[2]["artifact_digest"] for item in latest}
        if len(digests) != 1:
            raise ContractError("Newest authority verification results conflict")
        _, path, value, _ = latest[-1]
        selected[authority.authority_id] = (
            _result_reference(value, path=path, root=runtime_root)
            if value["outcome"] == "PASS"
            else None
        )
        if (
            selected[authority.authority_id] is not None
            and isinstance(latest[-1][3].get("invocation"), Mapping)
        ):
            reusable_receipts.append(latest[-1][3])
    assert_invocation_receipt_set_isolated(
        reusable_receipts
    )
    return selected


def authority_verification_history_status(
    *,
    authorities: Sequence[AuthoritySpec],
    runtime_topology: Mapping[str, Any],
    prepared: Mapping[str, Any],
    plan: Mapping[str, Any],
    forced_authority_ids: set[str] | None = None,
    prior_generations: Sequence[Mapping[str, Any]] = (),
    root: Path | None = None,
) -> list[dict[str, Any]]:
    runtime_root = (root or validation_runtime_root()).resolve()
    candidates = _result_candidates(
        runtime_root,
        prepared=prepared,
        prior_generations=prior_generations,
        authority_ids=[item.authority_id for item in authorities],
    )
    runtime_by_id = {
        item["authority_id"]: item for item in runtime_topology["agents"]
    }
    paired = {
        item.canonical_agent: item
        for item in authorities
        if item.authority_kind == "baseline"
    }
    forced = forced_authority_ids or set()
    statuses = []
    for authority in authorities:
        current_identity = authority_execution_identity(
            authority=authority,
            paired_v0_authority=paired[authority.canonical_agent],
            runtime=runtime_by_id[authority.authority_id],
            paired_v0_runtime=runtime_by_id[
                paired[authority.canonical_agent].authority_id
            ],
        )
        entries = candidates.get(authority.authority_id, [])
        exact = [
            item
            for item in entries
            if item[2]["execution_identity"] == current_identity
        ]
        valid = []
        for completed, path, candidate in exact:
            try:
                reference = _result_reference(
                    candidate["result"],
                    path=path,
                    root=runtime_root,
                )
                result = load_bound_authority_verification_result(
                    reference,
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
                    require_current_generation=False,
                    root=runtime_root,
                )
            except (ContractError, KeyError, OSError, ValueError):
                continue
            valid.append((completed, result))
        latest = _latest_unique_result(valid)
        changed = []
        verification_reason = None
        if authority.authority_id in forced:
            status = "missing"
            changed = ["explicit_invalidation"]
            verification_reason = "explicit_invalidation"
        elif latest is not None:
            status = str(latest["outcome"])
            if status != "PASS":
                verification_reason = "prior_non_pass"
        else:
            status = "missing"
            if exact:
                verification_reason = "evidence_interpretation"
            elif entries:
                newest = _latest_unique_history_result(entries)
                changed = _execution_identity_changed_reasons(
                    newest["execution_identity"],
                    current_identity,
                )
                verification_reason = "execution_identity_changed"
            else:
                verification_reason = "no_history"
        statuses.append(
            {
                "authority_id": authority.authority_id,
                "canonical_agent": authority.canonical_agent,
                "status": status,
                "changed": changed,
                "verification_required_reason": verification_reason,
            }
        )
    return statuses


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
) -> dict[str, Any] | None:
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
    maturity = {
        field: getattr(error, field, None)
        for field in (
            "invocation_receipt_digest",
            "evidence_window_end",
            "maturity_boundary",
            "snapshot_observed_at",
            "maximum_hydration_seconds",
            "stabilization_seconds",
        )
    }
    if any(value is not None for value in maturity.values()):
        if (
            not all(value is not None for value in maturity.values())
            or not all(
                isinstance(maturity[field], str) and maturity[field]
                for field in (
                    "invocation_receipt_digest",
                    "evidence_window_end",
                    "maturity_boundary",
                    "snapshot_observed_at",
                )
            )
            or not all(
                isinstance(maturity[field], int)
                and not isinstance(maturity[field], bool)
                and maturity[field] > 0
                for field in (
                    "maximum_hydration_seconds",
                    "stabilization_seconds",
                )
            )
        ):
            raise ContractError("Validation evidence maturity diagnostics are invalid")
        values.update(maturity)
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


def _result_candidates(
    root: Path,
    *,
    prepared: Mapping[str, Any],
    prior_generations: Sequence[Mapping[str, Any]],
    authority_ids: Sequence[str],
) -> dict[str, list[tuple[str, Path, dict[str, Any]]]]:
    repository = str(prepared["repository"])
    generations = _known_generations(prepared, prior_generations)
    candidates: dict[str, list[tuple[str, Path, dict[str, Any]]]] = {}
    for authority_id in authority_ids:
        completed_digests: dict[str, set[str]] = {}
        for generation in generations:
            path = _result_path(
                root,
                repository=repository,
                pr_number=generation["pr_number"],
                run_id=generation["run_id"],
                authority_id=authority_id,
            )
            if not path.is_file():
                continue
            try:
                raw = read_json(path)
                reference = _result_reference(raw, path=path, root=root)
                value = load_authority_verification_result(
                    reference,
                    root=root,
                )
                receipt = load_invocation_receipt(
                    value["invocation_receipt"],
                    root=root,
                )
            except (ContractError, KeyError, OSError, ValueError):
                continue
            if (
                value["repository"] != repository
                or value["pr_number"] != generation["pr_number"]
                or value["origin_run_id"] != generation["run_id"]
                or value["authority_id"] != authority_id
                or path.resolve() != _current_result_path(root, value).resolve()
            ):
                raise ContractError(
                    "Authority verification result generation provenance changed"
                )
            completed = str(value["completed_at"])
            completed_digests.setdefault(completed, set()).add(
                str(value["artifact_digest"])
            )
            candidates.setdefault(authority_id, []).append(
                (
                    completed,
                    path.resolve(),
                    {
                        "execution_identity": (
                            invocation_receipt_execution_identity(receipt)
                        ),
                        "result": value,
                    },
                )
            )
        if any(len(digests) > 1 for digests in completed_digests.values()):
            raise ContractError("Newest authority verification results conflict")
    return candidates


def _known_generations(
    prepared: Mapping[str, Any],
    prior_generations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    repository = str(prepared["repository"])
    values = [
        {
            "repository": repository,
            "pr_number": int(prepared["pr_number"]),
            "run_id": str(prepared["run_id"]),
        },
        *[dict(item) for item in prior_generations],
    ]
    generations: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for value in values:
        try:
            pr_number = int(value["pr_number"])
            run_id = str(value["run_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise ContractError("Validation generation reference is invalid") from error
        if (
            value.get("repository", repository) != repository
            or pr_number < 1
            or re.fullmatch(r"validation-[0-9a-f]{12}", run_id) is None
        ):
            raise ContractError("Validation generation reference is invalid")
        key = (pr_number, run_id)
        if key not in seen:
            generations.append(
                {
                    "repository": repository,
                    "pr_number": pr_number,
                    "run_id": run_id,
                }
            )
            seen.add(key)
    return generations


def _latest_unique_result(
    candidates: Sequence[tuple[str, Mapping[str, Any]]],
) -> Mapping[str, Any] | None:
    if not candidates:
        return None
    ordered = sorted(
        candidates,
        key=lambda item: (item[0], item[1]["artifact_digest"]),
    )
    latest_completed = ordered[-1][0]
    latest = [item for item in ordered if item[0] == latest_completed]
    if len({item[1]["artifact_digest"] for item in latest}) != 1:
        raise ContractError("Newest authority verification results conflict")
    return latest[-1][1]


def _latest_unique_history_result(
    candidates: Sequence[tuple[str, Path, Mapping[str, Any]]],
) -> Mapping[str, Any]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            item[0],
            item[2]["result"]["artifact_digest"],
        ),
    )
    latest_completed = ordered[-1][0]
    latest = [item for item in ordered if item[0] == latest_completed]
    if (
        len(
            {
                item[2]["result"]["artifact_digest"]
                for item in latest
            }
        )
        != 1
    ):
        raise ContractError("Newest authority verification results conflict")
    return latest[-1][2]


def _execution_identity_changed_reasons(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> list[str]:
    reasons = []
    pairs = (
        ("source_content", "authority", "source_content_digest"),
        ("traffic_execution", "authority", "traffic_execution_digest"),
        ("provider_content", "authority", "provider_content_digest"),
        ("paired_v0_source_content", "paired_v0", "source_content_digest"),
        (
            "paired_v0_traffic_execution",
            "paired_v0",
            "traffic_execution_digest",
        ),
        ("paired_v0_provider_content", "paired_v0", "provider_content_digest"),
    )
    for reason, section, field in pairs:
        previous_section = previous.get(section)
        current_section = current.get(section)
        previous_value = (
            previous_section.get(field)
            if isinstance(previous_section, Mapping)
            else None
        )
        current_value = (
            current_section.get(field)
            if isinstance(current_section, Mapping)
            else None
        )
        if previous_value != current_value:
            reasons.append(reason)
    if (
        previous.get("authority", {}).get("runtime_kind")
        != current.get("authority", {}).get("runtime_kind")
    ):
        reasons.append("source_content")
    if (
        isinstance(previous.get("paired_v0"), Mapping)
        and isinstance(current.get("paired_v0"), Mapping)
        and previous["paired_v0"].get("runtime_kind")
        != current["paired_v0"].get("runtime_kind")
    ):
        reasons.append("paired_v0_source_content")
    return list(dict.fromkeys(reasons))
