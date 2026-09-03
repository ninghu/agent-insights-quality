from __future__ import annotations

import copy
import json
import re
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from agent_insights_quality.util import (
    ROOT,
    ContractError,
    content_hash,
    file_hash,
    immutable_json,
    read_json,
)
from agent_insights_quality.validation_evidence import runtime_mapping_digest
from agent_insights_quality.validation_lifecycle import validation_runtime_root
from agent_insights_quality.validation_lifecycle import LocalValidationLock
from agent_insights_quality.validation_runtime import AuthoritySpec, DeployedRuntime

RECEIPT_SCHEMA = (
    ROOT / "schemas" / "test-agent-validation-invocation-receipt.schema.json"
)
_MIGRATION_NAME = "shard-invocations-v2-to-authority-receipts-v1"
_SUPPLEMENTAL_MIGRATION_NAME = (
    "shard-invocations-v2-to-authority-receipts-v1-supplemental"
)


def write_invocation_receipt(
    *,
    prepared: Mapping[str, Any],
    plan: Mapping[str, Any],
    shard_id: int,
    authority: AuthoritySpec,
    runtime: DeployedRuntime,
    paired_v0_authority: AuthoritySpec | None,
    paired_v0_runtime: DeployedRuntime | None,
    invocation: Mapping[str, Any],
    resources: Sequence[Mapping[str, Any]],
    fence: Callable[[], None],
    root: Path | None = None,
    migrated_from: Mapping[str, str] | None = None,
) -> dict[str, str]:
    fence()
    value = _invocation_receipt(
        prepared=prepared,
        plan=plan,
        shard_id=shard_id,
        authority=authority,
        runtime=runtime,
        paired_v0_authority=paired_v0_authority,
        paired_v0_runtime=paired_v0_runtime,
        invocation=invocation,
        resources=resources,
        migrated_from=migrated_from,
    )
    runtime_root = (root or validation_runtime_root()).resolve()
    path = _receipt_path(runtime_root, value)
    immutable_json(path, value)
    persisted = read_json(path)
    validate_invocation_receipt(
        persisted,
        authority=authority,
        paired_v0_authority=paired_v0_authority,
    )
    if persisted != value:
        raise ContractError("Immutable invocation receipt changed after persistence")
    return _receipt_reference(value, path=path, root=runtime_root)


def load_invocation_receipt(
    reference: Mapping[str, str],
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    runtime_root = (root or validation_runtime_root()).resolve()
    path = (runtime_root / str(reference.get("path") or "")).resolve()
    if runtime_root not in path.parents:
        raise ContractError("Invocation receipt path escapes the runtime root")
    value = read_json(path)
    if path.read_bytes() != _canonical_document_bytes(value):
        raise ContractError("Invocation receipt bytes are not canonical")
    validate_invocation_receipt(value)
    if (
        value["authority_id"] != reference.get("authority_id")
        or value["receipt_digest"] != reference.get("receipt_digest")
        or value["invocation_digest"] != reference.get("invocation_digest")
    ):
        raise ContractError("Invocation receipt reference changed")
    return value


def load_bound_invocation_receipt(
    reference: Mapping[str, str],
    *,
    authority: AuthoritySpec,
    paired_v0_authority: AuthoritySpec,
    runtime: Mapping[str, Any],
    paired_v0_runtime: Mapping[str, Any],
    prepared: Mapping[str, Any],
    plan: Mapping[str, Any],
    root: Path | None = None,
) -> dict[str, Any]:
    value = load_invocation_receipt(reference, root=root)
    if not _receipt_is_reusable(
        value,
        authority=authority,
        paired_v0_authority=paired_v0_authority,
        runtime=runtime,
        paired_v0_runtime=paired_v0_runtime,
        prepared=prepared,
        plan=plan,
    ):
        raise ContractError("Selected invocation receipt binding is stale")
    return value


def assert_invocation_receipt_set_isolated(
    receipts: Sequence[Mapping[str, Any]],
) -> None:
    response_ids = [
        response_id
        for receipt in receipts
        for response_id in _invocation_response_ids(receipt["invocation"])
    ]
    session_ids = [
        session_id
        for receipt in receipts
        for session_id in _invocation_session_ids(receipt["invocation"])
    ]
    if (
        len(response_ids) != len(set(response_ids))
        or len(session_ids) != len(set(session_ids))
    ):
        raise ContractError(
            "Invocation receipt response or session references collide"
        )


def validate_invocation_receipt(
    value: Mapping[str, Any],
    *,
    authority: AuthoritySpec | None = None,
    paired_v0_authority: AuthoritySpec | None = None,
) -> None:
    schema = read_json(RECEIPT_SCHEMA)
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
            f"Invocation receipt schema error at {location}: {error.message}"
        )
    if value["receipt_digest"] != _digest_without(value, "receipt_digest"):
        raise ContractError("Invocation receipt digest is stale")
    if value["invocation_digest"] != content_hash(value["invocation"]):
        raise ContractError("Invocation receipt payload digest is stale")
    runtime = value["runtime"]
    paired = value["paired_v0_runtime"]
    if (
        runtime["authority_id"] != value["authority_id"]
        or (
            authority is not None
            and authority.authority_kind == "baseline"
            and paired is not None
        )
        or (
            authority is not None
            and authority.authority_kind == "issue"
            and (
                not isinstance(paired, Mapping)
                or paired["authority_id"]
                != f"{authority.canonical_agent}/v0"
            )
        )
    ):
        raise ContractError("Invocation receipt runtime binding is inconsistent")
    response_bindings = _response_bindings(value["invocation"])
    if value["response_binding_digest"] != content_hash(response_bindings):
        raise ContractError("Invocation receipt response binding is stale")
    if value["completed_at"] != max(
        item["completed_at"] for item in response_bindings
    ):
        raise ContractError("Invocation receipt completion time is inconsistent")
    if authority is not None:
        if (
            value["authority_id"] != authority.authority_id
            or value["source_content_digest"] != authority.source_content_digest
            or value["execution_digest"] != authority.execution_digest
        ):
            raise ContractError("Invocation receipt authority binding is stale")
        expected_paired_contract = (
            None
            if authority.authority_kind == "baseline"
            else {
                "authority_id": paired_v0_authority.authority_id,
                "source_content_digest": (
                    paired_v0_authority.source_content_digest
                ),
                "execution_digest": paired_v0_authority.execution_digest,
            }
            if paired_v0_authority is not None
            else None
        )
        if value["paired_v0_contract"] != expected_paired_contract:
            raise ContractError("Invocation receipt paired-v0 contract is stale")
        _validate_invocation(authority, value["invocation"])
        _validate_resource_provenance(
            value,
            authority=authority,
        )


def select_reusable_invocation_receipts(
    *,
    authorities: Sequence[AuthoritySpec],
    authority_ids: Sequence[str],
    runtime_topology: Mapping[str, Any],
    prepared: Mapping[str, Any],
    plan: Mapping[str, Any],
    forced_authority_ids: set[str] | None = None,
    root: Path | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    runtime_root = (root or validation_runtime_root()).resolve()
    candidates: dict[str, list[tuple[str, Path, dict[str, Any]]]] = {}
    receipt_root = (
        runtime_root
        / "invocation-receipts"
        / str(prepared["repository"]).replace("/", "--")
    )
    if receipt_root.is_dir():
        for path in receipt_root.rglob("*.json"):
            try:
                raw = read_json(path)
                value = load_invocation_receipt(
                    _receipt_reference(
                        raw,
                        path=path,
                        root=runtime_root,
                    ),
                    root=runtime_root,
                )
                if path.resolve() != _receipt_path(
                    runtime_root,
                    value,
                ).resolve():
                    raise ContractError(
                        "Invocation receipt path provenance is invalid"
                    )
                completed = datetime.fromisoformat(
                    str(value["completed_at"]).replace("Z", "+00:00")
                ).astimezone(UTC)
            except (ContractError, KeyError, OSError, ValueError):
                continue
            candidates.setdefault(value["authority_id"], []).append(
                (completed.isoformat(), path, value)
            )

    by_id = {item.authority_id: item for item in authorities}
    runtime_by_id = {
        item["authority_id"]: item for item in runtime_topology["agents"]
    }
    forced = forced_authority_ids or set()
    selected: list[str] = []
    reused: list[dict[str, str]] = []
    reused_values: list[dict[str, Any]] = []
    for authority_id in authority_ids:
        authority = by_id[authority_id]
        matching = [
            item
            for item in candidates.get(authority_id, [])
            if _receipt_is_reusable(
                item[2],
                authority=authority,
                paired_v0_authority=by_id[
                    f"{authority.canonical_agent}/v0"
                ],
                runtime=runtime_by_id[authority_id],
                paired_v0_runtime=runtime_by_id[
                    f"{authority.canonical_agent}/v0"
                ],
                prepared=prepared,
                plan=plan,
            )
        ]
        matching.sort(key=lambda item: (item[0], item[2]["receipt_digest"]))
        if authority_id in forced or not matching:
            selected.append(authority_id)
            continue
        latest_completed = matching[-1][0]
        latest = [item for item in matching if item[0] == latest_completed]
        if len({item[2]["receipt_digest"] for item in latest}) != 1:
            selected.append(authority_id)
            continue
        _, path, value = latest[-1]
        reused.append(_receipt_reference(value, path=path, root=runtime_root))
        reused_values.append(value)
    assert_invocation_receipt_set_isolated(reused_values)
    return selected, reused


@contextmanager
def extract_legacy_shard_invocations(
    *,
    active_path: Path,
    plan: Mapping[str, Any],
    authorities: Sequence[AuthoritySpec],
    root: Path | None = None,
    _supplemental_marker: Mapping[str, Any] | None = None,
    _source_archive_digest: str | None = None,
) -> Iterator[dict[str, Any]]:
    runtime_root = (root or validation_runtime_root()).resolve()
    empty = {
        "source_run_id": None,
        "imported_authority_ids": [],
        "incomplete_authority_ids": [],
    }
    if not active_path.is_file():
        yield empty
        return
    try:
        active = read_json(active_path)
    except (ContractError, OSError):
        yield empty
        return
    if (
        active.get("schema_version") != "2.0.0"
        or active.get("kind") != "test-agent-validation-lifecycle"
        or active.get("state") != "VALIDATING"
        or "invocation_authority_ids" in active
        or active.get("repository") != plan["repository"]
        or active.get("pr_number") != plan["pr_number"]
        or active.get("failure") is not None
        or active.get("deployment", {}).get("failures")
    ):
        yield empty
        return
    source_ids = list(active.get("validation_authority_ids") or [])
    if not source_ids or len(source_ids) != len(set(source_ids)):
        yield empty
        return
    assignments = sorted(
        active.get("shard_assignments", []),
        key=lambda item: int(item["shard_id"]),
    )
    assigned = [
        authority_id
        for assignment in assignments
        for authority_id in assignment["authority_ids"]
    ]
    if len(assigned) != len(set(assigned)) or set(assigned) != set(source_ids):
        yield {
            **empty,
            "source_run_id": active["run_id"],
            "incomplete_authority_ids": source_ids,
        }
        return
    marker = runtime_root / "migrations" / f"{_MIGRATION_NAME}.json"
    with ExitStack() as locks:
        for assignment in assignments:
            locks.enter_context(
                LocalValidationLock(
                    _legacy_shard_root(
                        active,
                        runtime_root,
                        int(assignment["shard_id"]),
                    )
                    / "validation.lock"
                )
            )
        for authority_id in sorted(source_ids):
            locks.enter_context(
                LocalValidationLock(
                    runtime_root
                    / "authority-locks"
                    / active["run_id"]
                    / f"{authority_id.replace('/', '--')}.lock"
                )
            )
        if read_json(active_path) != active:
            raise ContractError(
                "Legacy invocation source changed while acquiring extraction locks"
            )
        if marker.is_file():
            value = read_json(marker)
            try:
                _validate_migration_marker(value)
            except ContractError:
                raise ContractError("Invocation migration marker is inconsistent")
            if value.get("source_run_id") != active.get("run_id"):
                raise ContractError("Invocation migration marker is inconsistent")
            if _supplemental_marker is None:
                yield {
                    key: copy.deepcopy(value[key])
                    for key in (
                        "source_run_id",
                        "imported_authority_ids",
                        "incomplete_authority_ids",
                    )
                }
                return
            if value != _supplemental_marker:
                raise ContractError(
                    "Supplemental invocation migration source marker changed"
                )
        if (
            active.get("journal_digest")
            != _digest_without(active, "journal_digest")
            or active.get("digests", {}).get("execution_matrix_digest")
            != plan["execution_matrix_digest"]
        ):
            result = _write_migration_marker(
                marker=marker,
                active=active,
                imported=[],
                incomplete=source_ids,
            )
            yield result
            return
        desired = _load_legacy_desired_state(active, runtime_root)
        if desired is None:
            result = _write_migration_marker(
                marker=marker,
                active=active,
                imported=[],
                incomplete=source_ids,
            )
            yield result
            return
        by_id = {item.authority_id: item for item in authorities}
        runtime_by_id = {
            item["authority_id"]: item
            for item in active.get("runtime_topology", {}).get("agents", [])
        }
        desired_by_id = {
            item["authority_id"]: item for item in desired["authorities"]
        }
        occurrences: dict[
            str,
            list[tuple[dict[str, Any], list[dict[str, Any]], str, int]],
        ] = {}
        for assignment in assignments:
            artifact = _read_legacy_shard_artifact(
                active=active,
                assignment=assignment,
                root=runtime_root,
            )
            if artifact is None:
                continue
            for invocation in artifact["invocations"]:
                authority_id = invocation["authority_id"]
                occurrences.setdefault(authority_id, []).append(
                    (
                        copy.deepcopy(invocation),
                        _resources_for_invocation(
                            artifact["resources"],
                            invocation,
                        ),
                        artifact["artifact_digest"],
                        int(assignment["shard_id"]),
                    )
                )

        response_counts = Counter(
            response_id
            for candidates in occurrences.values()
            for invocation, _, _, _ in candidates
            for response_id in _invocation_response_ids(invocation)
        )
        session_counts = Counter(
            session_id
            for candidates in occurrences.values()
            for invocation, _, _, _ in candidates
            for session_id in _invocation_session_ids(invocation)
        )
        target_ids = (
            list(_supplemental_marker["incomplete_authority_ids"])
            if _supplemental_marker is not None
            else source_ids
        )
        imported: list[str] = []
        for authority_id in target_ids:
            candidates = occurrences.get(authority_id, [])
            authority = by_id.get(authority_id)
            runtime_value = runtime_by_id.get(authority_id)
            desired_value = desired_by_id.get(authority_id)
            if (
                len(candidates) != 1
                or authority is None
                or runtime_value is None
                or desired_value is None
                or desired_value.get("source_content_digest")
                != authority.source_content_digest
                or desired_value.get("provider_content_digest")
                != runtime_value.get("provider_content_digest")
            ):
                continue
            invocation, resources, artifact_digest, shard_id = candidates[0]
            if any(
                response_counts[item] != 1
                for item in _invocation_response_ids(invocation)
            ) or any(
                session_counts[item] != 1
                for item in _invocation_session_ids(invocation)
            ):
                continue
            runtime = _deployed_runtime(runtime_value)
            paired_id = f"{authority.canonical_agent}/v0"
            paired_authority = by_id.get(paired_id)
            paired_runtime_value = runtime_by_id.get(paired_id)
            if paired_authority is None or paired_runtime_value is None:
                continue
            paired_runtime = (
                None
                if authority.authority_kind == "baseline"
                else _deployed_runtime(paired_runtime_value)
            )
            try:
                _validate_invocation(authority, invocation)
                write_invocation_receipt(
                    prepared=active,
                    plan=plan,
                    shard_id=shard_id,
                    authority=authority,
                    runtime=runtime,
                    paired_v0_authority=(
                        None
                        if authority.authority_kind == "baseline"
                        else paired_authority
                    ),
                    paired_v0_runtime=paired_runtime,
                    invocation=invocation,
                    resources=resources,
                    fence=lambda: None,
                    root=runtime_root,
                    migrated_from={
                        "schema_version": "2.0.0",
                        "kind": "test-agent-validation-shard-invocations",
                        "artifact_digest": artifact_digest,
                    },
                )
            except (ContractError, OSError, ValueError):
                continue
            imported.append(authority_id)

        incomplete = [item for item in target_ids if item not in set(imported)]
        result = (
            _write_supplemental_migration_marker(
                root=runtime_root,
                source_marker=_supplemental_marker,
                active=active,
                source_archive_digest=str(_source_archive_digest or ""),
                imported=imported,
                incomplete=incomplete,
            )
            if _supplemental_marker is not None
            else _write_migration_marker(
                marker=marker,
                active=active,
                imported=imported,
                incomplete=incomplete,
            )
        )
        yield result


@contextmanager
def recover_supplemental_legacy_invocations(
    *,
    active_path: Path,
    plan: Mapping[str, Any],
    authorities: Sequence[AuthoritySpec],
    root: Path | None = None,
) -> Iterator[dict[str, Any]]:
    runtime_root = (root or validation_runtime_root()).resolve()
    empty = {
        "source_run_id": None,
        "imported_authority_ids": [],
        "incomplete_authority_ids": [],
    }
    marker_path = runtime_root / "migrations" / f"{_MIGRATION_NAME}.json"
    if not marker_path.is_file():
        yield empty
        return
    marker = read_json(marker_path)
    _validate_migration_marker(marker)
    marker_imported = list(marker["imported_authority_ids"])
    marker_incomplete = list(marker["incomplete_authority_ids"])
    source_ids = _current_plan_authority_ids(
        plan=plan,
        authorities=authorities,
    )
    _validate_migration_authority_coverage(
        source_ids=source_ids,
        imported=marker_imported,
        incomplete=marker_incomplete,
        message="Supplemental invocation migration authority coverage changed",
    )
    if not marker_incomplete:
        _validate_completed_migration_receipts(
            root=runtime_root,
            marker=marker,
            plan=plan,
            authorities=authorities,
        )
        yield {
            "source_run_id": marker["source_run_id"],
            "imported_authority_ids": marker_imported,
            "incomplete_authority_ids": [],
        }
        return
    supplemental_path = (
        runtime_root
        / "migrations"
        / f"{_SUPPLEMENTAL_MIGRATION_NAME}.json"
    )
    supplemental: dict[str, Any] | None = None
    supplemental_imported: list[str] = []
    supplemental_incomplete: list[str] = []
    combined_imported: list[str] = []
    if supplemental_path.is_file():
        supplemental = read_json(supplemental_path)
        _validate_supplemental_migration_marker(
            supplemental,
            source_marker=marker,
        )
        supplemental_imported = list(supplemental["imported_authority_ids"])
        supplemental_incomplete = list(supplemental["incomplete_authority_ids"])
        _validate_migration_authority_coverage(
            source_ids=marker_incomplete,
            imported=supplemental_imported,
            incomplete=supplemental_incomplete,
            message=(
                "Supplemental invocation completion authority coverage changed"
            ),
        )
        completed = set(marker_imported).union(supplemental_imported)
        combined_imported = [item for item in source_ids if item in completed]
        _validate_migration_authority_coverage(
            source_ids=source_ids,
            imported=combined_imported,
            incomplete=supplemental_incomplete,
            message=(
                "Supplemental invocation combined authority coverage changed"
            ),
        )
        if not supplemental_incomplete:
            _validate_completed_migration_receipts(
                root=runtime_root,
                marker=marker,
                plan=plan,
                authorities=authorities,
            )
            yield {
                "source_run_id": supplemental["source_run_id"],
                "imported_authority_ids": combined_imported,
                "incomplete_authority_ids": [],
            }
            return
    archive_path, archive_digest, source = _locate_legacy_source_archive(
        root=runtime_root,
        source_marker=marker,
        plan=plan,
    )
    archived_source_ids = list(source.get("validation_authority_ids") or [])
    _validate_migration_authority_coverage(
        source_ids=archived_source_ids,
        imported=marker_imported,
        incomplete=marker_incomplete,
        message="Supplemental invocation migration authority coverage changed",
    )
    if supplemental is not None:
        _validate_supplemental_migration_marker(
            supplemental,
            source_marker=marker,
            source_archive_digest=archive_digest,
        )
        yield {
            "source_run_id": supplemental["source_run_id"],
            "imported_authority_ids": combined_imported,
            "incomplete_authority_ids": supplemental_incomplete,
        }
        return
    if not active_path.is_file():
        raise ContractError(
            "Supplemental invocation migration active binding is invalid"
        )
    current = read_json(active_path)
    if (
        current.get("schema_version") != "2.0.0"
        or current.get("kind") != "test-agent-validation-lifecycle"
        or "invocation_authority_ids" not in current
        or current.get("journal_digest")
        != _digest_without(current, "journal_digest")
        or current.get("repository") != plan["repository"]
        or current.get("pr_number") != plan["pr_number"]
    ):
        raise ContractError(
            "Supplemental invocation migration active binding is invalid"
        )
    with extract_legacy_shard_invocations(
        active_path=archive_path,
        plan=plan,
        authorities=authorities,
        root=runtime_root,
        _supplemental_marker=marker,
        _source_archive_digest=archive_digest,
    ) as result:
        supplemental_imported = list(result["imported_authority_ids"])
        supplemental_incomplete = list(result["incomplete_authority_ids"])
        _validate_migration_authority_coverage(
            source_ids=marker_incomplete,
            imported=supplemental_imported,
            incomplete=supplemental_incomplete,
            message=(
                "Supplemental invocation completion authority coverage changed"
            ),
        )
        yield {
            "source_run_id": result["source_run_id"],
            "imported_authority_ids": [
                item
                for item in archived_source_ids
                if item in set(marker_imported).union(supplemental_imported)
            ],
            "incomplete_authority_ids": supplemental_incomplete,
        }


def _locate_legacy_source_archive(
    *,
    root: Path,
    source_marker: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> tuple[Path, str, dict[str, Any]]:
    archive_root = root / "lifecycle" / "superseded-formats"
    matches: list[tuple[Path, str, dict[str, Any]]] = []
    for path in sorted(archive_root.glob("*.json")):
        if re.fullmatch(r"[0-9a-f]{64}", path.stem) is None:
            raise ContractError(
                "Supplemental invocation archive filename is invalid"
            )
        digest = f"sha256:{path.stem}"
        if file_hash(path) != digest:
            raise ContractError(
                "Supplemental invocation archive filename digest changed"
            )
        try:
            value = read_json(path)
        except (ContractError, OSError):
            continue
        if (
            value.get("schema_version") == "2.0.0"
            and value.get("kind") == "test-agent-validation-lifecycle"
            and value.get("state") == "VALIDATING"
            and "invocation_authority_ids" not in value
            and value.get("run_id") == source_marker["source_run_id"]
            and value.get("journal_digest")
            == source_marker["source_lifecycle_digest"]
            and value.get("repository") == plan["repository"]
            and value.get("pr_number") == plan["pr_number"]
        ):
            matches.append((path, digest, value))
    if len(matches) != 1:
        raise ContractError(
            "Supplemental invocation migration requires exactly one source archive"
        )
    return matches[0]


def _write_migration_marker(
    *,
    marker: Path,
    active: Mapping[str, Any],
    imported: list[str],
    incomplete: list[str],
) -> dict[str, Any]:
    migration = {
        "schema_version": "1.0.0",
        "kind": _MIGRATION_NAME,
        "source_run_id": active["run_id"],
        "source_lifecycle_digest": active["journal_digest"],
        "imported_authority_ids": imported,
        "incomplete_authority_ids": incomplete,
        "migration_digest": "",
    }
    migration["migration_digest"] = _digest_without(
        migration,
        "migration_digest",
    )
    immutable_json(marker, migration)
    return {
        "source_run_id": active["run_id"],
        "imported_authority_ids": imported,
        "incomplete_authority_ids": incomplete,
    }


def _write_supplemental_migration_marker(
    *,
    root: Path,
    source_marker: Mapping[str, Any],
    active: Mapping[str, Any],
    source_archive_digest: str,
    imported: list[str],
    incomplete: list[str],
) -> dict[str, Any]:
    if active["run_id"] != source_marker["source_run_id"]:
        raise ContractError(
            "Supplemental invocation migration run binding changed"
        )
    value = {
        "schema_version": "1.0.0",
        "kind": _SUPPLEMENTAL_MIGRATION_NAME,
        "source_run_id": active["run_id"],
        "source_lifecycle_digest": active["journal_digest"],
        "source_marker_digest": source_marker["migration_digest"],
        "source_archive_digest": source_archive_digest,
        "imported_authority_ids": imported,
        "incomplete_authority_ids": incomplete,
        "migration_digest": "",
    }
    value["migration_digest"] = _digest_without(
        value,
        "migration_digest",
    )
    path = (
        root
        / "migrations"
        / f"{_SUPPLEMENTAL_MIGRATION_NAME}.json"
    )
    immutable_json(path, value)
    return {
        "source_run_id": active["run_id"],
        "imported_authority_ids": imported,
        "incomplete_authority_ids": incomplete,
    }


def _validate_migration_marker(value: Mapping[str, Any]) -> None:
    imported = value.get("imported_authority_ids")
    incomplete = value.get("incomplete_authority_ids")
    if (
        value.get("schema_version") != "1.0.0"
        or value.get("kind") != _MIGRATION_NAME
        or value.get("migration_digest")
        != _digest_without(value, "migration_digest")
        or re.fullmatch(
            r"validation-[0-9a-f]{12}",
            str(value.get("source_run_id") or ""),
        )
        is None
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            str(value.get("source_lifecycle_digest") or ""),
        )
        is None
        or not isinstance(imported, list)
        or not all(isinstance(item, str) and item for item in imported)
        or not isinstance(incomplete, list)
        or not all(isinstance(item, str) and item for item in incomplete)
    ):
        raise ContractError("Invocation migration marker is invalid")


def _validate_supplemental_migration_marker(
    value: Mapping[str, Any],
    *,
    source_marker: Mapping[str, Any],
    source_archive_digest: str | None = None,
) -> None:
    imported = value.get("imported_authority_ids")
    incomplete = value.get("incomplete_authority_ids")
    if (
        value.get("schema_version") != "1.0.0"
        or value.get("kind") != _SUPPLEMENTAL_MIGRATION_NAME
        or value.get("source_run_id") != source_marker["source_run_id"]
        or value.get("source_lifecycle_digest")
        != source_marker["source_lifecycle_digest"]
        or value.get("source_marker_digest")
        != source_marker["migration_digest"]
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            str(value.get("source_archive_digest") or ""),
        )
        is None
        or (
            source_archive_digest is not None
            and value.get("source_archive_digest") != source_archive_digest
        )
        or value.get("migration_digest")
        != _digest_without(value, "migration_digest")
        or not isinstance(imported, list)
        or not all(isinstance(item, str) and item for item in imported)
        or not isinstance(incomplete, list)
        or not all(isinstance(item, str) and item for item in incomplete)
    ):
        raise ContractError(
            "Supplemental invocation migration marker is invalid"
        )


def _validate_migration_authority_coverage(
    *,
    source_ids: list[str],
    imported: list[str],
    incomplete: list[str],
    message: str,
) -> None:
    if (
        len(source_ids) != len(set(source_ids))
        or len(imported) != len(set(imported))
        or len(incomplete) != len(set(incomplete))
        or set(imported).intersection(incomplete)
        or set(imported).union(incomplete) != set(source_ids)
    ):
        raise ContractError(message)


def _current_plan_authority_ids(
    *,
    plan: Mapping[str, Any],
    authorities: Sequence[AuthoritySpec],
) -> list[str]:
    source_ids = [item.authority_id for item in authorities]
    plan_authorities = plan.get("authorities")
    if (
        len(source_ids) != 41
        or len(source_ids) != len(set(source_ids))
        or not isinstance(plan.get("repository"), str)
        or not plan["repository"]
        or not isinstance(plan.get("pr_number"), int)
        or isinstance(plan["pr_number"], bool)
        or plan["pr_number"] < 1
        or not isinstance(plan_authorities, list)
        or len(plan_authorities) != 41
    ):
        raise ContractError(
            "Supplemental invocation migration current plan binding changed"
        )
    for expected, actual in zip(authorities, plan_authorities, strict=True):
        if (
            not isinstance(actual, Mapping)
            or actual.get("authority_id") != expected.authority_id
            or actual.get("source_content_digest")
            != expected.source_content_digest
            or actual.get("execution_digest") != expected.execution_digest
        ):
            raise ContractError(
                "Supplemental invocation migration current plan binding changed"
            )
    return source_ids


def _validate_completed_migration_receipts(
    *,
    root: Path,
    marker: Mapping[str, Any],
    plan: Mapping[str, Any],
    authorities: Sequence[AuthoritySpec],
) -> None:
    repository_root = (
        root
        / "invocation-receipts"
        / str(plan["repository"]).replace("/", "--")
    )
    paths = sorted(
        repository_root.glob(
            f"*/{marker['source_run_id']}/*/*.json"
        )
    )
    if len(paths) != len(authorities):
        raise ContractError(
            "Completed invocation migration receipt binding changed"
        )
    authority_by_id = {item.authority_id: item for item in authorities}
    receipts: list[dict[str, Any]] = []
    source_pr_numbers: set[int] = set()
    for path in paths:
        try:
            value = read_json(path)
            authority = authority_by_id.get(str(value.get("authority_id") or ""))
            if authority is None:
                raise ContractError(
                    "Completed invocation migration receipt authority changed"
                )
            validate_invocation_receipt(value)
        except (ContractError, OSError, ValueError) as error:
            raise ContractError(
                "Completed invocation migration receipt binding changed"
            ) from error
        migrated_from = value.get("migrated_from")
        expected_path = (
            repository_root
            / str(value["pr_number"])
            / str(marker["source_run_id"])
            / authority.authority_id.replace("/", "--")
            / f"{value['receipt_digest'].removeprefix('sha256:')}.json"
        )
        if (
            path.resolve() != expected_path.resolve()
            or value["repository"] != plan["repository"]
            or value["origin_run_id"] != marker["source_run_id"]
            or value["origin_binding"]["lifecycle_digest"]
            != marker["source_lifecycle_digest"]
            or not isinstance(migrated_from, Mapping)
            or migrated_from.get("schema_version") != "2.0.0"
            or migrated_from.get("kind")
            != "test-agent-validation-shard-invocations"
        ):
            raise ContractError(
                "Completed invocation migration receipt binding changed"
            )
        receipts.append(value)
        source_pr_numbers.add(int(value["pr_number"]))
    if (
        len(source_pr_numbers) != 1
        or {item["authority_id"] for item in receipts} != set(authority_by_id)
    ):
        raise ContractError(
            "Completed invocation migration receipt authority coverage changed"
        )
    assert_invocation_receipt_set_isolated(receipts)


def _invocation_receipt(
    *,
    prepared: Mapping[str, Any],
    plan: Mapping[str, Any],
    shard_id: int,
    authority: AuthoritySpec,
    runtime: DeployedRuntime,
    paired_v0_authority: AuthoritySpec | None,
    paired_v0_runtime: DeployedRuntime | None,
    invocation: Mapping[str, Any],
    resources: Sequence[Mapping[str, Any]],
    migrated_from: Mapping[str, str] | None,
) -> dict[str, Any]:
    _validate_invocation(authority, invocation)
    response_bindings = _response_bindings(invocation)
    value = {
        "schema_version": "1.0.0",
        "kind": "test-agent-validation-authority-invocation",
        "repository": prepared["repository"],
        "pr_number": prepared["pr_number"],
        "origin_run_id": prepared["run_id"],
        "origin_commit_sha": prepared["commit_sha"],
        "origin_shard_id": shard_id,
        "origin_binding": {
            "lifecycle_digest": prepared["journal_digest"],
            "desired_state_digest": prepared["desired_state_reference"][
                "digest"
            ],
            "runtime_topology_digest": prepared["digests"][
                "runtime_topology_digest"
            ],
            "quota_plan_digest": prepared["digests"]["quota_plan_digest"],
        },
        "authority_id": authority.authority_id,
        "source_content_digest": authority.source_content_digest,
        "execution_digest": authority.execution_digest,
        "invocation_contract_digest": plan["invocation_contract_digest"],
        "paired_v0_contract": (
            {
                "authority_id": paired_v0_authority.authority_id,
                "source_content_digest": (
                    paired_v0_authority.source_content_digest
                ),
                "execution_digest": paired_v0_authority.execution_digest,
            }
            if paired_v0_authority is not None
            else None
        ),
        "environment": _environment_binding(prepared, plan),
        "runtime": {
            **asdict(runtime),
            "connection_ids": list(runtime.connection_ids),
        },
        "paired_v0_runtime": (
            {
                **asdict(paired_v0_runtime),
                "connection_ids": list(paired_v0_runtime.connection_ids),
            }
            if paired_v0_runtime is not None
            else None
        ),
        "invocation": copy.deepcopy(dict(invocation)),
        "resources": copy.deepcopy(list(resources)),
        "completed_at": max(
            item["completed_at"] for item in response_bindings
        ),
        "invocation_digest": content_hash(invocation),
        "response_binding_digest": content_hash(response_bindings),
        "migrated_from": (
            copy.deepcopy(dict(migrated_from))
            if migrated_from is not None
            else None
        ),
        "final_set_claim": "complete-unambiguous-recorded-final-set",
        "receipt_digest": "",
    }
    value["receipt_digest"] = _digest_without(value, "receipt_digest")
    validate_invocation_receipt(
        value,
        authority=authority,
        paired_v0_authority=paired_v0_authority,
    )
    return value


def _environment_binding(
    prepared: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> dict[str, str]:
    project_id = str(prepared.get("project", {}).get("provider_id") or "")
    telemetry_id = str(
        prepared.get("substrate", {}).get("telemetry_resource_id") or ""
    )
    telemetry_set = str(
        prepared.get("runtime_topology", {}).get("telemetry_resource_set") or ""
    )
    if not project_id or not telemetry_id or not telemetry_set:
        raise ContractError("Invocation receipt environment binding is incomplete")
    return {
        "environment_id": str(plan["environment_id"]),
        "location": str(plan["location"]),
        "project_name": str(prepared["project"]["name"]),
        "project_reference": content_hash({"project_id": project_id}),
        "telemetry_resource_set": telemetry_set,
        "telemetry_resource_reference": content_hash(
            {"telemetry_resource_id": telemetry_id}
        ),
    }


def _receipt_is_reusable(
    value: Mapping[str, Any],
    *,
    authority: AuthoritySpec,
    paired_v0_authority: AuthoritySpec,
    runtime: Mapping[str, Any],
    paired_v0_runtime: Mapping[str, Any],
    prepared: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> bool:
    try:
        validate_invocation_receipt(
            value,
            authority=authority,
            paired_v0_authority=(
                None
                if authority.authority_kind == "baseline"
                else paired_v0_authority
            ),
        )
        expected_environment = _environment_binding(prepared, plan)
    except (ContractError, ValueError):
        return False
    return bool(
        value["repository"] == prepared["repository"]
        and _stored_authority_invocation_contract_digest(value)
        == authority_invocation_contract_digest(
            authority,
            paired_v0_authority,
        )
        and value["environment"] == expected_environment
        and value["runtime"]["provider_content_digest"]
        == runtime["provider_content_digest"]
        and value["runtime"]["provider_agent_id"]
        == runtime["provider_agent_id"]
        and value["runtime"]["provider_agent_version_id"]
        == runtime["provider_agent_version_id"]
        and value["runtime"]["runtime_agent_version"]
        == runtime["runtime_agent_version"]
        and runtime_mapping_digest(value["runtime"])
        == runtime_mapping_digest(runtime)
        and (
            value["paired_v0_runtime"] is None
            if authority.authority_kind == "baseline"
            else (
                value["paired_v0_runtime"]["provider_content_digest"]
                == paired_v0_runtime["provider_content_digest"]
                and value["paired_v0_runtime"]["provider_agent_id"]
                == paired_v0_runtime["provider_agent_id"]
                and value["paired_v0_runtime"][
                    "provider_agent_version_id"
                ]
                == paired_v0_runtime["provider_agent_version_id"]
                and value["paired_v0_runtime"]["runtime_agent_version"]
                == paired_v0_runtime["runtime_agent_version"]
                and runtime_mapping_digest(value["paired_v0_runtime"])
                == runtime_mapping_digest(paired_v0_runtime)
            )
        )
    )


def authority_invocation_contract_digest(
    authority: AuthoritySpec,
    paired_v0_authority: AuthoritySpec,
) -> str:
    return content_hash(
        {
            "contract_version": "1.0.0",
            "authority_id": authority.authority_id,
            "authority_kind": authority.authority_kind,
            "canonical_agent": authority.canonical_agent,
            "runtime_kind": authority.runtime_kind,
            "source_content_digest": authority.source_content_digest,
            "execution_digest": authority.execution_digest,
            "paired_v0_contract": (
                None
                if authority.authority_kind == "baseline"
                else {
                    "authority_id": paired_v0_authority.authority_id,
                    "source_content_digest": (
                        paired_v0_authority.source_content_digest
                    ),
                    "execution_digest": paired_v0_authority.execution_digest,
                }
            ),
        }
    )


def _stored_authority_invocation_contract_digest(
    value: Mapping[str, Any],
) -> str:
    paired = value["paired_v0_contract"]
    canonical_agent = (
        str(value["authority_id"]).removesuffix("/v0")
        if paired is None
        else str(paired["authority_id"]).removesuffix("/v0")
    )
    return content_hash(
        {
            "contract_version": "1.0.0",
            "authority_id": value["authority_id"],
            "authority_kind": "baseline" if paired is None else "issue",
            "canonical_agent": canonical_agent,
            "runtime_kind": value["runtime"]["runtime_kind"],
            "source_content_digest": value["source_content_digest"],
            "execution_digest": value["execution_digest"],
            "paired_v0_contract": paired,
        }
    )


def _validate_invocation(
    authority: AuthoritySpec,
    invocation: Mapping[str, Any],
) -> None:
    scenarios = invocation.get("scenarios")
    if (
        invocation.get("authority_id") != authority.authority_id
        or not isinstance(scenarios, list)
    ):
        raise ContractError("Invocation receipt authority payload is invalid")
    expected_by_id = {
        str(item["id"]): item for item in authority.validation_rules["scenarios"]
    }
    actual_by_id = {
        str(item.get("scenario_id") or ""): item
        for item in scenarios
        if isinstance(item, Mapping)
    }
    if (
        len(actual_by_id) != len(scenarios)
        or set(actual_by_id) != set(expected_by_id)
    ):
        raise ContractError("Invocation receipt scenario coverage is invalid")
    response_ids: list[str] = []
    session_ids: list[str] = []
    for scenario_id, expected in expected_by_id.items():
        actual = actual_by_id[scenario_id]
        issue = actual.get("issue_invocations")
        paired = actual.get("v0_invocations")
        attempts = expected["attempts"]
        if (
            not isinstance(issue, list)
            or not isinstance(paired, list)
            or len(issue) != len(attempts)
            or (
                authority.authority_kind == "baseline"
                and paired
            )
            or (
                authority.authority_kind == "issue"
                and len(paired) != len(attempts)
            )
        ):
            raise ContractError("Invocation receipt attempt coverage is invalid")
        for expected_attempt, actual_attempt in zip(
            attempts,
            issue,
            strict=True,
        ):
            response_ids.extend(
                _validate_attempt_invocation(
                    expected_attempt,
                    actual_attempt,
                    hosted=authority.runtime_kind
                    in {"hosted_code", "hosted_custom_container"},
                )
            )
            if actual_attempt.get("session_id"):
                session_ids.append(actual_attempt["session_id"])
        if authority.authority_kind == "issue":
            for expected_attempt, actual_attempt in zip(
                attempts,
                paired,
                strict=True,
            ):
                response_ids.extend(
                    _validate_attempt_invocation(
                        expected_attempt,
                        actual_attempt,
                        hosted=authority.runtime_kind
                        in {"hosted_code", "hosted_custom_container"},
                    )
                )
                if actual_attempt.get("session_id"):
                    session_ids.append(actual_attempt["session_id"])
    if len(response_ids) != len(set(response_ids)):
        raise ContractError("Invocation receipt response references collide")
    if len(session_ids) != len(set(session_ids)):
        raise ContractError("Invocation receipt session references collide")


def _validate_attempt_invocation(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    hosted: bool = False,
) -> list[str]:
    expected_steps = len(expected["setup_steps"]) + len(expected["probe_steps"])
    responses = actual.get("response_ids")
    usable = actual.get("usable_results")
    session_id = actual.get("session_id")
    try:
        started = datetime.fromisoformat(
            str(actual.get("started_at") or "").replace("Z", "+00:00")
        ).astimezone(UTC)
        completed = datetime.fromisoformat(
            str(actual.get("completed_at") or "").replace("Z", "+00:00")
        ).astimezone(UTC)
    except ValueError as error:
        raise ContractError("Invocation receipt time window is invalid") from error
    if (
        completed < started
        or not isinstance(responses, list)
        or len(responses) != expected_steps
        or not all(isinstance(item, str) and item for item in responses)
        or not isinstance(usable, list)
        or len(usable) != expected_steps
        or not all(isinstance(item, bool) for item in usable)
        or (session_id is not None and not isinstance(session_id, str))
        or (hosted and not session_id)
    ):
        raise ContractError("Invocation receipt response window is invalid")
    return responses


def _response_bindings(invocation: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for scenario in invocation["scenarios"]:
        for role, key in (
            ("issue", "issue_invocations"),
            ("paired_v0", "v0_invocations"),
        ):
            for index, attempt in enumerate(scenario[key], start=1):
                result.append(
                    {
                        "scenario_id": scenario["scenario_id"],
                        "conversation_role": role,
                        "attempt": index,
                        "started_at": attempt["started_at"],
                        "completed_at": attempt["completed_at"],
                        "response_ids": list(attempt["response_ids"]),
                    }
                )
    return result


def _receipt_path(root: Path, value: Mapping[str, Any]) -> Path:
    return (
        root
        / "invocation-receipts"
        / str(value["repository"]).replace("/", "--")
        / str(value["pr_number"])
        / str(value["origin_run_id"])
        / str(value["authority_id"]).replace("/", "--")
        / f"{str(value['receipt_digest']).removeprefix('sha256:')}.json"
    )


def _canonical_document_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("ascii")


def _receipt_reference(
    value: Mapping[str, Any],
    *,
    path: Path,
    root: Path,
) -> dict[str, str]:
    return {
        "authority_id": str(value["authority_id"]),
        "path": path.resolve().relative_to(root).as_posix(),
        "receipt_digest": str(value["receipt_digest"]),
        "invocation_digest": str(value["invocation_digest"]),
    }


def _load_legacy_desired_state(
    active: Mapping[str, Any],
    root: Path,
) -> dict[str, Any] | None:
    reference = active.get("desired_state_reference")
    if not isinstance(reference, Mapping):
        return None
    path = (root / str(reference.get("path") or "")).resolve()
    if root not in path.parents:
        return None
    try:
        value = read_json(path)
    except (ContractError, OSError):
        return None
    if (
        value.get("schema_version") != "2.0.0"
        or value.get("kind") != "test-agent-validation-desired-state"
        or value.get("run_id") != active["run_id"]
        or value.get("repository") != active["repository"]
        or value.get("pr_number") != active["pr_number"]
        or value.get("desired_state_digest")
        != _digest_without(value, "desired_state_digest")
        or reference.get("digest") != value["desired_state_digest"]
    ):
        return None
    return value


def _read_legacy_shard_artifact(
    *,
    active: Mapping[str, Any],
    assignment: Mapping[str, Any],
    root: Path,
) -> dict[str, Any] | None:
    try:
        shard_id = int(assignment["shard_id"])
        authority_ids = list(assignment["authority_ids"])
        path = _legacy_shard_root(active, root, shard_id) / "invocations.json"
        value = read_json(path)
    except (ContractError, OSError, KeyError, TypeError, ValueError):
        return None
    runtime_by_id = {
        item["authority_id"]: item
        for item in active["runtime_topology"]["agents"]
    }
    required_runtime_ids = set(authority_ids)
    required_runtime_ids.update(
        f"{runtime_by_id[item]['canonical_agent']}/v0"
        for item in authority_ids
    )
    expected_binding = {
        "repository": active["repository"],
        "pr_number": active["pr_number"],
        "commit_sha": active["commit_sha"],
        "run_id": active["run_id"],
        "validation_digest": active["digests"]["validation_digest"],
        "execution_matrix_digest": active["digests"]["execution_matrix_digest"],
        "runtime_topology_digest": active["digests"]["runtime_topology_digest"],
        "project_id": active["project"]["provider_id"],
        "authorities": [
            {
                field: runtime_by_id[authority_id][field]
                for field in (
                    "authority_id",
                    "runtime_agent_name",
                    "runtime_agent_version",
                    "provider_agent_id",
                    "provider_agent_version_id",
                    "provider_content_digest",
                )
            }
            for authority_id in sorted(required_runtime_ids)
        ],
    }
    if (
        value.get("schema_version") != "2.0.0"
        or value.get("kind")
        != "test-agent-validation-shard-invocations"
        or value.get("shard_id") != shard_id
        or value.get("authority_ids") != authority_ids
        or value.get("binding") != expected_binding
        or value.get("status") != "invoked"
        or not isinstance(value.get("resources"), list)
        or not isinstance(value.get("invocations"), list)
        or [
            item.get("authority_id")
            for item in value.get("invocations", [])
            if isinstance(item, Mapping)
        ]
        != sorted(authority_ids)
        or any(
            item.get("state") == "ambiguous_create"
            for item in value.get("resources", [])
            if isinstance(item, Mapping)
        )
        or value.get("artifact_digest")
        != _digest_without(value, "artifact_digest")
    ):
        return None
    return value


def _legacy_shard_root(
    active: Mapping[str, Any],
    root: Path,
    shard_id: int,
) -> Path:
    owner, name = str(active["repository"]).split("/", 1)
    return (
        root
        / "shards"
        / owner
        / name
        / str(active["pr_number"])
        / str(active["run_id"])
        / f"shard-{shard_id:02d}"
    )


def _invocation_response_ids(invocation: Mapping[str, Any]) -> list[str]:
    return [
        response_id
        for scenario in invocation.get("scenarios", [])
        if isinstance(scenario, Mapping)
        for key in ("issue_invocations", "v0_invocations")
        for attempt in scenario.get(key, [])
        if isinstance(attempt, Mapping)
        for response_id in attempt.get("response_ids", [])
        if isinstance(response_id, str)
    ]


def _invocation_session_ids(invocation: Mapping[str, Any]) -> list[str]:
    return [
        str(attempt["session_id"])
        for scenario in invocation.get("scenarios", [])
        if isinstance(scenario, Mapping)
        for key in ("issue_invocations", "v0_invocations")
        for attempt in scenario.get(key, [])
        if isinstance(attempt, Mapping) and attempt.get("session_id")
    ]


def _resources_for_invocation(
    resources: Sequence[Mapping[str, Any]],
    invocation: Mapping[str, Any],
) -> list[dict[str, Any]]:
    provider_ids = set(_invocation_response_ids(invocation))
    provider_ids.update(_invocation_session_ids(invocation))
    created = [
        item
        for item in resources
        if item.get("state") == "created"
        and item.get("provider_id") in provider_ids
    ]
    intents = {str(item.get("intent_reference") or "") for item in created}
    return [
        copy.deepcopy(dict(item))
        for item in resources
        if item.get("intent_reference") in intents
    ]


def _validate_resource_provenance(
    value: Mapping[str, Any],
    *,
    authority: AuthoritySpec,
) -> None:
    resources = value["resources"]
    if not resources or not all(isinstance(item, Mapping) for item in resources):
        raise ContractError("Invocation receipt resource provenance is missing")
    if any(
        item.get("state") not in {"create_intent", "created"}
        for item in resources
    ):
        raise ContractError("Invocation receipt resource provenance is ambiguous")
    by_intent: dict[str, list[Mapping[str, Any]]] = {}
    expected = _expected_resource_bindings(value, authority)
    for item in resources:
        intent = str(item.get("intent_reference") or "")
        if not intent:
            raise ContractError(
                "Invocation receipt resource intent is missing"
            )
        by_intent.setdefault(intent, []).append(item)
    for events in by_intent.values():
        states = [item.get("state") for item in events]
        if (
            states.count("create_intent") != 1
            or states.count("created") != 1
            or len(events) != 2
        ):
            raise ContractError(
                "Invocation receipt resource chain is incomplete"
            )
        intent = next(
            item for item in events if item["state"] == "create_intent"
        )
        created = next(item for item in events if item["state"] == "created")
        expected_binding = expected.get(str(created.get("provider_id") or ""))
        if any(
            intent.get(field) != created.get(field)
            for field in ("kind", "authority_id", "parent_id")
        ) or expected_binding is None or any(
            created.get(field) != expected_binding[field]
            for field in (
                "kind",
                "authority_id",
                "parent_id",
                "intent_reference",
            )
        ):
            raise ContractError(
                "Invocation receipt resource chain binding changed"
            )
    expected_responses = set(_invocation_response_ids(value["invocation"]))
    expected_sessions = set(_invocation_session_ids(value["invocation"]))
    created_responses = {
        str(item["provider_id"])
        for item in resources
        if item.get("state") == "created"
        and item.get("kind") == "stored_response"
    }
    created_sessions = {
        str(item["provider_id"])
        for item in resources
        if item.get("state") == "created"
        and item.get("kind") == "session"
    }
    if authority.runtime_kind == "prompt":
        valid = (
            created_responses == expected_responses
            and not created_sessions
        )
    else:
        valid = created_sessions == expected_sessions and (
            created_responses == expected_responses
            or (
                value["migrated_from"] is not None
                and not created_responses
            )
        )
    if not valid:
        raise ContractError(
            "Invocation receipt resource coverage is incomplete"
        )


def _expected_resource_bindings(
    value: Mapping[str, Any],
    authority: AuthoritySpec,
) -> dict[str, dict[str, Any]]:
    expected_scenarios = {
        item["id"]: item for item in authority.validation_rules["scenarios"]
    }
    paired_id = f"{authority.canonical_agent}/v0"
    runtimes = {
        authority.authority_id: value["runtime"],
        paired_id: value["paired_v0_runtime"] or value["runtime"],
    }
    result: dict[str, dict[str, Any]] = {}
    for scenario in value["invocation"]["scenarios"]:
        expected = expected_scenarios[scenario["scenario_id"]]
        for role, key in (
            (
                "baseline" if authority.authority_kind == "baseline" else "issue",
                "issue_invocations",
            ),
            ("paired_v0", "v0_invocations"),
        ):
            if role == "paired_v0" and authority.authority_kind == "baseline":
                continue
            target_id = paired_id if role == "paired_v0" else authority.authority_id
            target = runtimes[target_id]
            for attempt, persisted in zip(
                expected["attempts"],
                scenario[key],
                strict=True,
            ):
                scope = {
                    "executing_authority_id": authority.authority_id,
                    "target_authority_id": target_id,
                    "conversation_role": role,
                    "scenario_id": scenario["scenario_id"],
                    "conversation_group": attempt["conversation_group"],
                    "attempt": attempt["index"],
                }
                session_id = persisted.get("session_id")
                if session_id:
                    intent = content_hash(
                        {
                            "authority_id": target_id,
                            "kind": "session",
                            "execution_scope": scope,
                        }
                    )
                    result[str(session_id)] = {
                        "kind": "session",
                        "authority_id": target_id,
                        "parent_id": target["provider_agent_id"],
                        "intent_reference": intent,
                    }
                for index, response_id in enumerate(
                    persisted["response_ids"],
                    start=1,
                ):
                    intent = content_hash(
                        {
                            "authority_id": target_id,
                            "kind": "stored_response",
                            "execution_scope": scope,
                            "step": index,
                        }
                    )
                    result[str(response_id)] = {
                        "kind": "stored_response",
                        "authority_id": target_id,
                        "parent_id": target["provider_agent_id"],
                        "intent_reference": intent,
                    }
    return result


def _deployed_runtime(value: Mapping[str, Any]) -> DeployedRuntime:
    return DeployedRuntime(
        authority_id=str(value["authority_id"]),
        runtime_kind=str(value["runtime_kind"]),
        runtime_agent_name=str(value["runtime_agent_name"]),
        runtime_agent_version=str(value["runtime_agent_version"]),
        provider_agent_id=str(value["provider_agent_id"]),
        provider_agent_version_id=str(value["provider_agent_version_id"]),
        provider_content_digest=str(value["provider_content_digest"]),
        hosted_identity_id=_optional_string(value.get("hosted_identity_id")),
        hosted_blueprint_id=_optional_string(value.get("hosted_blueprint_id")),
        hosted_deployment_id=_optional_string(
            value.get("hosted_deployment_id")
        ),
        runtime_principal_id=_optional_string(value.get("runtime_principal_id")),
        telemetry_identity_id=str(value["telemetry_identity_id"]),
        connection_ids=tuple(str(item) for item in value["connection_ids"]),
    )


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _digest_without(value: Mapping[str, Any], field: str) -> str:
    payload = copy.deepcopy(dict(value))
    payload.pop(field, None)
    return content_hash(payload)
