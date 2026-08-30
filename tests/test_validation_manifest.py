from __future__ import annotations

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.validation_manifest import (
    authority_specs,
    prepare_candidate_manifest,
    stamp_candidate_manifest,
    validation_step_cost,
)
from agent_insights_quality.validation_policy import load_validation_policy
from agent_insights_quality.util import canonical_bytes


def test_candidate_manifest_binds_all_executable_and_source_inputs() -> None:
    agents, issues = load_catalogs()
    manifest = stamp_candidate_manifest(
        prepare_candidate_manifest(
            agents=agents,
            issues=issues,
            policy=load_validation_policy(),
            repository="ninghu/agent-insights-quality",
            pr_number=999,
            candidate_head_sha="a" * 40,
            candidate_tree_sha="b" * 40,
            workflow_run_id="synthetic-run-1",
        )
    )
    assert len(manifest["authorities"]) == 41
    assert len(manifest["source_content_digests"]) == 41
    assert len(manifest["execution_digests"]) == 41
    assert manifest["artifact_manifest_hash"] == manifest["catalog_hashes"]["artifacts"]
    assert manifest["project_name"].startswith("aiq-validation-")
    assert manifest["telemetry_resource_set"] == "g29"
    assert manifest["endpoint_envelope"]["attempts"] == 445
    assert manifest["endpoint_envelope"]["requests"] >= 890
    assert (
        manifest["endpoint_envelope"]["worst_case_inner_model_calls"] == 4
    )
    assert manifest["manifest_digest"].startswith("sha256:")


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
    assert cost.requests == 1
    assert cost.inner_model_calls == 4
    assert cost.tokens == (
        len(canonical_bytes(step["request"]["body"])) + 200
    ) * 4
