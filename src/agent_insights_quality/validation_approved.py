from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

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
    _git_sha,
    _run_json,
    _run_json_array,
    current_clean_commit,
    current_tree_sha,
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
        )
        tree_sha = current_tree_sha(checkout_commit_sha)
        return _stamp_approval_binding(
            checkout_commit_sha=checkout_commit_sha,
            approved_record=approved,
            tree_sha=tree_sha,
        )

    source = _resolve_merged_approval_source(
        expected_repository,
        checkout_commit_sha,
    )
    approved_name = approved_record_blob_name(
        expected_repository,
        source["approved_commit_sha"],
    )
    approved_blob = store.read(
        APPROVED_RECORD_CONTAINER,
        approved_name,
    )
    approved = _validate_authoritative_approved_record(
        approved_blob,
        expected_repository=expected_repository,
        expected_commit_sha=source["approved_commit_sha"],
        expected_name=approved_name,
        expected_pr_number=source["approved_pr_number"],
    )
    return _stamp_approval_binding(
        checkout_commit_sha=checkout_commit_sha,
        approved_record=approved,
        tree_sha=source["tree_sha"],
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
        "tree_sha",
        "validation_digest",
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
                "tree_sha",
            )
        )
        or value["checkout_commit_sha"] != expected_checkout_commit_sha
        or value["validation_digest"] != expected_validation_digest
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
    expected_commit_sha: str,
    expected_name: str,
    expected_pr_number: int | None = None,
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
    value = validate_approved_record_for_checkout(
        record.value,
        expected_repository=expected_repository,
        expected_commit_sha=expected_commit_sha,
    )
    if expected_pr_number is not None and value["pr_number"] != expected_pr_number:
        raise ContractError("Approved validation record does not match the merged pull request")
    return value


def _stamp_approval_binding(
    *,
    checkout_commit_sha: str,
    approved_record: Mapping[str, Any],
    tree_sha: str,
) -> dict[str, Any]:
    value = {
        "checkout_commit_sha": checkout_commit_sha,
        "approved_commit_sha": approved_record["commit_sha"],
        "tree_sha": tree_sha,
        "validation_digest": approved_record["validation_digest"],
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


def _resolve_merged_approval_source(
    repository: str,
    checkout_commit_sha: str,
) -> dict[str, Any]:
    repository_value = _github_object(
        f"repos/{repository}",
        "GitHub repository",
    )
    default_branch = repository_value.get("default_branch")
    if (
        repository_value.get("full_name") != repository
        or not isinstance(default_branch, str)
        or not default_branch
        or default_branch.strip() != default_branch
    ):
        raise ContractError("GitHub default branch response is invalid")
    branch_value = _github_object(
        f"repos/{repository}/branches/{quote(default_branch, safe='')}",
        "GitHub default branch",
    )
    branch_commit = branch_value.get("commit")
    default_head_sha = (
        branch_commit.get("sha") if isinstance(branch_commit, Mapping) else None
    )
    if branch_value.get("name") != default_branch or default_head_sha != (
        checkout_commit_sha
    ):
        raise ContractError("Current clean commit is not the GitHub default branch head")

    pulls = _associated_pulls(repository, checkout_commit_sha)
    merged_pulls = [
        pull
        for pull in pulls
        if isinstance(pull.get("merged_at"), str) and pull["merged_at"].strip()
    ]
    if len(merged_pulls) != 1:
        raise ContractError(
            "GitHub must associate exactly one merged pull request with "
            "the default branch head"
        )
    pull = merged_pulls[0]
    number = pull.get("number")
    head = pull.get("head")
    base = pull.get("base")
    head_sha = head.get("sha") if isinstance(head, Mapping) else None
    base_repository = base.get("repo") if isinstance(base, Mapping) else None
    base_full_name = (
        base_repository.get("full_name")
        if isinstance(base_repository, Mapping)
        else None
    )
    if (
        not isinstance(number, int)
        or isinstance(number, bool)
        or number < 1
        or pull.get("state") != "closed"
        or not isinstance(pull.get("merged_at"), str)
        or not pull["merged_at"].strip()
        or pull.get("merge_commit_sha") != checkout_commit_sha
        or not isinstance(head_sha, str)
        or not _git_sha(head_sha)
        or base_full_name != repository
    ):
        raise ContractError("GitHub merged pull request response is invalid")

    checkout_tree_sha = _github_tree_sha(repository, checkout_commit_sha)
    approved_tree_sha = _github_tree_sha(repository, head_sha)
    if checkout_tree_sha != approved_tree_sha:
        raise ContractError(
            "Merged checkout tree does not match the approved pull request head tree"
        )
    return {
        "approved_pr_number": number,
        "approved_commit_sha": head_sha,
        "tree_sha": checkout_tree_sha,
    }


def _associated_pulls(
    repository: str,
    commit_sha: str,
) -> list[dict[str, Any]]:
    pulls: list[dict[str, Any]] = []
    seen_numbers: set[int] = set()
    page = 1
    while True:
        values = _github_array(
            (
                f"repos/{repository}/commits/{commit_sha}/pulls"
                f"?per_page=100&page={page}"
            ),
            "GitHub associated pull requests",
        )
        for pull in values:
            number = pull.get("number")
            merged_at = pull.get("merged_at")
            if (
                not isinstance(number, int)
                or isinstance(number, bool)
                or number < 1
                or number in seen_numbers
                or (
                    merged_at is not None
                    and (
                        not isinstance(merged_at, str)
                        or not merged_at.strip()
                    )
                )
            ):
                raise ContractError("GitHub associated pull request response is invalid")
            seen_numbers.add(number)
        pulls.extend(values)
        if len(values) < 100:
            return pulls
        page += 1


def _github_tree_sha(repository: str, commit_sha: str) -> str:
    value = _github_object(
        f"repos/{repository}/git/commits/{commit_sha}",
        "GitHub commit",
    )
    tree = value.get("tree")
    tree_sha = tree.get("sha") if isinstance(tree, Mapping) else None
    if (
        value.get("sha") != commit_sha
        or not isinstance(tree_sha, str)
        or not _git_sha(tree_sha)
    ):
        raise ContractError("GitHub commit tree response is invalid")
    return tree_sha


def _github_object(path: str, label: str) -> dict[str, Any]:
    return _run_json(_github_arguments(path), label)


def _github_array(path: str, label: str) -> list[dict[str, Any]]:
    return _run_json_array(_github_arguments(path), label)


def _github_arguments(path: str) -> list[str]:
    return [
        "gh",
        "api",
        "--method",
        "GET",
        path,
        "--header",
        "Accept: application/vnd.github+json",
        "--header",
        "X-GitHub-Api-Version: 2022-11-28",
    ]


def _content_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
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
