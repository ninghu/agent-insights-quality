from __future__ import annotations

from copy import deepcopy

import pytest

from agent_insights_quality.util import ContractError, canonical_bytes
from agent_insights_quality.validation_approved import (
    APPROVED_RECORD_CONTAINER,
    _assert_identical_approval,
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


def _blob(
    value: dict,
    *,
    name: str | None = None,
    etag: str = "etag",
    version_id: str = "version",
) -> BlobRecord:
    blob_name = name or approved_record_blob_name(
        value["repository"],
        value["commit_sha"],
    )
    return BlobRecord(
        APPROVED_RECORD_CONTAINER,
        blob_name,
        value,
        etag,
        version_id,
        canonical_bytes(value),
    )


class _Store:
    def __init__(self, records: list[BlobRecord]) -> None:
        self.records = {record.name: record for record in records}
        self.listed_records = list(records)
        self.reads: list[str] = []
        self.asserted_container: str | None = None
        self.listed_repository: str | None = None

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

    def list_approved_records(self, repository: str) -> list[BlobRecord]:
        self.listed_repository = repository
        return list(self.listed_records)


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
        "agent_insights_quality.validation_approved.current_validation_digest",
        lambda *_args: HASH,
    )
    binding = fetch_approved_record_for_checkout(
        store,
        expected_repository=value["repository"],
    )

    assert binding["checkout_commit_sha"] == value["commit_sha"]
    assert binding["approved_commit_sha"] == value["commit_sha"]
    assert binding["approved_pr_number"] == value["pr_number"]
    assert binding["evidence_digest"] == value["evidence_digest"]
    assert binding["approved_record_digest"] == value["record_digest"]
    assert store.reads[0].endswith(
        f"/{value['commit_sha']}/record.json"
    )
    assert store.listed_repository is None
    assert store.asserted_container == APPROVED_RECORD_CONTAINER


def test_daily_reuses_pr_65_record_for_matching_current_validation_digest(
    monkeypatch,
) -> None:
    checkout_commit_sha = "a" * 40
    approved_commit_sha = "b" * 40
    value = _record(commit_sha=approved_commit_sha, pr_number=65)
    store = _Store([_blob(value)])
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_clean_commit",
        lambda: checkout_commit_sha,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_validation_digest",
        lambda *_args: HASH,
    )

    binding = fetch_approved_record_for_checkout(
        store,
        expected_repository=value["repository"],
    )

    assert binding["checkout_commit_sha"] == checkout_commit_sha
    assert binding["approved_commit_sha"] == approved_commit_sha
    assert binding["approved_pr_number"] == 65
    assert binding["evidence_digest"] == value["evidence_digest"]
    assert binding["approved_record_digest"] == value["record_digest"]
    assert store.reads == [
        approved_record_blob_name(value["repository"], checkout_commit_sha)
    ]
    assert store.listed_repository == value["repository"]


def test_digest_fallback_selects_deterministically_from_equivalent_records(
    monkeypatch,
) -> None:
    checkout_commit_sha = "a" * 40
    first = _record(commit_sha="b" * 40, pr_number=65)
    second = _record(commit_sha="c" * 40, pr_number=66)
    store = _Store([_blob(second), _blob(first)])
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_clean_commit",
        lambda: checkout_commit_sha,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_validation_digest",
        lambda *_args: HASH,
    )

    binding = fetch_approved_record_for_checkout(
        store,
        expected_repository=first["repository"],
    )

    assert binding["approved_commit_sha"] == first["commit_sha"]
    assert binding["approved_pr_number"] == 65
    assert binding["approved_record_digest"] == first["record_digest"]


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (
            _record(repository="synthetic/other"),
            "identity is invalid",
        ),
        (
            _record(validation_digest="sha256:" + ("e" * 64)),
            "No authoritative approved validation record",
        ),
    ],
)
def test_digest_fallback_rejects_mismatched_repository_or_digest(
    monkeypatch,
    record: dict,
    message: str,
) -> None:
    checkout_commit_sha = "a" * 40
    expected_repository = "ninghu/agent-insights-quality"
    store = _Store(
        [
            _blob(
                record,
                name=approved_record_blob_name(
                    expected_repository,
                    record["commit_sha"],
                ),
            )
        ]
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_clean_commit",
        lambda: checkout_commit_sha,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_validation_digest",
        lambda *_args: HASH,
    )

    with pytest.raises(ContractError, match=message):
        fetch_approved_record_for_checkout(
            store,
            expected_repository=expected_repository,
        )


def test_digest_fallback_fails_closed_on_malformed_candidate(monkeypatch) -> None:
    checkout_commit_sha = "a" * 40
    valid = _record(commit_sha="b" * 40)
    malformed = deepcopy(_record(commit_sha="c" * 40))
    malformed.pop("evidence_digest")
    store = _Store([_blob(valid), _blob(malformed)])
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_clean_commit",
        lambda: checkout_commit_sha,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_validation_digest",
        lambda *_args: HASH,
    )

    with pytest.raises(ContractError, match="schema error"):
        fetch_approved_record_for_checkout(
            store,
            expected_repository=valid["repository"],
        )


def test_digest_fallback_fails_closed_on_conflicting_blob_name(monkeypatch) -> None:
    checkout_commit_sha = "a" * 40
    first = _record(commit_sha="b" * 40, pr_number=65)
    second = _record(commit_sha="b" * 40, pr_number=66)
    store = _Store([_blob(first), _blob(second)])
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_clean_commit",
        lambda: checkout_commit_sha,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_validation_digest",
        lambda *_args: HASH,
    )

    with pytest.raises(ContractError, match="conflicting Blob name"):
        fetch_approved_record_for_checkout(
            store,
            expected_repository=first["repository"],
        )


@pytest.mark.parametrize(("etag", "version_id"), [("", "version"), ("etag", "")])
def test_digest_fallback_rejects_missing_blob_metadata(
    monkeypatch,
    etag: str,
    version_id: str,
) -> None:
    checkout_commit_sha = "a" * 40
    value = _record()
    store = _Store([_blob(value, etag=etag, version_id=version_id)])
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_clean_commit",
        lambda: checkout_commit_sha,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_validation_digest",
        lambda *_args: HASH,
    )

    with pytest.raises(ContractError, match="Blob metadata is invalid"):
        fetch_approved_record_for_checkout(
            store,
            expected_repository=value["repository"],
        )
