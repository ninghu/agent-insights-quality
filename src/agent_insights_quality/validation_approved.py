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
    commit_sha = current_clean_commit()
    store.assert_approved_record_contract(APPROVED_RECORD_CONTAINER)
    record = store.read(
        APPROVED_RECORD_CONTAINER,
        approved_record_blob_name(expected_repository, commit_sha),
    )
    if (
        not record.etag
        or not record.version_id
        or record.content != canonical_bytes(record.value)
    ):
        raise ContractError("Authoritative approved record Blob metadata is invalid")
    return validate_approved_record_for_checkout(
        record.value,
        expected_repository=expected_repository,
        expected_commit_sha=commit_sha,
    )


def approved_record_blob_name(repository: str, commit_sha: str) -> str:
    owner, name = repository.split("/", 1)
    if (
        not owner
        or not name
        or len(commit_sha) != 40
        or any(character not in "0123456789abcdef" for character in commit_sha)
    ):
        raise ContractError("Approved validation record path identity is invalid")
    return f"approved-validation-records/{owner}/{name}/{commit_sha}/record.json"


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
