from __future__ import annotations

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.util import canonical_bytes
from agent_insights_quality.validation_manifest import (
    _validation_contract_file_hash,
    authority_specs,
    prepare_validation_plan,
    prepare_resumed_validation_plan,
    validate_validation_plan,
    validation_step_cost,
)
from agent_insights_quality.validation_policy import load_validation_policy


def test_local_plan_binds_one_commit_and_all_executable_inputs() -> None:
    agents, issues = load_catalogs()
    policy = load_validation_policy()
    plan = prepare_validation_plan(
        agents=agents,
        issues=issues,
        policy=policy,
        repository=policy.repository,
        pr_number=999,
        commit_sha="a" * 40,
        local_run_id="synthetic-run-1",
    )
    validate_validation_plan(
        plan,
        agents=agents,
        issues=issues,
        policy=policy,
    )
    assert len(plan["authorities"]) == 41
    assert plan["commit_sha"] == "a" * 40
    assert plan["environment_id"] == "swedencentral-g30"
    assert plan["location"] == "swedencentral"
    assert plan["project_name"] == "aiq-staging-swedencentral"
    assert plan["telemetry_resource_set"] == "g30"
    assert plan["endpoint_envelope"]["attempts"] == 449
    assert plan["endpoint_envelope"]["requests"] >= 890
    assert plan["endpoint_envelope"]["worst_case_inner_model_calls"] == 4
    assert plan["validation_digest"].startswith("sha256:")
    assert "tree_sha" not in plan
    assert "policy_manifest" not in plan


def test_resumed_plan_keeps_exact_cycle_and_topology() -> None:
    agents, issues = load_catalogs()
    policy = load_validation_policy()
    first = prepare_resumed_validation_plan(
        agents=agents,
        issues=issues,
        policy=policy,
        repository=policy.repository,
        pr_number=999,
        commit_sha="a" * 40,
        cycle_id="validation-0123456789ab",
    )
    second = prepare_resumed_validation_plan(
        agents=agents,
        issues=issues,
        policy=policy,
        repository=policy.repository,
        pr_number=999,
        commit_sha="a" * 40,
        cycle_id="validation-0123456789ab",
    )
    assert first == second
    assert first["cycle_id"] == "validation-0123456789ab"
    assert first["project_name"] == "aiq-staging-swedencentral"
    assert all(
        item["runtime_agent_name"].endswith(
            "baseline" if item["logical_version"] == "v0" else item["logical_version"]
        )
        for item in first["authorities"]
    )
    validate_validation_plan(
        first,
        agents=agents,
        issues=issues,
        policy=policy,
    )


def test_authority_source_digests_include_runtime_identity_sources() -> None:
    agents, issues = load_catalogs()
    specs = authority_specs(agents, issues)
    assert len(specs) == 41
    assert all(item.source_content_digest.startswith("sha256:") for item in specs)
    assert len({item.execution_digest for item in specs}) == 41


def test_validation_contract_hash_normalizes_text_line_endings(tmp_path) -> None:
    lf = tmp_path / "lf.py"
    crlf = tmp_path / "crlf.py"
    cr = tmp_path / "cr.py"
    lf.write_bytes(b"first\nsecond\n")
    crlf.write_bytes(b"first\r\nsecond\r\n")
    cr.write_bytes(b"first\rsecond\r")

    assert _validation_contract_file_hash(lf) == _validation_contract_file_hash(crlf)
    assert _validation_contract_file_hash(lf) == _validation_contract_file_hash(cr)


def test_endpoint_cost_accounts_for_input_and_output_tokens() -> None:
    step = {
        "request": {
            "body": {
                "input": [{"role": "user", "content": "synthetic request"}],
                "max_output_tokens": 200,
            }
        }
    }
    cost = validation_step_cost("microsoft_agent_framework", step)
    assert cost.requests == 4
    assert cost.inner_model_calls == 4
    assert cost.tokens == (
        len(canonical_bytes(step["request"]["body"])) + 200
    ) * 4
