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
    content_hash,
    immutable_json,
    read_json,
    runtime_root,
)
from agent_insights_quality.validation_blob import AzureValidationBlobStore
from agent_insights_quality.validation_credentials import local_azure_operator
from agent_insights_quality.validation_evidence import validate_evidence
from agent_insights_quality.validation_lifecycle import (
    LifecycleJournal,
    LocalValidationLock,
    read_bound_local_record,
    validate_lifecycle,
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
APPROVED_RECORD_CONTAINER = "test-agent-validation-approved-records"


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
            active.value["state"] != "CLEAN"
            or active.value["commit_sha"] != git.commit_sha
            or active.value["repository"] != git.repository
            or active.value["pr_number"] != git.pr_number
        ):
            raise ContractError(
                "Latest local validation is not CLEAN for the current pull request head"
            )
        clean_record = read_bound_local_record(
            journal.root,
            active.value["clean_reference"],
            digest_field="journal_digest",
            label="CLEAN",
        )
        clean = clean_record.value
        validate_lifecycle(clean)
        evidence_record = read_bound_local_record(
            runtime_root() / "test-agent-validation",
            active.value["evidence_reference"],
            digest_field="evidence_digest",
            label="evidence",
        )
        evidence = evidence_record.value
        validate_evidence(evidence)
        agents, issues = load_catalogs()
        validation_digest = current_validation_digest(agents, issues)
        if (
            clean["snapshot_type"] != "clean"
            or clean["state"] != "CLEAN"
            or clean["commit_sha"] != git.commit_sha
            or clean["cleanup"]["exact_clean"] is not True
            or clean["cleanup"]["residue_ids"]
            or evidence["commit_sha"] != git.commit_sha
            or evidence["validation_digest"] != validation_digest
            or active.value["digests"]["validation_digest"] != validation_digest
            or active.value["digests"]["evidence_digest"]
            != evidence["evidence_digest"]
        ):
            raise ContractError(
                "Local evidence or CLEAN proof does not match the current commit"
            )
        record = stamp_approved_record(
            {
                "schema_version": "1.0.0",
                "kind": "test-agent-validation-approved-record",
                "repository": git.repository,
                "pr_number": git.pr_number,
                "commit_sha": git.commit_sha,
                "validation_digest": validation_digest,
                "evidence_digest": evidence["evidence_digest"],
                "clean_digest": clean["journal_digest"],
                "approved_by": approver,
                "approved_at": now().astimezone(UTC).isoformat(),
                "record_digest": "",
            }
        )
        validate_approved_record(record)
        operator = local_azure_operator()
        storage_account = RuntimeProfile.from_env(
            "staging",
            "g29",
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
            _assert_same_approval_bindings(existing.value, record)
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
            runtime_root()
            / "test-agent-validation"
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
    path: Path,
    *,
    expected_repository: str,
) -> dict[str, Any]:
    value = read_json(path)
    validate_approved_record(value)
    agents, issues = load_catalogs()
    if (
        value["repository"] != expected_repository
        or value["commit_sha"] != current_clean_commit()
        or value["validation_digest"] != current_validation_digest(agents, issues)
    ):
        raise ContractError(
            "Approved validation record does not match the exact clean checkout"
        )
    return value


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


def _assert_same_approval_bindings(
    existing: Mapping[str, Any],
    requested: Mapping[str, Any],
) -> None:
    for field in (
        "repository",
        "pr_number",
        "commit_sha",
        "validation_digest",
        "evidence_digest",
        "clean_digest",
    ):
        if existing[field] != requested[field]:
            raise ContractError(
                "Approved validation record path already has different content"
            )
