from __future__ import annotations

from copy import deepcopy

import pytest

from agent_insights_quality.util import ContractError, canonical_bytes
from agent_insights_quality.validation_approved import (
    APPROVED_RECORD_CONTAINER,
    _assert_identical_approval,
    _resolve_merged_approval_source,
    approved_record_blob_name,
    fetch_approved_record_for_checkout,
    load_or_create_approval_intent,
    stamp_approved_record,
    validate_approved_record,
    validate_local_result_binding,
)
from agent_insights_quality.validation_blob import BlobRecord

HASH = "sha256:" + ("a" * 64)


def _record(
    *,
    commit_sha: str = "b" * 40,
    pr_number: int = 63,
    repository: str = "ninghu/agent-insights-quality",
    validation_digest: str = HASH,
) -> dict:
    return stamp_approved_record(
        {
            "schema_version": "2.0.0",
            "kind": "test-agent-validation-approved-record",
            "repository": repository,
            "pr_number": pr_number,
            "commit_sha": commit_sha,
            "validation_digest": validation_digest,
            "evidence_digest": "sha256:" + ("c" * 64),
            "generation_digest": "sha256:" + ("d" * 64),
            "approved_by": "synthetic-approver",
            "approved_at": "2026-08-29T12:00:00+00:00",
            "record_digest": "",
        }
    )


def _blob(value: dict, *, name: str | None = None) -> BlobRecord:
    blob_name = name or approved_record_blob_name(
        value["repository"],
        value["commit_sha"],
    )
    return BlobRecord(
        APPROVED_RECORD_CONTAINER,
        blob_name,
        value,
        "etag",
        "version",
        canonical_bytes(value),
    )


class _Store:
    def __init__(self, records: list[BlobRecord]) -> None:
        self.records = {record.name: record for record in records}
        self.reads: list[str] = []
        self.asserted_container: str | None = None

    def assert_approved_record_contract(self, container: str) -> None:
        self.asserted_container = container

    def read_optional(self, container: str, name: str) -> BlobRecord | None:
        assert container == APPROVED_RECORD_CONTAINER
        self.reads.append(name)
        return self.records.get(name)

    def read(self, container: str, name: str) -> BlobRecord:
        assert container == APPROVED_RECORD_CONTAINER
        self.reads.append(name)
        return self.records[name]


def _pull(
    checkout_commit_sha: str,
    approved_commit_sha: str,
    *,
    number: int = 63,
) -> dict:
    return {
        "number": number,
        "state": "closed",
        "merged_at": "2026-09-01T12:00:00Z",
        "merge_commit_sha": checkout_commit_sha,
        "head": {"sha": approved_commit_sha},
        "base": {"repo": {"full_name": "ninghu/agent-insights-quality"}},
    }


def _github_responses(
    monkeypatch,
    *,
    checkout_commit_sha: str,
    approved_commit_sha: str,
    checkout_tree_sha: str = "c" * 40,
    approved_tree_sha: str = "c" * 40,
    default_head_sha: str | None = None,
    pulls: list[dict] | None = None,
) -> None:
    def github_object(path: str, _label: str) -> dict:
        if path == "repos/ninghu/agent-insights-quality":
            return {
                "full_name": "ninghu/agent-insights-quality",
                "default_branch": "main",
            }
        if path == "repos/ninghu/agent-insights-quality/branches/main":
            return {
                "name": "main",
                "commit": {"sha": default_head_sha or checkout_commit_sha},
            }
        if path.endswith(checkout_commit_sha):
            return {
                "sha": checkout_commit_sha,
                "tree": {"sha": checkout_tree_sha},
            }
        if path.endswith(approved_commit_sha):
            return {
                "sha": approved_commit_sha,
                "tree": {"sha": approved_tree_sha},
            }
        raise AssertionError(path)

    monkeypatch.setattr(
        "agent_insights_quality.validation_approved._github_object",
        github_object,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved._github_array",
        lambda _path, _label: (
            pulls
            if pulls is not None
            else [_pull(checkout_commit_sha, approved_commit_sha)]
        ),
    )


def test_approved_record_is_minimal_and_self_bound() -> None:
    value = _record()
    validate_approved_record(value)
    assert set(value) == {
        "schema_version",
        "kind",
        "repository",
        "pr_number",
        "commit_sha",
        "validation_digest",
        "evidence_digest",
        "generation_digest",
        "approved_by",
        "approved_at",
        "record_digest",
    }
    assert approved_record_blob_name(
        value["repository"],
        value["commit_sha"],
    ) == (
        "approved-validation-records/ninghu/agent-insights-quality/"
        + ("b" * 40)
        + "/record.json"
    )


def test_approved_record_rejects_tamper_or_gate_provenance() -> None:
    changed = deepcopy(_record())
    changed["commit_sha"] = "e" * 40
    with pytest.raises(ContractError, match="digest is stale"):
        validate_approved_record(changed)

    changed = deepcopy(_record())
    changed["workflow"] = "removed"
    with pytest.raises(ContractError, match="schema error"):
        validate_approved_record(changed)


def test_approval_rejects_evidence_from_another_run() -> None:
    active = {
        "snapshot_type": "active",
        "state": "READY",
        "repository": "ninghu/agent-insights-quality",
        "pr_number": 63,
        "commit_sha": "b" * 40,
        "run_id": "validation-aaaaaaaaaaaa",
        "digests": {
            "validation_digest": HASH,
            "runtime_topology_digest": HASH,
            "evidence_digest": "sha256:" + ("c" * 64),
        },
        "evidence_reference": {"digest": "sha256:" + ("c" * 64)},
    }
    evidence = {
        "repository": active["repository"],
        "pr_number": active["pr_number"],
        "run_id": "validation-bbbbbbbbbbbb",
        "commit_sha": active["commit_sha"],
        "validation_digest": HASH,
        "result": "PASS",
        "runtime_topology_digest": HASH,
        "evidence_digest": "sha256:" + ("c" * 64),
        "authorities": [{"pass": True}],
    }
    with pytest.raises(ContractError, match="active validation"):
        validate_local_result_binding(
            active,
            evidence,
            repository=active["repository"],
            pr_number=active["pr_number"],
            commit_sha=active["commit_sha"],
            validation_digest=HASH,
        )


def test_approval_retry_reuses_original_byte_identical_intent(tmp_path) -> None:
    path = tmp_path / "approval-intent.json"
    original = _record()
    assert load_or_create_approval_intent(path, original) == original
    retry = deepcopy(original)
    retry["approved_by"] = "different-user"
    retry["approved_at"] = "2026-08-30T12:00:00+00:00"
    retry = stamp_approved_record(retry)
    assert load_or_create_approval_intent(path, retry) == original
    _assert_identical_approval(
        BlobRecord(
            "container",
            "record",
            original,
            "etag",
            "version",
            canonical_bytes(original),
        ),
        original,
    )
    with pytest.raises(ContractError, match="different canonical bytes"):
        _assert_identical_approval(
            BlobRecord(
                "container",
                "record",
                original,
                "etag",
                "version",
                canonical_bytes(retry),
            ),
            original,
        )


def test_daily_fetches_exact_head_authoritative_blob(monkeypatch) -> None:
    value = _record()
    store = _Store([_blob(value)])

    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_clean_commit",
        lambda: value["commit_sha"],
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_tree_sha",
        lambda _commit_sha: "c" * 40,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_validation_digest",
        lambda *_args: HASH,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved._resolve_merged_approval_source",
        lambda *_args: pytest.fail("exact approval lookup must not query GitHub"),
    )
    binding = fetch_approved_record_for_checkout(
        store,
        expected_repository=value["repository"],
    )

    assert binding["checkout_commit_sha"] == value["commit_sha"]
    assert binding["approved_commit_sha"] == value["commit_sha"]
    assert binding["approved_record_digest"] == value["record_digest"]
    assert binding["tree_sha"] == "c" * 40
    assert store.reads[0].endswith(
        f"/{value['commit_sha']}/record.json"
    )
    assert store.asserted_container == APPROVED_RECORD_CONTAINER


def test_daily_bridges_valid_squash_merge_to_exact_approved_head(
    monkeypatch,
) -> None:
    checkout_commit_sha = "a" * 40
    approved_commit_sha = "b" * 40
    value = _record(commit_sha=approved_commit_sha)
    store = _Store([_blob(value)])
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_clean_commit",
        lambda: checkout_commit_sha,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_validation_digest",
        lambda *_args: HASH,
    )
    _github_responses(
        monkeypatch,
        checkout_commit_sha=checkout_commit_sha,
        approved_commit_sha=approved_commit_sha,
    )

    binding = fetch_approved_record_for_checkout(
        store,
        expected_repository=value["repository"],
    )

    assert binding["checkout_commit_sha"] == checkout_commit_sha
    assert binding["approved_commit_sha"] == approved_commit_sha
    assert binding["tree_sha"] == "c" * 40
    assert binding["approved_record_digest"] == value["record_digest"]
    assert store.reads == [
        approved_record_blob_name(value["repository"], checkout_commit_sha),
        approved_record_blob_name(value["repository"], approved_commit_sha),
    ]


def test_squash_bridge_rejects_non_default_branch_head(monkeypatch) -> None:
    checkout_commit_sha = "a" * 40
    _github_responses(
        monkeypatch,
        checkout_commit_sha=checkout_commit_sha,
        approved_commit_sha="b" * 40,
        default_head_sha="d" * 40,
    )

    with pytest.raises(ContractError, match="not the GitHub default branch head"):
        _resolve_merged_approval_source(
            "ninghu/agent-insights-quality",
            checkout_commit_sha,
        )


@pytest.mark.parametrize(
    "pulls",
    [
        [],
        [
            _pull("a" * 40, "b" * 40),
            _pull("a" * 40, "d" * 40, number=64),
        ],
    ],
)
def test_squash_bridge_rejects_zero_or_multiple_associated_pulls(
    monkeypatch,
    pulls: list[dict],
) -> None:
    checkout_commit_sha = "a" * 40
    _github_responses(
        monkeypatch,
        checkout_commit_sha=checkout_commit_sha,
        approved_commit_sha="b" * 40,
        pulls=pulls,
    )

    with pytest.raises(ContractError, match="exactly one merged pull request"):
        _resolve_merged_approval_source(
            "ninghu/agent-insights-quality",
            checkout_commit_sha,
        )


@pytest.mark.parametrize(
    "pull",
    [
        {
            **_pull("a" * 40, "b" * 40),
            "state": "open",
            "merged_at": None,
        },
        {
            **_pull("a" * 40, "b" * 40),
            "merge_commit_sha": "d" * 40,
        },
    ],
)
def test_squash_bridge_rejects_unmerged_or_mismatched_merge(
    monkeypatch,
    pull: dict,
) -> None:
    checkout_commit_sha = "a" * 40
    _github_responses(
        monkeypatch,
        checkout_commit_sha=checkout_commit_sha,
        approved_commit_sha="b" * 40,
        pulls=[pull],
    )

    with pytest.raises(ContractError, match="pull request"):
        _resolve_merged_approval_source(
            "ninghu/agent-insights-quality",
            checkout_commit_sha,
        )


def test_squash_bridge_rejects_tree_mismatch(monkeypatch) -> None:
    checkout_commit_sha = "a" * 40
    _github_responses(
        monkeypatch,
        checkout_commit_sha=checkout_commit_sha,
        approved_commit_sha="b" * 40,
        approved_tree_sha="d" * 40,
    )

    with pytest.raises(ContractError, match="does not match"):
        _resolve_merged_approval_source(
            "ninghu/agent-insights-quality",
            checkout_commit_sha,
        )


def test_squash_bridge_rejects_record_for_another_pull(monkeypatch) -> None:
    checkout_commit_sha = "a" * 40
    approved_commit_sha = "b" * 40
    value = _record(commit_sha=approved_commit_sha, pr_number=64)
    store = _Store([_blob(value)])
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_clean_commit",
        lambda: checkout_commit_sha,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_validation_digest",
        lambda *_args: HASH,
    )
    _github_responses(
        monkeypatch,
        checkout_commit_sha=checkout_commit_sha,
        approved_commit_sha=approved_commit_sha,
    )

    with pytest.raises(ContractError, match="does not match the merged pull request"):
        fetch_approved_record_for_checkout(
            store,
            expected_repository=value["repository"],
        )
