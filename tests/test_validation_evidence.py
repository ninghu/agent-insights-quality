from __future__ import annotations

from copy import deepcopy

import pytest

from agent_insights_quality.util import ContractError, content_hash
from agent_insights_quality.validation_evidence import (
    persist_evidence,
    stamp_evidence_digests,
    validate_evidence,
)
from agent_insights_quality.validation_blob import BlobRecord

HASH = "sha256:" + ("a" * 64)
HEAD = "b" * 40


def _step(index: int, *, assertion_pass: bool = True) -> dict:
    return {
        "index": index,
        "request_digest": content_hash({"request": index}),
        "response_reference": content_hash({"response": index}),
        "operation_reference": content_hash({"operation": index}),
        "complete": True,
        "endpoint_pass": True,
        "semantic_pass": assertion_pass,
        "trace_pass": True,
        "identity_pass": True,
    }


def _attempt(index: int, *, observed: bool, expected: bool) -> dict:
    setup = _step(index * 10)
    probe = _step(index * 10 + 1, assertion_pass=observed)
    return {
        "index": index,
        "conversation_reference": content_hash({"conversation": index}),
        "session_reference": content_hash({"session": index}),
        "response_references": [
            setup["response_reference"],
            probe["response_reference"],
        ],
        "operation_references": [
            setup["operation_reference"],
            probe["operation_reference"],
        ],
        "setup_steps": [setup],
        "probe_steps": [probe],
        "complete": True,
        "defect_observed": observed,
        "expected_observation_pass": expected,
        "error_code": None,
    }


def _authority(
    authority_id: str,
    agent: str,
    *,
    mode: str,
    observed: int,
) -> dict:
    baseline = authority_id.endswith("/v0")
    n = 7 if mode == "model_mediated" else 5
    issue_attempts = [
        _attempt(
            index,
            observed=(not baseline and index <= observed),
            expected=(baseline or index <= observed),
        )
        for index in range(1, n + 1)
    ]
    v0_attempts = (
        []
        if baseline
        else [
            _attempt(index, observed=False, expected=True)
            for index in range(1, n + 1)
        ]
    )
    scenario = {
        "scenario_id": "reviewed-path",
        "execution_digest": HASH,
        "validation_mode": mode,
        "n": n,
        "k": 5,
        "complete_count": n,
        "observed": observed,
        "pass": True,
        "issue_attempts": issue_attempts,
        "v0_attempts": v0_attempts,
    }
    return {
        "authority_id": authority_id,
        "authority_kind": "baseline" if baseline else "issue",
        "canonical_agent": agent,
        "logical_version": "v0" if baseline else authority_id,
        "runtime_agent_name": f"{agent}-candidate",
        "provider_agent_version_reference": HASH,
        "source_content_digest": HASH,
        "execution_digest": HASH,
        "validated_head_sha": HEAD,
        "n": n,
        "k": 5,
        "complete_count": n,
        "observed": observed,
        "pass": True,
        "scenarios": [scenario],
        "authority_evidence_digest": HASH,
    }


def _evidence() -> dict:
    agents = [
        "weather-agent",
        "healthcare-agent",
        "finance-agent",
        "travel-agent",
        "support-ticket-agent",
    ]
    authorities = [
        _authority(f"{agent}/v0", agent, mode="baseline", observed=0)
        for agent in agents
    ]
    for number in range(1, 37):
        agent = (
            "weather-agent"
            if number <= 6
            else "healthcare-agent"
            if number <= 12
            else "finance-agent"
            if number <= 20
            else "travel-agent"
            if number <= 28
            else "support-ticket-agent"
        )
        mode = (
            "model_mediated"
            if number <= 12 or number in {21, 25, 26}
            else "deterministic"
        )
        authorities.append(
            _authority(
                f"issue-{number:03d}",
                agent,
                mode=mode,
                observed=5 if mode == "model_mediated" else 5,
            )
        )
    return stamp_evidence_digests(
        {
            "schema_version": "1.0.0",
            "kind": "test-agent-validation-evidence",
            "cycle_id": "validation-cycle-0001",
            "epoch": 1,
            "repository": "ninghu/agent-insights-quality",
            "pr_number": 999,
            "candidate_head_sha": HEAD,
            "candidate_tree_sha": "c" * 40,
            "policy_manifest_digest": HASH,
            "catalog_hashes": {
                "agents": HASH,
                "issues": HASH,
                "artifacts": HASH,
            },
            "artifact_manifest_hash": HASH,
            "source_tree_digest": HASH,
            "validation_contract_digest": HASH,
            "execution_matrix_digest": HASH,
            "runtime_topology_digest": HASH,
            "quota_plan_digest": HASH,
            "telemetry_resource_set": "g29",
            "test_agent_model": {
                "deployment_name": "gpt-5.4-mini",
                "model_id": "gpt-5.4-mini",
                "model_version": "2026-03-17",
            },
            "authorities": authorities,
            "evidence_digest": HASH,
        }
    )


def test_evidence_accepts_exact_41_authorities_and_model_mediated_five_of_seven() -> None:
    validate_evidence(_evidence())


def test_incomplete_attempt_cannot_claim_defect() -> None:
    value = _evidence()
    attempt = value["authorities"][5]["scenarios"][0]["issue_attempts"][0]
    attempt["complete"] = False
    attempt["defect_observed"] = True
    value = stamp_evidence_digests(value)
    with pytest.raises(ContractError, match="schema error"):
        validate_evidence(value)


def test_complete_and_defect_observed_remain_independent() -> None:
    value = _evidence()
    scenario = value["authorities"][5]["scenarios"][0]
    scenario["issue_attempts"][5]["defect_observed"] = False
    scenario["issue_attempts"][5]["expected_observation_pass"] = False
    scenario["issue_attempts"][6]["defect_observed"] = False
    scenario["issue_attempts"][6]["expected_observation_pass"] = False
    value = stamp_evidence_digests(value)
    validate_evidence(value)


def test_paired_v0_observation_fails_discrimination() -> None:
    value = _evidence()
    authority = value["authorities"][5]
    scenario = authority["scenarios"][0]
    scenario["v0_attempts"][0]["defect_observed"] = True
    scenario["v0_attempts"][0]["expected_observation_pass"] = False
    scenario["pass"] = False
    authority["pass"] = False
    value = stamp_evidence_digests(value)
    validate_evidence(value)

    tampered = deepcopy(value)
    tampered["authorities"][5]["scenarios"][0]["pass"] = True
    tampered["authorities"][5]["pass"] = True
    tampered = stamp_evidence_digests(tampered)
    with pytest.raises(ContractError, match="scenario pass result"):
        validate_evidence(tampered)


def test_evidence_rejects_cross_cycle_or_wrong_head_authority() -> None:
    value = _evidence()
    value["authorities"][0]["validated_head_sha"] = "d" * 40
    value = stamp_evidence_digests(value)
    with pytest.raises(ContractError, match="candidate head"):
        validate_evidence(value)


def test_evidence_is_create_once_in_private_snapshot_storage(
    monkeypatch,
    tmp_path,
) -> None:
    private = tmp_path / ".aiq-runtime" / "agent-insights-quality"
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", str(private))
    value = _evidence()

    class Store:
        @staticmethod
        def create_once(container, name, payload):
            return BlobRecord(
                container,
                name,
                deepcopy(payload),
                "evidence-etag",
                "evidence-version",
            )

    record = persist_evidence(Store(), value)
    assert record.container == "test-agent-validation-snapshots"
    assert record.name.endswith("test-agent-validation-evidence.json")
    assert (
        private / "test-agent-validation" / record.name
    ).is_file()
