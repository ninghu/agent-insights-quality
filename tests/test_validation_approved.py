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


def _record() -> dict:
    return stamp_approved_record(
        {
            "schema_version": "1.0.0",
            "kind": "test-agent-validation-approved-record",
            "repository": "ninghu/agent-insights-quality",
            "pr_number": 63,
            "commit_sha": "b" * 40,
            "validation_digest": HASH,
            "evidence_digest": "sha256:" + ("c" * 64),
            "clean_digest": "sha256:" + ("d" * 64),
            "approved_by": "synthetic-approver",
            "approved_at": "2026-08-29T12:00:00+00:00",
            "record_digest": "",
        }
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
        "clean_digest",
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


def test_approval_rejects_evidence_from_another_cycle() -> None:
    clean = {
        "snapshot_type": "clean",
        "state": "CLEAN",
        "repository": "ninghu/agent-insights-quality",
        "pr_number": 63,
        "commit_sha": "b" * 40,
        "cycle_id": "validation-cycle-a",
        "cleanup": {
            "exact_clean": True,
            "residue_ids": [],
        },
    }
    evidence = {
        "repository": clean["repository"],
        "pr_number": clean["pr_number"],
        "cycle_id": "validation-cycle-b",
    }
    with pytest.raises(ContractError, match="one validation cycle"):
        validate_local_result_binding(
            {},
            clean,
            evidence,
            repository=clean["repository"],
            pr_number=clean["pr_number"],
            commit_sha=clean["commit_sha"],
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
    observed = {}

    class Store:
        def assert_approved_record_contract(self, container):
            observed["container"] = container

        def read(self, container, name):
            observed["read"] = (container, name)
            return BlobRecord(
                container,
                name,
                value,
                "etag",
                "version",
                canonical_bytes(value),
            )

    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.current_clean_commit",
        lambda: value["commit_sha"],
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_approved.validate_approved_record_for_checkout",
        lambda record, **_kwargs: dict(record),
    )
    assert fetch_approved_record_for_checkout(
        Store(),
        expected_repository=value["repository"],
    ) == value
    assert observed["read"][1].endswith(
        f"/{value['commit_sha']}/record.json"
    )
    assert observed["container"] == APPROVED_RECORD_CONTAINER
    assert observed["read"][0] == APPROVED_RECORD_CONTAINER
