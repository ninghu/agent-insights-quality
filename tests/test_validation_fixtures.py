from __future__ import annotations

import json

import pytest

from agent_insights_quality.util import ROOT, ContractError
from agent_insights_quality.validation_approved import validate_approved_record
from agent_insights_quality.validation_evidence import validate_evidence
from agent_insights_quality.validation_lifecycle import validate_lifecycle


def test_sanitized_r01_fixture_is_retained_as_superseded_history() -> None:
    root = ROOT / "tests" / "fixtures" / "test_agent_validation"
    run = root / "r01"
    evidence = json.loads((run / "evidence.json").read_text())
    clean = json.loads((run / "clean-lifecycle.json").read_text())
    approved = json.loads((run / "approved-record.json").read_text())
    durations = json.loads((run / "durations.json").read_text())
    with pytest.raises(ContractError, match="judge_model"):
        validate_evidence(
            evidence,
            runtime_topology=clean["runtime_topology"],
        )
    validate_lifecycle(clean)
    with pytest.raises(ContractError, match="judge_model"):
        validate_approved_record(approved)
    assert len(evidence["authorities"]) == 41
    assert all(item["pass"] for item in evidence["authorities"])
    assert clean["state"] == "CLEAN"
    assert clean["cleanup"]["exact_clean"] is True
    assert clean["project"]["state"] == "deleted"
    assert approved["commit_sha"] == evidence["commit_sha"] == clean["commit_sha"]
    assert approved["evidence_digest"] == evidence["evidence_digest"]
    assert approved["clean_digest"] == clean["journal_digest"]
    assert durations["percentiles_calculated"] is False
    assert not (root / "r02").exists()


def test_local_validation_fixtures_have_no_raw_or_private_values() -> None:
    root = ROOT / "tests" / "fixtures" / "test_agent_validation"
    forbidden_keys = {"input", "output", "prompt", "raw_trace", "response_body"}
    for path in root.rglob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        text = json.dumps(value, sort_keys=True).casefold()
        assert "/subscriptions/" not in text
        assert "https://" not in text
        assert "@microsoft.com" not in text

        def keys(item):
            if isinstance(item, dict):
                return set(item).union(*(keys(child) for child in item.values()))
            if isinstance(item, list):
                return set().union(*(keys(child) for child in item))
            return set()

        assert forbidden_keys.isdisjoint(keys(value))
