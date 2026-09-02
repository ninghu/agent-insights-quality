from __future__ import annotations

import shutil

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.util import ROOT, canonical_bytes
from agent_insights_quality.validation_manifest import (
    _validation_contract_file_hash,
    authority_specs,
    invocation_implementation_digest,
    prepare_validation_plan,
    prepare_bound_validation_plan,
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
    assert plan["shared_validation_digest"].startswith("sha256:")
    assert plan["invocation_contract_digest"].startswith("sha256:")
    assert plan["invocation_contract_digest"] != plan[
        "shared_validation_digest"
    ]
    assert plan["run_id"].startswith("validation-")
    assert "tree_sha" not in plan
    assert "policy_manifest" not in plan


def test_invocation_digest_tracks_post_code_but_not_verifier_code(
    tmp_path,
) -> None:
    target = tmp_path / "src" / "agent_insights_quality"
    target.mkdir(parents=True)
    for name in (
        "validation_runtime.py",
        "validation_live.py",
        "live.py",
    ):
        shutil.copyfile(
            ROOT / "src" / "agent_insights_quality" / name,
            target / name,
        )
    original = invocation_implementation_digest(tmp_path)
    verifier = target / "validation_live.py"
    verifier.write_text(
        verifier.read_text(encoding="utf-8").replace(
            "Validation response-anchor mapping is incomplete",
            "Changed verifier-only message",
        ),
        encoding="utf-8",
    )
    assert invocation_implementation_digest(tmp_path) == original
    invoker = target / "validation_runtime.py"
    invoker.write_text(
        invoker.read_text(encoding="utf-8").replace(
            "Validation shard authority assignment is empty",
            "Changed invocation behavior",
        ),
        encoding="utf-8",
    )
    assert invocation_implementation_digest(tmp_path) != original


def test_invocation_digest_tracks_transitive_hosted_traffic_globals(
    tmp_path,
) -> None:
    target = tmp_path / "src" / "agent_insights_quality"
    target.mkdir(parents=True)
    for name in (
        "validation_runtime.py",
        "validation_live.py",
        "live.py",
    ):
        shutil.copyfile(
            ROOT / "src" / "agent_insights_quality" / name,
            target / name,
        )
    original = invocation_implementation_digest(tmp_path)
    live = target / "live.py"
    source = live.read_text(encoding="utf-8")
    mutations = (
        (
            "_HOSTED_RESPONSE_PROPAGATION_RETRY_DELAYS = (1, 2, 4, 8)",
            "_HOSTED_RESPONSE_PROPAGATION_RETRY_DELAYS = (1, 3, 5, 8)",
        ),
        (
            "_HOSTED_RESPONSE_PROPAGATION_WINDOW_SECONDS = sum(",
            "_HOSTED_RESPONSE_PROPAGATION_WINDOW_SECONDS = 99 + sum(",
        ),
        (
            '_FOUNDRY_SCOPE = "https://ai.azure.com/.default"',
            '_FOUNDRY_SCOPE = "https://changed.invalid/.default"',
        ),
        (
            '_RESPONSE_REFERENCE = re.compile(r"^[A-Za-z0-9]',
            '_RESPONSE_REFERENCE = re.compile(r"^[A-Za-z]',
        ),
    )
    for old, new in mutations:
        assert old in source
        live.write_text(source.replace(old, new), encoding="utf-8")
        assert invocation_implementation_digest(tmp_path) != original


def test_invocation_digest_tracks_referenced_error_class_semantics(
    tmp_path,
) -> None:
    target = tmp_path / "src" / "agent_insights_quality"
    target.mkdir(parents=True)
    for name in (
        "validation_runtime.py",
        "validation_live.py",
        "live.py",
    ):
        shutil.copyfile(
            ROOT / "src" / "agent_insights_quality" / name,
            target / name,
        )
    original = invocation_implementation_digest(tmp_path)
    live = target / "live.py"
    source = live.read_text(encoding="utf-8")
    assert "self.request_accepted = request_accepted" in source
    live.write_text(
        source.replace(
            "self.request_accepted = request_accepted",
            "self.request_accepted = False",
        ),
        encoding="utf-8",
    )
    assert invocation_implementation_digest(tmp_path) != original


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
