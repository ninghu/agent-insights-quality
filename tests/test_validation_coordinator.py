from __future__ import annotations

from pathlib import Path
import inspect
from types import SimpleNamespace

import pytest

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.live import LiveRuntime
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_coordinator import (
    _assignments,
    _desired_state,
    _runner,
    _verifier,
)
from agent_insights_quality import validation_coordinator, validation_runtime
from agent_insights_quality.validation_manifest import (
    authority_specs,
    prepare_validation_plan,
)
from agent_insights_quality.validation_policy import load_validation_policy
from agent_insights_quality.validation_runtime import (
    DeployedRuntime,
    plan_runtime_topology,
)


def test_deployment_assignments_are_disjoint_and_bounded() -> None:
    authority_ids = [f"issue-{index:03d}" for index in range(1, 37)]
    assignments = _assignments(authority_ids, maximum_shards=8)
    assigned = [
        authority_id
        for assignment in assignments
        for authority_id in assignment["authority_ids"]
    ]
    assert len(assignments) == 8
    assert assigned != authority_ids
    assert len(assigned) == len(set(assigned)) == len(authority_ids)
    assert set(assigned) == set(authority_ids)


def test_validation_orchestration_contains_no_hidden_worker_pool() -> None:
    source = inspect.getsource(validation_coordinator) + inspect.getsource(
        validation_runtime
    )
    assert "ThreadPoolExecutor" not in source
    assert "_run_parallel" not in source
    assert "subprocess" not in source


def test_desired_state_assigns_only_content_without_exact_reuse(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_coordinator._read_deployment_registry",
        lambda: None,
    )
    agents, issues = load_catalogs()
    policy = load_validation_policy()
    authorities = authority_specs(agents, issues)
    plan = prepare_validation_plan(
        agents=agents,
        issues=issues,
        policy=policy,
        repository=policy.repository,
        pr_number=999,
        commit_sha="a" * 40,
        local_run_id="synthetic-run",
    )
    planned = list(
        plan_runtime_topology(
            authorities,
            run_suffix=plan["run_id"].removeprefix("validation-"),
            policy=policy,
        )
    )

    class Deployer:
        @staticmethod
        def desired_content_digest(authority):
            return f"sha256:{int(authority.authority_id[-3:]) if authority.authority_kind == 'issue' else 0:064x}"

        @staticmethod
        def find_existing(authority, target):
            index = authorities.index(authority)
            if index >= 29:
                return None
            digest = Deployer.desired_content_digest(authority)
            return DeployedRuntime(
                authority_id=authority.authority_id,
                runtime_kind=authority.runtime_kind,
                runtime_agent_name=target.runtime_agent_name,
                runtime_agent_version="1",
                provider_agent_id=f"agent-{index}",
                provider_agent_version_id=f"version-{index}",
                provider_content_digest=digest,
                hosted_identity_id=None,
                hosted_blueprint_id=None,
                hosted_deployment_id=None,
                runtime_principal_id=None,
                telemetry_identity_id=f"version-{index}",
                connection_ids=(),
            )

    desired = _desired_state(
        plan=plan,
        authorities=authorities,
        planned=planned,
        deployer=Deployer(),
        support_images={
            ("v0" if index == 0 else f"issue-{index + 28:03d}"): (
                "synthetic.azurecr.io/agent-insights-quality-support@"
                f"sha256:{index:064x}"
            )
            for index in range(9)
        },
        superseded_authority_ids=[],
        forced_invocation_authority_ids=[],
    )
    assert len(desired["reused_runtimes"]) == 29
    assert len(desired["deployment_authority_ids"]) == 12
    assert len(desired["deployment_assignments"]) <= 8
    assert {
        authority_id
        for assignment in desired["deployment_assignments"]
        for authority_id in assignment["authority_ids"]
    } == set(desired["deployment_authority_ids"])


def test_shard_runner_skips_global_traffic_ledger_but_live_runtime_uses_it(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[str] = []

    class Ledger:
        def __init__(self, profile_name):
            calls.append(f"open:{profile_name}")

        @staticmethod
        def mark_started(*_args, **_kwargs):
            calls.append("started")

    monkeypatch.setattr("agent_insights_quality.live.TrafficLedger", Ledger)
    profile = RuntimeProfile(
        name="staging",
        project_name="aiq-staging",
        project_endpoint="https://example.invalid",
        insights_endpoint="https://example.invalid",
        application_insights_resource_id="/synthetic/telemetry",
        registry_path=Path("synthetic-registry.json"),
    )
    context = {
        "profile": profile,
        "operator": SimpleNamespace(token_provider=lambda _scope: "token"),
        "authorities": [],
        "policy": SimpleNamespace(trace_hydration_stabilization_seconds=1),
    }
    traffic = tmp_path / "traffic.json"
    traffic.write_text(
        '[{"id":"synthetic","request":{"body":{"input":"synthetic"}}}]',
        encoding="utf-8",
    )

    shard_runtime = _runner(
        context,
        record_resource=lambda _resource: None,
    )._runtime
    shard_runtime._invoke_group = lambda *_args: (_ for _ in ()).throw(
        ContractError("synthetic stop")
    )
    with pytest.raises(ContractError, match="synthetic stop"):
        shard_runtime.invoke_version(
            agent_name="weather-agent-baseline",
            agent_type="prompt",
            foundry_version="1",
            traffic_path=traffic,
            seed=1,
        )
    assert calls == []

    runtime = LiveRuntime(profile)
    runtime._invoke_group = shard_runtime._invoke_group
    with pytest.raises(ContractError, match="synthetic stop"):
        runtime.invoke_version(
            agent_name="weather-agent-baseline",
            agent_type="prompt",
            foundry_version="1",
            traffic_path=traffic,
            seed=1,
        )
    assert calls == ["open:staging", "started"]


def test_verify_primitive_has_no_endpoint_or_session_create_capability() -> None:
    profile = RuntimeProfile(
        name="staging",
        project_name="aiq-staging",
        project_endpoint="https://example.invalid",
        insights_endpoint="https://example.invalid",
        application_insights_resource_id="/synthetic/telemetry",
        registry_path=Path("synthetic-registry.json"),
    )
    verifier = _verifier(
        {
            "profile": profile,
            "operator": SimpleNamespace(token_provider=lambda _scope: "token"),
            "authorities": [],
            "policy": SimpleNamespace(
                trace_hydration_stabilization_seconds=1
            ),
        }
    )
    assert not hasattr(verifier, "invoke")
    assert not hasattr(verifier, "prepare_hosted_routes")
