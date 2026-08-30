from __future__ import annotations

from copy import deepcopy

import pytest

from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_approved import (
    approved_record_blob_name,
    stamp_approved_record,
    validate_approved_record,
)

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
