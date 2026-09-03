from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    canonical_bytes,
    content_hash,
    immutable_json,
    read_json,
)
from agent_insights_quality.validation_blob import (
    APPROVED_RECORD_CONTAINER,
    AzureValidationBlobStore,
    BlobRecord,
    approved_record_blob_prefix,
)
from agent_insights_quality.validation_credentials import local_azure_operator
from agent_insights_quality.validation_evidence import validate_evidence
from agent_insights_quality.validation_lifecycle import (
    LifecycleJournal,
    LocalValidationLock,
    read_bound_local_record,
    validation_runtime_root,
)
from agent_insights_quality.validation_local import (
    _git_sha,
    current_clean_commit,
    discover_github_user,
    discover_local_git_context,
)
from agent_insights_quality.validation_manifest import current_validation_digest

APPROVED_RECORD_SCHEMA = (
    ROOT / "schemas" / "test-agent-validation-approved-record.schema.json"
)


def approve_test_agent_validation(
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    lock = LocalValidationLock()
    with lock:
        git = discover_local_git_context()
        approver = discover_github_user()
        journal = LifecycleJournal(lock=lock)
        active = journal.read_active()
        if (
            active.value["state"] != "READY"
            or active.value["commit_sha"] != git.commit_sha
            or active.value["repository"] != git.repository
            or active.value["pr_number"] != git.pr_number
        ):
            raise ContractError(
                "Latest local validation is not READY for the current pull request head"
            )
        evidence_record = read_bound_local_record(
            validation_runtime_root(),
            active.value["evidence_reference"],
            digest_field="evidence_digest",
            label="evidence",
        )
        evidence = evidence_record.value
        validate_evidence(evidence)
        agents, issues = load_catalogs()
        validation_digest = current_validation_digest(agents, issues)
        validate_local_result_binding(
            active.value,
            evidence,
            repository=git.repository,
            pr_number=git.pr_number,
            commit_sha=git.commit_sha,
            validation_digest=validation_digest,
        )
        requested_record = stamp_approved_record(
            {
                "schema_version": "2.0.0",
                "kind": "test-agent-validation-approved-record",
                "repository": git.repository,
                "pr_number": git.pr_number,
                "commit_sha": git.commit_sha,
                "validation_digest": validation_digest,
                "evidence_digest": evidence["evidence_digest"],
                "generation_digest": active.value["journal_digest"],
                "approved_by": approver,
                "approved_at": now().astimezone(UTC).isoformat(),
                "record_digest": "",
            }
        )
        validate_approved_record(requested_record)
        intent_path = (
            validation_runtime_root()
            / "approval-intents"
            / approved_record_blob_name(git.repository, git.commit_sha)
        )
        record = load_or_create_approval_intent(
            intent_path,
            requested_record,
        )
        operator = local_azure_operator()
        storage_account = RuntimeProfile.from_env(
            "staging",
            "g30",
        ).registry_storage_account_name
        store = AzureValidationBlobStore(
            storage_account,
            credential=operator.credential,
        )
        store.assert_approved_record_contract(APPROVED_RECORD_CONTAINER)
        blob_name = approved_record_blob_name(
            git.repository,
            git.commit_sha,
        )
        existing = store.read_optional(APPROVED_RECORD_CONTAINER, blob_name)
        if existing is not None:
            validate_approved_record(existing.value)
            _assert_identical_approval(existing, record)
            persisted = existing.value
            status = "already_approved"
        else:
            persisted = store.create_once(
                APPROVED_RECORD_CONTAINER,
                blob_name,
                record,
            ).value
            status = "approved"
        immutable_json(
            validation_runtime_root()
            / "approved-records"
            / blob_name,
            persisted,
        )
        return {
            "status": status,
            "repository": git.repository,
            "pr_number": git.pr_number,
            "commit_sha": git.commit_sha,
            "record_digest": persisted["record_digest"],
        }


def stamp_approved_record(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    result["record_digest"] = ""
    result["record_digest"] = content_hash(
        {key: item for key, item in result.items() if key != "record_digest"}
    )
    return result


def validate_local_result_binding(
    active: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    repository: str,
    pr_number: int,
    commit_sha: str,
    validation_digest: str,
) -> None:
    if (
        active["snapshot_type"] != "active"
        or         active["state"] != "READY"
        or active["repository"] != repository
        or active["pr_number"] != pr_number
        or active["commit_sha"] != commit_sha
        or evidence["repository"] != repository
        or evidence["pr_number"] != pr_number
        or evidence["run_id"] != active["run_id"]
        or evidence["commit_sha"] != commit_sha
        or evidence["validation_digest"] != validation_digest
        or evidence["result"] != "PASS"
        or not all(item["pass"] for item in evidence["authorities"])
        or evidence["runtime_topology_digest"]
        != active["digests"]["runtime_topology_digest"]
        or active["evidence_reference"]["digest"] != evidence["evidence_digest"]
        or active["digests"]["validation_digest"] != validation_digest
        or active["digests"]["evidence_digest"] != evidence["evidence_digest"]
    ):
        raise ContractError(
            "Local PASS evidence does not match the active validation result"
        )


def validate_approved_record(value: Mapping[str, Any]) -> None:
    errors = sorted(
        Draft202012Validator(
            read_json(APPROVED_RECORD_SCHEMA),
            format_checker=FormatChecker(),
        ).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ContractError(
            f"Approved validation record schema error at {location}: "
            f"{error.message}"
        )
    expected = content_hash(
        {key: item for key, item in value.items() if key != "record_digest"}
    )
    if value["record_digest"] != expected:
        raise ContractError("Approved validation record digest is stale")


def validate_approved_record_for_checkout(
    value: Mapping[str, Any],
    *,
    expected_repository: str,
    expected_commit_sha: str | None = None,
) -> dict[str, Any]:
    validate_approved_record(value)
    agents, issues = load_catalogs()
    commit_sha = expected_commit_sha or current_clean_commit()
    if (
        value["repository"] != expected_repository
        or value["commit_sha"] != commit_sha
        or value["validation_digest"] != current_validation_digest(agents, issues)
    ):
        raise ContractError(
            "Approved validation record does not match the exact clean checkout"
        )
    return dict(value)


def fetch_approved_record_for_checkout(
    store: AzureValidationBlobStore,
    *,
    expected_repository: str,
) -> dict[str, Any]:
    checkout_commit_sha = current_clean_commit()
    agents, issues = load_catalogs()
    validation_digest = current_validation_digest(agents, issues)
    store.assert_approved_record_contract(APPROVED_RECORD_CONTAINER)
    exact_name = approved_record_blob_name(
        expected_repository,
        checkout_commit_sha,
    )
    exact = store.read_optional(
        APPROVED_RECORD_CONTAINER,
        exact_name,
    )
    if exact is not None:
        approved = _validate_authoritative_approved_record(
            exact,
            expected_repository=expected_repository,
            expected_commit_sha=checkout_commit_sha,
            expected_name=exact_name,
            expected_validation_digest=validation_digest,
        )
        versions = store.list_approved_records(
            expected_repository,
            exact_name=exact_name,
        )
        if not versions or not any(
            record.version_id == exact.version_id and record.etag == exact.etag
            for record in versions
        ):
            raise ContractError(
                "Exact approved validation record version is absent from its "
                "authoritative listing"
            )
        for record in versions:
            version = _validate_authoritative_approved_record(
                record,
                expected_repository=expected_repository,
                expected_commit_sha=checkout_commit_sha,
                expected_name=exact_name,
                expected_validation_digest=validation_digest,
            )
            if version != approved:
                raise ContractError(
                    "Approved validation record path has conflicting immutable versions"
                )
        return _stamp_approval_binding(
            checkout_commit_sha=checkout_commit_sha,
            approved_record=approved,
        )

    matching: list[tuple[str, str, dict[str, Any]]] = []
    records_by_name: dict[str, dict[str, Any]] = {}
    for record in store.list_approved_records(expected_repository):
        approved = _validate_authoritative_approved_record(
            record,
            expected_repository=expected_repository,
            expected_name=record.name,
        )
        if record.name in records_by_name:
            if records_by_name[record.name] != approved:
                raise ContractError(
                    "Approved validation record path has conflicting immutable "
                    "versions"
                )
            continue
        records_by_name[record.name] = approved
        if approved["validation_digest"] == validation_digest:
            matching.append(
                (
                    record.name,
                    str(approved["record_digest"]),
                    approved,
                )
            )
    if not matching:
        raise ContractError(
            "No authoritative approved validation record matches the current "
            "repository validation digest"
        )
    _, _, approved = min(matching, key=lambda item: (item[0], item[1]))
    return _stamp_approval_binding(
        checkout_commit_sha=checkout_commit_sha,
        approved_record=approved,
    )


def validate_approval_binding(
    value: Mapping[str, Any],
    *,
    expected_checkout_commit_sha: str,
    expected_validation_digest: str,
) -> dict[str, Any]:
    required = {
        "checkout_commit_sha",
        "approved_commit_sha",
        "approved_pr_number",
        "validation_digest",
        "evidence_digest",
        "approved_record_digest",
        "binding_digest",
    }
    if set(value) != required:
        raise ContractError("Daily approval binding fields are invalid")
    if (
        any(
            not isinstance(value[field], str) or not _git_sha(value[field])
            for field in (
                "checkout_commit_sha",
                "approved_commit_sha",
            )
        )
        or not isinstance(value["approved_pr_number"], int)
        or isinstance(value["approved_pr_number"], bool)
        or value["approved_pr_number"] < 1
        or value["checkout_commit_sha"] != expected_checkout_commit_sha
        or value["validation_digest"] != expected_validation_digest
        or not _content_digest(value["evidence_digest"])
        or not _content_digest(value["approved_record_digest"])
        or not _content_digest(value["binding_digest"])
    ):
        raise ContractError("Daily approval binding is invalid")
    expected_digest = content_hash(
        {key: item for key, item in value.items() if key != "binding_digest"}
    )
    if value["binding_digest"] != expected_digest:
        raise ContractError("Daily approval binding digest is stale")
    return dict(value)


def _validate_authoritative_approved_record(
    record: BlobRecord,
    *,
    expected_repository: str,
    expected_name: str,
    expected_commit_sha: str | None = None,
    expected_validation_digest: str | None = None,
) -> dict[str, Any]:
    if (
        record.container != APPROVED_RECORD_CONTAINER
        or record.name != expected_name
        or not isinstance(record.etag, str)
        or not record.etag.strip()
        or not isinstance(record.version_id, str)
        or not record.version_id.strip()
        or record.content != canonical_bytes(record.value)
    ):
        raise ContractError("Authoritative approved record Blob metadata is invalid")
    validate_approved_record(record.value)
    value = dict(record.value)
    if (
        value["repository"] != expected_repository
        or record.name
        != approved_record_blob_name(expected_repository, value["commit_sha"])
        or (
            expected_commit_sha is not None
            and value["commit_sha"] != expected_commit_sha
        )
    ):
        raise ContractError(
            "Authoritative approved validation record identity is invalid"
        )
    if expected_validation_digest is not None and (
        value["validation_digest"] != expected_validation_digest
    ):
        raise ContractError(
            "Approved validation record does not match the current validation digest"
        )
    return value


def _stamp_approval_binding(
    *,
    checkout_commit_sha: str,
    approved_record: Mapping[str, Any],
) -> dict[str, Any]:
    value = {
        "checkout_commit_sha": checkout_commit_sha,
        "approved_commit_sha": approved_record["commit_sha"],
        "approved_pr_number": approved_record["pr_number"],
        "validation_digest": approved_record["validation_digest"],
        "evidence_digest": approved_record["evidence_digest"],
        "approved_record_digest": approved_record["record_digest"],
        "binding_digest": "",
    }
    value["binding_digest"] = content_hash(
        {key: item for key, item in value.items() if key != "binding_digest"}
    )
    return validate_approval_binding(
        value,
        expected_checkout_commit_sha=checkout_commit_sha,
        expected_validation_digest=str(approved_record["validation_digest"]),
    )


def _content_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def approved_record_blob_name(repository: str, commit_sha: str) -> str:
    if (
        len(commit_sha) != 40
        or any(character not in "0123456789abcdef" for character in commit_sha)
    ):
        raise ContractError("Approved validation record path identity is invalid")
    return f"{approved_record_blob_prefix(repository)}{commit_sha}/record.json"


def load_or_create_approval_intent(
    path: Path,
    requested: Mapping[str, Any],
) -> dict[str, Any]:
    if path.exists():
        existing = read_json(path)
        validate_approved_record(existing)
        for field in (
            "repository",
            "pr_number",
            "commit_sha",
            "validation_digest",
            "evidence_digest",
            "generation_digest",
        ):
            if existing[field] != requested[field]:
                raise ContractError(
                    "Existing approval intent targets different validation proof"
                )
        return existing
    immutable_json(path, requested)
    return dict(requested)


def _assert_identical_approval(
    existing: BlobRecord,
    requested: Mapping[str, Any],
) -> None:
    if existing.content != canonical_bytes(requested):
        raise ContractError(
            "Approved validation record path already has different canonical bytes"
        )
