from __future__ import annotations

from copy import deepcopy
import json

import pytest

from agent_insights_quality.util import ROOT, ContractError
from agent_insights_quality.validation_approved import (
    approved_record_blob_name,
    stamp_approved_record,
    validate_approved_record,
    validate_local_result_binding,
)
from agent_insights_quality.validation_lifecycle import stamp_lifecycle_digest

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
    fixture = ROOT / "tests" / "fixtures" / "test_agent_validation" / "r01"
    evidence = json.loads((fixture / "evidence.json").read_text(encoding="utf-8"))
    clean = json.loads(
        (fixture / "clean-lifecycle.json").read_text(encoding="utf-8")
    )
    active = deepcopy(clean)
    active["snapshot_type"] = "active"
    active["event_reference"] = {
        "path": "history/synthetic.json",
        "digest": "sha256:" + ("e" * 64),
    }
    active["clean_reference"] = {
        "path": "clean/synthetic.json",
        "digest": clean["journal_digest"],
    }
    active = stamp_lifecycle_digest(active)
    changed = deepcopy(evidence)
    changed["cycle_id"] = "validation-other-cycle"
    with pytest.raises(ContractError, match="one validation cycle"):
        validate_local_result_binding(
            active,
            clean,
            changed,
            repository=clean["repository"],
            pr_number=clean["pr_number"],
            commit_sha=clean["commit_sha"],
            validation_digest=clean["digests"]["validation_digest"],
        )
