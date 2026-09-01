from __future__ import annotations

from copy import deepcopy

import pytest
from agent_insights_quality.catalogs import load_catalogs

from agent_insights_quality.util import ContractError, content_hash
from agent_insights_quality.validation_evidence import (
    persist_evidence,
    stamp_evidence_digests,
    validate_evidence,
)
from agent_insights_quality.validation_manifest import (
    authority_specs,
    current_validation_digest,
)

HASH = "sha256:" + ("a" * 64)
HEAD = "b" * 40


def _step(
    index: int,
    *,
    namespace: str,
    rule_step: dict,
) -> dict:
    return {
        "index": index,
        "step_id": rule_step["id"],
        "request_digest": content_hash(rule_step["request"]),
        "response_reference": content_hash({"response": namespace, "index": index}),
        "operation_reference": content_hash({"operation": namespace, "index": index}),
        "complete": True,
        "endpoint_pass": True,
        "identity_pass": True,
    }


def _attempt(
    index: int,
    *,
    namespace: str,
    rule_attempt: dict,
) -> dict:
    setup = [
        _step(
            position,
            namespace=f"{namespace}:attempt:{index}:setup:{position}",
            rule_step=rule_step,
        )
        for position, rule_step in enumerate(
            rule_attempt["setup_steps"],
            start=1,
        )
    ]
    probe = [
        _step(
            position,
            namespace=f"{namespace}:attempt:{index}:probe:{position}",
            rule_step=rule_step,
        )
        for position, rule_step in enumerate(
            rule_attempt["probe_steps"],
            start=len(setup) + 1,
        )
    ]
    steps = [*setup, *probe]
    return {
        "index": index,
        "conversation_reference": content_hash({"conversation": namespace, "index": index}),
        "session_reference": content_hash({"session": namespace, "index": index}),
        "response_references": [step["response_reference"] for step in steps],
        "operation_references": [step["operation_reference"] for step in steps],
        "setup_steps": setup,
        "probe_steps": probe,
        "complete": True,
        "error_code": None,
    }


def _authority(spec) -> dict:
    authority_id = spec.authority_id
    baseline = spec.authority_kind == "baseline"
    rule = spec.validation_rules["scenarios"][0]
    mode = rule["validation_mode"]
    n = rule["n"]
    issue_attempts = [
        _attempt(
            index,
            namespace=f"{authority_id}:authority",
            rule_attempt=rule_attempt,
        )
        for index, rule_attempt in enumerate(rule["attempts"], start=1)
    ]
    v0_attempts = (
        []
        if baseline
        else [
            _attempt(
                index,
                namespace=f"{authority_id}:paired-v0",
                rule_attempt=rule_attempt,
            )
            for index, rule_attempt in enumerate(rule["attempts"], start=1)
        ]
    )
    scenario = {
        "scenario_id": rule["id"],
        "execution_digest": rule["execution_digest"],
        "validation_mode": mode,
        "n": n,
        "complete_count": n,
        "pass": True,
        "issue_attempts": issue_attempts,
        "v0_attempts": v0_attempts,
    }
    authority = {
        "authority_id": authority_id,
        "authority_kind": "baseline" if baseline else "issue",
        "canonical_agent": spec.canonical_agent,
        "logical_version": "v0" if baseline else authority_id,
        "runtime_agent_name": (
            f"{spec.canonical_agent}-{authority_id.replace('/', '-')}-candidate"
        ),
        "runtime_agent_version": "1",
        "provider_agent_version_reference": HASH,
        "provider_content_digest": HASH,
        "source_content_digest": spec.source_content_digest,
        "execution_digest": spec.execution_digest,
        "validated_commit_sha": HEAD,
        "n": n,
        "complete_count": n,
        "pass": True,
        "scenarios": [scenario],
        "authority_evidence_digest": HASH,
    }
    return authority


def _evidence() -> dict:
    agents, issues = load_catalogs()
    specs = authority_specs(agents, issues)
    authorities = [_authority(spec) for spec in specs]
    return stamp_evidence_digests(
        {
            "schema_version": "1.0.0",
            "kind": "test-agent-validation-evidence",
            "repository": "ninghu/agent-insights-quality",
            "pr_number": 999,
            "cycle_id": "validation-cycle-0001",
            "commit_sha": HEAD,
            "validation_digest": current_validation_digest(agents, issues),
            "execution_matrix_digest": content_hash(
                {
                    item.authority_id: item.execution_digest
                    for item in specs
                }
            ),
            "runtime_topology_digest": HASH,
            "resource_inventory_digest": HASH,
            "environment_id": "swedencentral-g30",
            "location": "swedencentral",
            "telemetry_resource_set": "g30",
            "authorities": authorities,
            "evidence_digest": HASH,
        }
    )


def test_evidence_accepts_exact_41_mechanically_complete_authorities() -> None:
    validate_evidence(_evidence())


def test_evidence_schema_rejects_removed_issue_verdict_fields() -> None:
    value = _evidence()
    attempt = value["authorities"][5]["scenarios"][0]["issue_attempts"][0]
    attempt["defect_observed"] = True
    value = stamp_evidence_digests(value)
    with pytest.raises(ContractError, match="schema error"):
        validate_evidence(value)


def test_identity_failure_is_visible_as_incomplete_evidence() -> None:
    value = _evidence()
    authority = value["authorities"][0]
    scenario = authority["scenarios"][0]
    attempt = scenario["issue_attempts"][0]
    attempt["setup_steps"][0]["identity_pass"] = False
    attempt["complete"] = False
    attempt["error_code"] = "telemetry_identity_mismatch"
    scenario["complete_count"] = 4
    scenario["pass"] = False
    authority["complete_count"] = 4
    authority["pass"] = False
    value = stamp_evidence_digests(value)
    validate_evidence(value)


def test_issue_evidence_contains_no_code_generated_verdict() -> None:
    value = _evidence()
    serialized = str(value["authorities"][5])
    for field in (
        "defect_observed",
        "expected_observation_pass",
        "semantic_pass",
        "trace_pass",
        "observed",
        "defect_predicate",
    ):
        assert field not in serialized


def test_incomplete_paired_v0_fails_mechanical_completeness() -> None:
    value = _evidence()
    authority = value["authorities"][5]
    scenario = authority["scenarios"][0]
    scenario["v0_attempts"][0]["complete"] = False
    scenario["v0_attempts"][0]["probe_steps"][0]["identity_pass"] = False
    scenario["v0_attempts"][0]["error_code"] = "telemetry_identity_mismatch"
    scenario["pass"] = False
    authority["pass"] = False
    value = stamp_evidence_digests(value)
    validate_evidence(value)

    tampered = deepcopy(value)
    tampered["authorities"][5]["scenarios"][0]["pass"] = True
    tampered["authorities"][5]["pass"] = True
    tampered = stamp_evidence_digests(tampered)
    with pytest.raises(ContractError, match="mechanical evidence result"):
        validate_evidence(tampered)


def test_evidence_rejects_global_reference_reuse() -> None:
    value = _evidence()
    first = value["authorities"][0]["scenarios"][0]["issue_attempts"][0]
    second = value["authorities"][1]["scenarios"][0]["issue_attempts"][0]
    second["operation_references"][0] = first["operation_references"][0]
    second["setup_steps"][0]["operation_reference"] = first[
        "operation_references"
    ][0]
    value = stamp_evidence_digests(value)
    with pytest.raises(ContractError, match="global operation reference"):
        validate_evidence(value)


def test_evidence_binds_every_authority_to_exact_runtime_topology() -> None:
    value = _evidence()
    agents = []
    for authority in value["authorities"]:
        provider_agent_id = f"provider-{authority['authority_id']}"
        provider_version_id = f"version-{authority['authority_id']}"
        authority["provider_agent_version_reference"] = content_hash(
            {
                "provider_agent_id": provider_agent_id,
                "provider_agent_version_id": provider_version_id,
            }
        )
        agents.append(
            {
                "authority_id": authority["authority_id"],
                "runtime_agent_name": authority["runtime_agent_name"],
                "runtime_agent_version": authority["runtime_agent_version"],
                "provider_agent_id": provider_agent_id,
                "provider_agent_version_id": provider_version_id,
                "provider_content_digest": authority[
                    "provider_content_digest"
                ],
            }
        )
    value = stamp_evidence_digests(value)
    value["runtime_topology_digest"] = content_hash(agents)
    value = stamp_evidence_digests(value)
    validate_evidence(value, runtime_topology={"agents": agents})

    tampered = deepcopy(value)
    tampered["authorities"][0]["runtime_agent_version"] = "2"
    tampered = stamp_evidence_digests(tampered)
    with pytest.raises(ContractError, match="runtime identity is stale"):
        validate_evidence(tampered, runtime_topology={"agents": agents})


def test_evidence_rejects_wrong_commit_authority() -> None:
    value = _evidence()
    value["authorities"][0]["validated_commit_sha"] = "d" * 40
    value = stamp_evidence_digests(value)
    with pytest.raises(ContractError, match="bound to the commit"):
        validate_evidence(value)


def test_evidence_is_content_addressed_in_private_local_storage(
    tmp_path,
) -> None:
    private = tmp_path / "test-agent-validation"
    value = _evidence()

    record = persist_evidence(
        value,
        repository="ninghu/agent-insights-quality",
        pr_number=999,
        cycle_id="validation-cycle-0001",
        root=private / "evidence",
    )
    assert record.digest == value["evidence_digest"]
    assert record.path.is_file()
    assert private in record.path.parents


def test_v0_request_digest_cannot_be_changed_and_restamped() -> None:
    value = _evidence()
    step = value["authorities"][5]["scenarios"][0]["v0_attempts"][0][
        "probe_steps"
    ][0]
    step["request_digest"] = content_hash({"changed": True})
    value = stamp_evidence_digests(value)
    with pytest.raises(ContractError, match="canonical request contract"):
        validate_evidence(value)


def test_evidence_rejects_cross_cycle_path_or_resource_binding(tmp_path) -> None:
    value = _evidence()
    with pytest.raises(ContractError, match="path context"):
        persist_evidence(
            value,
            repository=value["repository"],
            pr_number=value["pr_number"],
            cycle_id="validation-other-cycle",
            root=tmp_path,
        )
    with pytest.raises(ContractError, match="resource inventory"):
        validate_evidence(value, resources=[{"different": True}])
