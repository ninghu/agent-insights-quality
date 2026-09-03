from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

import pytest
import agent_insights_quality.validation_authority_results as authority_results
from agent_insights_quality.catalogs import load_catalogs

from agent_insights_quality.util import ContractError, atomic_json, content_hash
from agent_insights_quality.live import _trace_assertion_activation_error
from agent_insights_quality.validation_evidence import (
    digest_without_field,
    persist_evidence,
    runtime_mapping_digest,
    select_reusable_authority_evidence,
    stamp_evidence_digests,
    validate_evidence,
)
from agent_insights_quality.validation_authority_results import (
    load_bound_authority_verification_result,
    load_authority_verification_result,
    reusable_authority_verification_results,
    sanitize_verification_error,
    verification_query_diagnostics,
    write_authority_verification_result,
)
from agent_insights_quality.validation_live import PostResponseTelemetryError
from agent_insights_quality.validation_manifest import (
    authority_specs,
    current_validation_digest,
    current_shared_validation_digest,
)
from agent_insights_quality.validation_verifier import current_verifier_digest

HASH = "sha256:" + ("a" * 64)
HEAD = "b" * 40


def test_safe_trace_activation_diagnostics_survive_sanitization() -> None:
    error = PostResponseTelemetryError(
        _trace_assertion_activation_error(
            "Hosted evidence did not stabilize before the bounded deadline",
            code="trace_assertion_correlated_row_set_changed",
            matched_reference_count=4,
            expected_reference_count=5,
        ),
        stage="trace_output_stability",
    )

    assert sanitize_verification_error(error) == (
        "trace_output_stability",
        "trace_assertion_correlated_row_set_changed",
    )
    assert verification_query_diagnostics(error) == {
        "matched_reference_count": 4,
        "expected_reference_count": 5,
        "missing_reference_count": 1,
    }
    assert str(error) == "Post-response telemetry verification failed"


def test_invalid_trace_activation_diagnostics_remain_generic() -> None:
    error = _trace_assertion_activation_error(
        "Hosted evidence did not stabilize before the bounded deadline",
        code="unreviewed_diagnostic",
        matched_reference_count=1,
        expected_reference_count=1,
    )

    assert sanitize_verification_error(error) == (
        "authority_assertion",
        "trace_assertion_activation_error",
    )
    assert verification_query_diagnostics(error) is None
    assert error.__dict__ == {}


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
        "semantic_pass": True,
        "trace_pass": True,
    }


def _attempt(
    index: int,
    *,
    namespace: str,
    rule_attempt: dict,
    observation: bool = True,
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
    if not observation:
        for step in probe:
            step["semantic_pass"] = False
            step["trace_pass"] = False
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
        "observation": observation,
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
                observation=False,
            )
            for index, rule_attempt in enumerate(rule["attempts"], start=1)
        ]
    )
    scenario = {
        "scenario_id": rule["id"],
        "execution_digest": rule["execution_digest"],
        "validation_mode": mode,
        "n": n,
        "k": 5,
        "complete_count": n,
        "paired_complete_count": 0 if baseline else n,
        "observation_count": n,
        "paired_observation_count": 0,
        "evidence_complete": True,
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
        "runtime_mapping_digest": HASH,
        "provider_content_digest": HASH,
        "source_content_digest": spec.source_content_digest,
        "execution_digest": spec.execution_digest,
        "validated_commit_sha": HEAD,
        "n": n,
        "k": 5,
        "complete_count": n,
        "paired_complete_count": 0 if baseline else n,
        "observation_count": n,
        "paired_observation_count": 0,
        "evidence_complete": True,
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
            "schema_version": "3.0.0",
            "kind": "test-agent-validation-evidence",
            "repository": "ninghu/agent-insights-quality",
            "pr_number": 999,
            "run_id": "validation-000000000001",
            "commit_sha": HEAD,
            "completed_at": "2026-09-01T12:00:00+00:00",
            "result": "PASS",
            "validation_digest": current_validation_digest(agents, issues),
            "shared_validation_digest": current_shared_validation_digest(),
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
    scenario["evidence_complete"] = False
    scenario["pass"] = False
    authority["complete_count"] = 4
    authority["evidence_complete"] = False
    authority["pass"] = False
    value["result"] = "FAIL"
    value = stamp_evidence_digests(value)
    with pytest.raises(ContractError, match="incomplete authority"):
        validate_evidence(value)


def test_issue_evidence_contains_bounded_assertion_observations() -> None:
    value = _evidence()
    serialized = str(value["authorities"][5])
    for field in (
        "defect_observed",
        "expected_observation_pass",
        "observed",
        "defect_predicate",
    ):
        assert field not in serialized
    assert "observation" in serialized
    assert "semantic_pass" in serialized
    assert "trace_pass" in serialized


def test_incomplete_paired_v0_fails_mechanical_completeness() -> None:
    value = _evidence()
    authority = value["authorities"][5]
    scenario = authority["scenarios"][0]
    scenario["v0_attempts"][0]["complete"] = False
    scenario["v0_attempts"][0]["probe_steps"][0]["identity_pass"] = False
    scenario["v0_attempts"][0]["error_code"] = "telemetry_identity_mismatch"
    scenario["paired_complete_count"] -= 1
    scenario["evidence_complete"] = False
    scenario["pass"] = False
    authority["paired_complete_count"] -= 1
    authority["evidence_complete"] = False
    authority["pass"] = False
    value["result"] = "FAIL"
    value = stamp_evidence_digests(value)
    with pytest.raises(ContractError, match="incomplete authority"):
        validate_evidence(value)


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
        authority["runtime_mapping_digest"] = runtime_mapping_digest(agents[-1])
    value = stamp_evidence_digests(value)
    value["runtime_topology_digest"] = content_hash(agents)
    value = stamp_evidence_digests(value)
    validate_evidence(value, runtime_topology={"agents": agents})

    tampered = deepcopy(value)
    tampered["authorities"][0]["runtime_agent_version"] = "2"
    tampered = stamp_evidence_digests(tampered)
    with pytest.raises(ContractError, match="runtime identity is stale"):
        validate_evidence(tampered, runtime_topology={"agents": agents})


def test_reused_authority_retains_its_original_validated_commit() -> None:
    value = _evidence()
    value["authorities"][0]["validated_commit_sha"] = "d" * 40
    value = stamp_evidence_digests(value)
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
        run_id="validation-000000000001",
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
            run_id="validation-000000000002",
            root=tmp_path,
        )
    with pytest.raises(ContractError, match="resource inventory"):
        validate_evidence(value, resources=[{"different": True}])


def test_exact_pass_evidence_is_reused_and_mapping_drift_is_selected(
    tmp_path,
) -> None:
    value = _evidence()
    runtime_agents = []
    for index, authority in enumerate(value["authorities"], start=1):
        runtime = {
            "authority_id": authority["authority_id"],
            "runtime_agent_name": authority["runtime_agent_name"],
            "runtime_agent_version": authority["runtime_agent_version"],
            "provider_agent_id": f"agent-{index}",
            "provider_agent_version_id": f"version-{index}",
            "provider_content_digest": authority["provider_content_digest"],
            "hosted_identity_id": None,
            "hosted_blueprint_id": None,
            "hosted_deployment_id": None,
            "runtime_principal_id": None,
            "telemetry_identity_id": f"version-{index}",
            "connection_ids": [],
        }
        authority["provider_agent_version_reference"] = content_hash(
            {
                "provider_agent_id": runtime["provider_agent_id"],
                "provider_agent_version_id": runtime[
                    "provider_agent_version_id"
                ],
            }
        )
        authority["runtime_mapping_digest"] = runtime_mapping_digest(runtime)
        runtime_agents.append(runtime)
    value["runtime_topology_digest"] = content_hash(runtime_agents)
    value = stamp_evidence_digests(value)
    persist_evidence(
        value,
        repository=value["repository"],
        pr_number=value["pr_number"],
        run_id=value["run_id"],
        root=tmp_path / "evidence",
    )
    agents, issues = load_catalogs()
    specs = authority_specs(agents, issues)
    selected, reused = select_reusable_authority_evidence(
        authorities=specs,
        runtime_topology={"agents": runtime_agents},
        repository=value["repository"],
        pr_number=value["pr_number"],
        environment_id=value["environment_id"],
        location=value["location"],
        telemetry_resource_set=value["telemetry_resource_set"],
        shared_validation_digest=value["shared_validation_digest"],
        root=tmp_path,
    )
    assert selected == []
    assert len(reused) == 41

    runtime_agents[0]["runtime_agent_version"] = "changed"
    selected, reused = select_reusable_authority_evidence(
        authorities=specs,
        runtime_topology={"agents": runtime_agents},
        repository=value["repository"],
        pr_number=value["pr_number"],
        environment_id=value["environment_id"],
        location=value["location"],
        telemetry_resource_set=value["telemetry_resource_set"],
        shared_validation_digest=value["shared_validation_digest"],
        root=tmp_path,
    )
    assert selected == [specs[0].authority_id]
    assert len(reused) == 40


def test_late_query_failure_preserves_prior_authority_result(tmp_path) -> None:
    evidence = _evidence()
    agents, issues = load_catalogs()
    specs = authority_specs(agents, issues)
    prepared = {
        "repository": evidence["repository"],
        "pr_number": evidence["pr_number"],
        "run_id": evidence["run_id"],
        "commit_sha": HEAD,
        "digests": {
            "validation_digest": evidence["validation_digest"],
            "shared_validation_digest": evidence["shared_validation_digest"],
            "verifier_digest": current_verifier_digest(),
            "execution_matrix_digest": evidence["execution_matrix_digest"],
            "runtime_topology_digest": HASH,
            "quota_plan_digest": HASH,
        },
        "project": {
            "name": "aiq-staging-swedencentral",
            "provider_id": "synthetic-project",
        },
        "runtime_topology": {
            "account_reference": HASH,
            "telemetry_resource_set": "g30",
        },
    }
    plan = {
        "environment_id": "swedencentral-g30",
        "location": "swedencentral",
    }
    invocation = {
        "authority_id": "",
        "path": "invocations/synthetic.json",
        "receipt_digest": HASH,
        "invocation_digest": HASH,
    }

    references = []
    for index, outcome in enumerate(("PASS", "INCOMPLETE")):
        spec = specs[index]
        authority = evidence["authorities"][index]
        runtime = {
            "runtime_agent_name": authority["runtime_agent_name"],
            "runtime_agent_version": authority["runtime_agent_version"],
            "provider_agent_id": f"agent-{index}",
            "provider_agent_version_id": f"version-{index}",
            "provider_content_digest": authority["provider_content_digest"],
            "hosted_identity_id": None,
            "hosted_blueprint_id": None,
            "hosted_deployment_id": None,
            "runtime_principal_id": None,
            "telemetry_identity_id": f"version-{index}",
            "connection_ids": [],
        }
        reference = write_authority_verification_result(
            prepared=prepared,
            plan=plan,
            authority=spec,
            runtime=runtime,
            invocation_reference={
                **invocation,
                "authority_id": spec.authority_id,
            },
            authority_evidence=authority if outcome == "PASS" else None,
            outcome=outcome,
            started_at=datetime(2026, 9, 1, 12, index, tzinfo=UTC),
            completed_at=datetime(2026, 9, 1, 12, index, 1, tzinfo=UTC),
            query_stage=None if outcome == "PASS" else "trace_output_stability",
            error_code=(
                None
                if outcome == "PASS"
                else "trace_assertion_correlated_row_set_changed"
            ),
            query_diagnostics=(
                None
                if outcome == "PASS"
                else {
                    "matched_reference_count": 4,
                    "expected_reference_count": 5,
                    "missing_reference_count": 1,
                }
            ),
            fence=lambda: None,
            root=tmp_path,
        )
        references.append(reference)

    first = load_authority_verification_result(references[0], root=tmp_path)
    second = load_authority_verification_result(references[1], root=tmp_path)
    assert first["outcome"] == "PASS"
    assert first["authority_evidence"]["pass"] is True
    assert second["outcome"] == "INCOMPLETE"
    assert second["authority_evidence"] is None
    assert second["query_stage"] == "trace_output_stability"
    assert second["error_code"] == "trace_assertion_correlated_row_set_changed"
    assert second["query_diagnostics"] == {
        "matched_reference_count": 4,
        "expected_reference_count": 5,
        "missing_reference_count": 1,
    }


def test_cross_generation_pass_reuse_ignores_global_verifier_state(
    monkeypatch,
    tmp_path,
) -> None:
    evidence = _evidence()
    specs = authority_specs(*load_catalogs())
    spec = specs[0]
    authority = evidence["authorities"][0]
    runtime = _result_runtime(spec, authority, index=1)
    prepared = _result_prepared(evidence, verifier_digit="1")
    invocation = _result_invocation(spec.authority_id, digit="2")
    reference = write_authority_verification_result(
        prepared=prepared,
        plan=_result_plan(),
        authority=spec,
        runtime=runtime,
        invocation_reference=invocation,
        authority_evidence=authority,
        outcome="PASS",
        started_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        completed_at=datetime(2026, 9, 1, 12, 1, tzinfo=UTC),
        query_stage=None,
        error_code=None,
        query_diagnostics=None,
        fence=lambda: None,
        root=tmp_path,
    )
    monkeypatch.setattr(
        authority_results,
        "load_bound_invocation_receipt",
        lambda *_args, **_kwargs: invocation,
    )
    changed = deepcopy(prepared)
    changed["run_id"] = "validation-000000000002"
    changed["commit_sha"] = "c" * 40
    for field, digit in (
        ("validation_digest", "3"),
        ("shared_validation_digest", "4"),
        ("verifier_digest", "5"),
        ("execution_matrix_digest", "6"),
        ("runtime_topology_digest", "7"),
        ("quota_plan_digest", "8"),
    ):
        changed["digests"][field] = "sha256:" + (digit * 64)

    reused = load_bound_authority_verification_result(
        reference,
        authority=spec,
        paired_v0_authority=spec,
        runtime=runtime,
        paired_v0_runtime=runtime,
        prepared=changed,
        plan=_result_plan(),
        require_current_generation=False,
        root=tmp_path,
    )
    assert reused["outcome"] == "PASS"
    assert reused["binding"]["verifier_digest"] == "sha256:" + ("1" * 64)
    assert reused["binding"]["verifier_commit_sha"] == HEAD

    with pytest.raises(ContractError, match="binding is stale"):
        load_bound_authority_verification_result(
            reference,
            authority=spec,
            paired_v0_authority=spec,
            runtime=runtime,
            paired_v0_runtime=runtime,
            prepared=changed,
            plan=_result_plan(),
            require_current_generation=True,
            root=tmp_path,
        )


def test_definitive_fail_result_is_reusable_without_revalidation(
    monkeypatch,
    tmp_path,
) -> None:
    evidence = _evidence()
    spec = authority_specs(*load_catalogs())[0]
    failed = deepcopy(evidence["authorities"][0])
    attempt = failed["scenarios"][0]["issue_attempts"][0]
    attempt["probe_steps"][0]["semantic_pass"] = False
    attempt["observation"] = False
    failed["scenarios"][0]["observation_count"] = 4
    failed["scenarios"][0]["pass"] = False
    failed["observation_count"] = 4
    failed["pass"] = False
    failed["authority_evidence_digest"] = digest_without_field(
        failed,
        "authority_evidence_digest",
    )
    prepared = _result_prepared(evidence, verifier_digit="1")
    runtime = _result_runtime(spec, failed, index=1)
    invocation = _result_invocation(spec.authority_id, digit="2")
    reference = write_authority_verification_result(
        prepared=prepared,
        plan=_result_plan(),
        authority=spec,
        runtime=runtime,
        invocation_reference=invocation,
        authority_evidence=failed,
        outcome="FAIL",
        started_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        completed_at=datetime(2026, 9, 1, 12, 1, tzinfo=UTC),
        query_stage=None,
        error_code=None,
        query_diagnostics=None,
        fence=lambda: None,
        root=tmp_path,
    )
    monkeypatch.setattr(
        authority_results,
        "load_bound_invocation_receipt",
        lambda *_args, **_kwargs: invocation,
    )

    reusable = reusable_authority_verification_results(
        authorities=[spec],
        runtime_topology={"agents": [runtime]},
        prepared={
            **prepared,
            "run_id": "validation-000000000002",
            "commit_sha": "c" * 40,
        },
        plan=_result_plan(),
        root=tmp_path,
    )

    assert reusable[spec.authority_id] == reference

    changed_verifier = deepcopy(prepared)
    changed_verifier["run_id"] = "validation-000000000003"
    changed_verifier["commit_sha"] = "d" * 40
    changed_verifier["digests"]["verifier_digest"] = "sha256:" + ("9" * 64)
    reusable = reusable_authority_verification_results(
        authorities=[spec],
        runtime_topology={"agents": [runtime]},
        prepared=changed_verifier,
        plan=_result_plan(),
        root=tmp_path,
    )

    assert reusable[spec.authority_id] is None


def test_authority_result_binds_immutable_copilot_evaluation(
    tmp_path,
) -> None:
    evidence = _evidence()
    spec = authority_specs(*load_catalogs())[0]
    authority = evidence["authorities"][0]
    runtime = _result_runtime(spec, authority, index=1)
    prepared = _result_prepared(evidence, verifier_digit="1")
    invocation = _result_invocation(spec.authority_id, digit="2")
    package = {
        "prompt_digest": HASH,
        "package_hash": "",
    }
    package["package_hash"] = digest_without_field(package, "package_hash")
    evaluation = {
        "model": "gpt-5.6-sol",
        "package_hash": package["package_hash"],
    }
    evaluation_digest = content_hash(evaluation)
    artifact_root = tmp_path / "copilot-authority-evaluations"
    atomic_json(
        artifact_root
        / "packages"
        / f"{package['package_hash'].removeprefix('sha256:')}.json",
        package,
    )
    import_path = (
        artifact_root
        / "imports"
        / f"{evaluation_digest.removeprefix('sha256:')}.json"
    )
    atomic_json(import_path, evaluation)
    reference = write_authority_verification_result(
        prepared=prepared,
        plan=_result_plan(),
        authority=spec,
        runtime=runtime,
        invocation_reference=invocation,
        authority_evidence=authority,
        outcome="PASS",
        started_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        completed_at=datetime(2026, 9, 1, 12, 1, tzinfo=UTC),
        query_stage=None,
        error_code=None,
        query_diagnostics=None,
        fence=lambda: None,
        copilot_evaluation={
            "model": "gpt-5.6-sol",
            "package_hash": package["package_hash"],
            "prompt_digest": HASH,
            "evaluation_digest": evaluation_digest,
        },
        root=tmp_path,
    )

    result = load_authority_verification_result(reference, root=tmp_path)
    assert result["copilot_evaluation"]["evaluation_digest"] == evaluation_digest

    evaluation["unexpected"] = "changed"
    atomic_json(import_path, evaluation)
    with pytest.raises(ContractError, match="evaluation reference changed"):
        load_authority_verification_result(reference, root=tmp_path)


@pytest.mark.parametrize("changed_binding", ["authority", "runtime", "receipt"])
def test_authority_local_change_reselects_only_that_authority(
    monkeypatch,
    tmp_path,
    changed_binding,
) -> None:
    evidence = _evidence()
    specs = authority_specs(*load_catalogs())
    selected_specs = specs[:2]
    prepared = _result_prepared(evidence, verifier_digit="1")
    runtimes = {
        spec.authority_id: _result_runtime(
            spec,
            evidence["authorities"][index],
            index=index + 1,
        )
        for index, spec in enumerate(specs)
    }
    invocations = {
        spec.authority_id: _result_invocation(
            spec.authority_id,
            digit=str(index + 1),
        )
        for index, spec in enumerate(selected_specs)
    }
    for index, spec in enumerate(selected_specs):
        write_authority_verification_result(
            prepared=prepared,
            plan=_result_plan(),
            authority=spec,
            runtime=runtimes[spec.authority_id],
            invocation_reference=invocations[spec.authority_id],
            authority_evidence=evidence["authorities"][index],
            outcome="PASS",
            started_at=datetime(2026, 9, 1, 12, index, tzinfo=UTC),
            completed_at=datetime(2026, 9, 1, 12, index, 1, tzinfo=UTC),
            query_stage=None,
            error_code=None,
            query_diagnostics=None,
            fence=lambda: None,
            root=tmp_path,
        )

    changed_id = selected_specs[1].authority_id

    def load_receipt(reference, **_kwargs):
        if changed_binding == "receipt" and reference["authority_id"] == changed_id:
            raise ContractError("synthetic changed receipt")
        return invocations[reference["authority_id"]]

    monkeypatch.setattr(
        authority_results,
        "load_bound_invocation_receipt",
        load_receipt,
    )
    current_specs = list(specs)
    if changed_binding == "authority":
        current_specs[1] = replace(
            current_specs[1],
            source_content_digest="sha256:" + ("9" * 64),
        )
    runtime_topology = {
        "agents": [deepcopy(runtimes[spec.authority_id]) for spec in specs]
    }
    if changed_binding == "runtime":
        runtime_topology["agents"][1]["runtime_agent_version"] = "changed"

    reusable = reusable_authority_verification_results(
        authorities=current_specs,
        runtime_topology=runtime_topology,
        prepared=prepared,
        plan=_result_plan(),
        root=tmp_path,
    )

    assert reusable[selected_specs[0].authority_id] is not None
    assert changed_id not in reusable


def _result_prepared(evidence: dict, *, verifier_digit: str) -> dict:
    return {
        "repository": evidence["repository"],
        "pr_number": evidence["pr_number"],
        "run_id": evidence["run_id"],
        "commit_sha": HEAD,
        "digests": {
            "validation_digest": evidence["validation_digest"],
            "shared_validation_digest": evidence["shared_validation_digest"],
            "verifier_digest": "sha256:" + (verifier_digit * 64),
            "execution_matrix_digest": evidence["execution_matrix_digest"],
            "runtime_topology_digest": HASH,
            "quota_plan_digest": HASH,
        },
        "project": {
            "name": "aiq-staging-swedencentral",
            "provider_id": "synthetic-project",
        },
        "runtime_topology": {
            "account_reference": HASH,
            "telemetry_resource_set": "g30",
        },
    }


def _result_plan() -> dict:
    return {
        "environment_id": "swedencentral-g30",
        "location": "swedencentral",
    }


def _result_runtime(spec, authority: dict, *, index: int) -> dict:
    return {
        "authority_id": spec.authority_id,
        "runtime_kind": spec.runtime_kind,
        "runtime_agent_name": authority["runtime_agent_name"],
        "runtime_agent_version": authority["runtime_agent_version"],
        "provider_agent_id": f"agent-{index}",
        "provider_agent_version_id": f"version-{index}",
        "provider_content_digest": authority["provider_content_digest"],
        "hosted_identity_id": None,
        "hosted_blueprint_id": None,
        "hosted_deployment_id": None,
        "runtime_principal_id": None,
        "telemetry_identity_id": f"version-{index}",
        "connection_ids": [],
    }


def _result_invocation(authority_id: str, *, digit: str) -> dict:
    return {
        "authority_id": authority_id,
        "path": f"invocations/{authority_id.replace('/', '--')}.json",
        "receipt_digest": "sha256:" + (digit * 64),
        "invocation_digest": "sha256:" + (digit * 64),
    }
