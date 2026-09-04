from __future__ import annotations

import shutil
from copy import deepcopy

import pytest

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.util import ROOT, canonical_bytes
from agent_insights_quality import validation_verifier
from agent_insights_quality.validation_manifest import (
    _validation_contract_file_hash,
    authority_specs,
    current_validation_digest,
    prepare_validation_plan,
    prepare_bound_validation_plan,
    validate_validation_plan,
    validation_step_cost,
)
from agent_insights_quality.validation_policy import load_validation_policy
from agent_insights_quality.validation_verifier import current_verifier_digest


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
    assert plan["commit_sha"] == "a" * 40
    assert plan["environment_id"] == "swedencentral-g30"
    assert plan["location"] == "swedencentral"
    assert plan["project_name"] == "aiq-staging-swedencentral"
    assert plan["telemetry_resource_set"] == "g30"
    assert plan["endpoint_envelope"]["attempts"] == 770
    assert plan["endpoint_envelope"]["requests"] >= 890
    assert plan["endpoint_envelope"]["worst_case_inner_model_calls"] == 4
    assert plan["validation_digest"].startswith("sha256:")
    assert plan["shared_validation_digest"].startswith("sha256:")
    assert plan["verifier_digest"] == current_verifier_digest()
    assert plan["invocation_contract_digest"].startswith("sha256:")
    assert plan["invocation_contract_digest"] != plan[
        "shared_validation_digest"
    ]
    assert plan["run_id"].startswith("validation-")
    assert "tree_sha" not in plan
    assert "policy_manifest" not in plan


def test_validation_digest_tracks_fixed_authorities_not_coordinator_sources() -> None:
    agents, issues = load_catalogs()
    assert current_validation_digest(agents, issues) == (
        "sha256:9472c6ac535cc9b394d942b354d4529ba6e120a81fcaeac5d6d652a98cdae3a5"
    )

    changed_issues = deepcopy(issues)
    changed_issues["issues"][0]["title"] += " changed"
    assert current_validation_digest(agents, changed_issues) != (
        current_validation_digest(agents, issues)
    )


def test_bound_plan_keeps_exact_run_and_topology() -> None:
    agents, issues = load_catalogs()
    policy = load_validation_policy()
    first = prepare_bound_validation_plan(
        agents=agents,
        issues=issues,
        policy=policy,
        repository=policy.repository,
        pr_number=999,
        commit_sha="a" * 40,
        run_id="validation-0123456789ab",
    )
    second = prepare_bound_validation_plan(
        agents=agents,
        issues=issues,
        policy=policy,
        repository=policy.repository,
        pr_number=999,
        commit_sha="a" * 40,
        run_id="validation-0123456789ab",
    )
    assert first == second
    assert first["run_id"] == "validation-0123456789ab"
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
    assert all(item.source_content_digest.startswith("sha256:") for item in specs)
    assert len({item.execution_digest for item in specs}) == len(specs)


def test_validation_contract_hash_normalizes_text_line_endings(tmp_path) -> None:
    lf = tmp_path / "lf.py"
    crlf = tmp_path / "crlf.py"
    cr = tmp_path / "cr.py"
    lf.write_bytes(b"first\nsecond\n")
    crlf.write_bytes(b"first\r\nsecond\r\n")
    cr.write_bytes(b"first\rsecond\r")

    assert _validation_contract_file_hash(lf) == _validation_contract_file_hash(crlf)
    assert _validation_contract_file_hash(lf) == _validation_contract_file_hash(cr)


def test_verifier_digest_excludes_orchestration_and_authority_content(
    tmp_path,
) -> None:
    included = {
        "config/test-agent-validation.yaml",
        *validation_verifier._VERIFIER_SCHEMA_PATHS,
        *validation_verifier._VERIFIER_IMPLEMENTATION_PATHS,
    }
    for relative in included:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)

    before = current_verifier_digest(tmp_path)
    for relative in (
        "src/agent_insights_quality/validation_coordinator.py",
        "catalogs/AGENT_CATALOG.yaml",
        "catalogs/ISSUE_CATALOG.yaml",
        "agents/weather-agent/v0/definition.json",
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("synthetic excluded change\n", encoding="utf-8")
    current_verifier_digest.cache_clear()
    assert current_verifier_digest(tmp_path) == before



@pytest.mark.parametrize(
    ("relative", "old", "new"),
    [
        (
            "src/agent_insights_quality/validation_rules.py",
            '"baseline": (10, 6)',
            '"baseline": (7, 5)',
        ),
        (
            "src/agent_insights_quality/validation_rules.py",
            "def scenario_execution_digest(",
            "def changed_scenario_execution_digest(",
        ),
        (
            "src/agent_insights_quality/validation_live.py",
            "class FoundryScenarioVerifier:",
            "class ChangedFoundryScenarioVerifier:",
        ),
    ],
)
def test_verifier_digest_changes_with_any_verifier_module_content(
    tmp_path,
    relative,
    old,
    new,
) -> None:
    included = {
        "config/test-agent-validation.yaml",
        *validation_verifier._VERIFIER_SCHEMA_PATHS,
        *validation_verifier._VERIFIER_IMPLEMENTATION_PATHS,
    }
    for included_path in included:
        destination = tmp_path / included_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / included_path, destination)
    before = current_verifier_digest(tmp_path)
    path = tmp_path / relative
    content = path.read_text(encoding="utf-8")
    assert old in content
    path.write_text(content.replace(old, new, 1), encoding="utf-8")
    current_verifier_digest.cache_clear()
    assert current_verifier_digest(tmp_path) != before


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
