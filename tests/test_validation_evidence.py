from __future__ import annotations

from copy import deepcopy

import pytest
from agent_insights_quality.catalogs import load_catalogs

from agent_insights_quality.util import ContractError, content_hash
from agent_insights_quality.validation_evidence import (
    authority_predicate_contract_digest,
    evaluate_defect_predicate,
    persist_evidence,
    scenario_predicate_contract_digest,
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
    semantic_pass: bool = True,
    trace_pass: bool = True,
) -> dict:
    return {
        "index": index,
        "step_id": rule_step["id"],
        "request_digest": content_hash(rule_step["request"]),
        "response_reference": content_hash({"response": namespace, "index": index}),
        "operation_reference": content_hash({"operation": namespace, "index": index}),
        "complete": True,
        "endpoint_pass": True,
        "semantic_pass": semantic_pass,
        "trace_pass": trace_pass,
        "identity_pass": True,
    }


def _attempt(
    index: int,
    *,
    namespace: str,
    rule_attempt: dict,
    defect_predicate: dict,
    should_observe: bool,
    expected: bool,
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
    selected_ids = set(defect_predicate.get("step_ids", []))
    required_surfaces = set(defect_predicate.get("required_surfaces", []))
    probe = []
    failed_selected = False
    for position, rule_step in enumerate(
        rule_attempt["probe_steps"],
        start=len(setup) + 1,
    ):
        semantic_pass = True
        trace_pass = True
        if (
            defect_predicate["kind"] != "never"
            and rule_step["id"] in selected_ids
            and not should_observe
            and not failed_selected
        ):
            semantic_pass = "semantic" not in required_surfaces
            trace_pass = "trace" not in required_surfaces
            failed_selected = True
        probe.append(
            _step(
                position,
                namespace=f"{namespace}:attempt:{index}:probe:{position}",
                rule_step=rule_step,
                semantic_pass=semantic_pass,
                trace_pass=trace_pass,
            )
        )
    observed = evaluate_defect_predicate(defect_predicate, probe)
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
        "defect_observed": observed,
        "expected_observation_pass": expected,
        "error_code": None,
    }


def _authority(spec, *, observed: int) -> dict:
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
            defect_predicate=rule["defect_predicate"],
            should_observe=not baseline and index <= observed,
            expected=(baseline or index <= observed),
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
                defect_predicate=rule["defect_predicate"],
                should_observe=False,
                expected=True,
            )
            for index, rule_attempt in enumerate(rule["attempts"], start=1)
        ]
    )
    scenario = {
        "scenario_id": rule["id"],
        "execution_digest": rule["execution_digest"],
        "validation_mode": mode,
        "healthy_predicate": rule["healthy_predicate"],
        "defect_predicate": rule["defect_predicate"],
        "v0_control_predicate": rule["v0_control_predicate"],
        "predicate_contract_digest": HASH,
        "n": n,
        "k": 5,
        "complete_count": n,
        "observed": observed,
        "pass": True,
        "issue_attempts": issue_attempts,
        "v0_attempts": v0_attempts,
    }
    scenario["predicate_contract_digest"] = scenario_predicate_contract_digest(
        scenario
    )
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
        "source_content_digest": spec.source_content_digest,
        "execution_digest": spec.execution_digest,
        "predicate_contract_digest": HASH,
        "validated_commit_sha": HEAD,
        "n": n,
        "k": 5,
        "complete_count": n,
        "observed": observed,
        "pass": True,
        "scenarios": [scenario],
        "authority_evidence_digest": HASH,
    }
    authority["predicate_contract_digest"] = (
        authority_predicate_contract_digest(authority)
    )
    return authority


def _evidence() -> dict:
    agents, issues = load_catalogs()
    specs = authority_specs(agents, issues)
    authorities = [
        _authority(
            spec,
            observed=0 if spec.authority_kind == "baseline" else 5,
        )
        for spec in specs
    ]
    return stamp_evidence_digests(
        {
            "schema_version": "1.0.0",
            "kind": "test-agent-validation-evidence",
            "commit_sha": HEAD,
            "validation_digest": current_validation_digest(agents, issues),
            "execution_matrix_digest": content_hash(
                {
                    item.authority_id: item.execution_digest
                    for item in specs
                }
            ),
            "telemetry_resource_set": "g29",
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


def test_identity_failure_is_visible_as_incomplete_evidence() -> None:
    value = _evidence()
    authority = value["authorities"][0]
    scenario = authority["scenarios"][0]
    attempt = scenario["issue_attempts"][0]
    attempt["setup_steps"][0]["identity_pass"] = False
    attempt["complete"] = False
    attempt["defect_observed"] = None
    attempt["error_code"] = "telemetry_identity_mismatch"
    scenario["complete_count"] = 4
    scenario["pass"] = False
    authority["complete_count"] = 4
    authority["pass"] = False
    value = stamp_evidence_digests(value)
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
    scenario["v0_attempts"][0]["probe_steps"][0]["semantic_pass"] = True
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


def test_evidence_recomputes_predicates_and_rejects_global_reference_reuse() -> None:
    value = _evidence()
    attempt = value["authorities"][5]["scenarios"][0]["issue_attempts"][0]
    attempt["defect_observed"] = False
    value = stamp_evidence_digests(value)
    with pytest.raises(ContractError, match="does not match its predicate"):
        validate_evidence(value)


def test_predicate_mutation_rejects_original_execution_binding() -> None:
    value = _evidence()
    authority = value["authorities"][5]
    scenario = authority["scenarios"][0]
    scenario["defect_predicate"]["required_surfaces"] = ["trace"]
    scenario["predicate_contract_digest"] = (
        scenario_predicate_contract_digest(scenario)
    )
    authority["predicate_contract_digest"] = (
        authority_predicate_contract_digest(authority)
    )
    value = stamp_evidence_digests(value)
    with pytest.raises(ContractError, match="canonical execution contract"):
        validate_evidence(value)

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
            }
        )
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
    monkeypatch,
    tmp_path,
) -> None:
    private = tmp_path / ".aiq-runtime" / "agent-insights-quality"
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", str(private))
    value = _evidence()

    record = persist_evidence(
        value,
        repository="ninghu/agent-insights-quality",
        pr_number=999,
        cycle_id="validation-cycle-0001",
    )
    assert record.digest == value["evidence_digest"]
    assert record.path.is_file()
    assert private / "test-agent-validation" in record.path.parents
