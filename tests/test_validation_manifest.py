from __future__ import annotations

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.util import canonical_bytes
from agent_insights_quality.validation_manifest import (
    authority_specs,
    prepare_validation_plan,
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
    assert plan["project_name"].startswith("aiq-validation-")
    assert plan["telemetry_resource_set"] == "g29"
    assert plan["endpoint_envelope"]["attempts"] == 449
    assert plan["endpoint_envelope"]["requests"] >= 890
    assert plan["endpoint_envelope"]["worst_case_inner_model_calls"] == 4
    assert plan["validation_digest"].startswith("sha256:")
    assert "tree_sha" not in plan
    assert "policy_manifest" not in plan


def test_authority_source_digests_include_runtime_identity_sources() -> None:
    agents, issues = load_catalogs()
    specs = authority_specs(agents, issues)
    assert len(specs) == 41
    assert all(item.source_content_digest.startswith("sha256:") for item in specs)
    assert len({item.execution_digest for item in specs}) == 41


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
