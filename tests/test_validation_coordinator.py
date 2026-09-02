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
    _forced_invocation_authority_ids,
    _merge_authority_result_selection,
    _runner,
    _support_image_reuse_candidates,
    _verifier,
)
from agent_insights_quality import validation_coordinator, validation_runtime
from agent_insights_quality.validation_assignments import verification_assignment
from agent_insights_quality.validation_manifest import (
    authority_specs,
    prepare_validation_plan,
)
from agent_insights_quality.validation_policy import load_validation_policy
from agent_insights_quality.validation_runtime import (
    DeployedRuntime,
    plan_runtime_topology,
)
from agent_insights_quality.validation_shards import ValidationShardStore


def test_deployment_assignments_are_disjoint_and_bounded() -> None:
    authority_ids = [f"issue-{index:03d}" for index in range(1, 37)]
    assignments = _assignments(
        authority_ids,
        quota_plan_digest="sha256:" + ("a" * 64),
        maximum_shards=8,
    )
    assigned = [
        authority_id
        for assignment in assignments
        for authority_id in assignment["authority_ids"]
    ]
    assert len(assignments) == 8
    assert assigned != authority_ids
    assert len(assigned) == len(set(assigned)) == len(authority_ids)
    assert set(assigned) == set(authority_ids)
    assert {
        item["quota_plan_digest"] for item in assignments
    } == {"sha256:" + ("a" * 64)}


def test_all_authorities_share_one_verifier_digest() -> None:
    active = _active_validation()
    assert {
        item["verifier_digest"]
        for item in active["verification_authority_assignments"]
    } == {active["digests"]["verifier_digest"]}


def test_validation_orchestration_contains_no_hidden_worker_pool() -> None:
    source = inspect.getsource(validation_coordinator) + inspect.getsource(
        validation_runtime
    )
    assert "ThreadPoolExecutor" not in source
    assert "_run_parallel" not in source
    assert "subprocess" not in source


def test_complete_migrated_receipts_force_zero_invoke_shards() -> None:
    authority_ids = [
        "weather-agent/v0",
        *[f"issue-{index:03d}" for index in range(1, 37)],
        "healthcare-agent/v0",
        "finance-agent/v0",
        "travel-agent/v0",
        "support-ticket-agent/v0",
    ]
    forced = _forced_invocation_authority_ids(
        migration={
            "incomplete_authority_ids": [],
        },
        supplemental={
            "imported_authority_ids": authority_ids,
            "incomplete_authority_ids": [],
        },
        incomplete_current_invocations=authority_ids,
    )
    assert forced == []
    assert _assignments(
        forced,
        quota_plan_digest="sha256:" + ("a" * 64),
    ) == []
    verify = _assignments(
        authority_ids,
        quota_plan_digest="sha256:" + ("a" * 64),
    )
    assert 1 <= len(verify) <= 8


def _active_validation() -> dict:
    authorities = authority_specs(*load_catalogs())
    authority_ids = [item.authority_id for item in authorities]
    value = {
        "state": "VALIDATING",
        "repository": "synthetic/example",
        "pr_number": 63,
        "commit_sha": "a" * 40,
        "run_id": "validation-0123456789ab",
        "digests": {
            "validation_digest": "sha256:" + ("a" * 64),
            "quota_plan_digest": "sha256:" + ("b" * 64),
            "shared_validation_digest": "sha256:" + ("c" * 64),
            "verifier_digest": "sha256:" + ("f" * 64),
            "execution_matrix_digest": "sha256:" + ("d" * 64),
            "runtime_topology_digest": "sha256:" + ("e" * 64),
        },
        "project": {
            "name": "aiq-staging-swedencentral",
            "provider_id": "synthetic-project",
        },
        "runtime_topology": {
            "telemetry_resource_set": "g30",
            "agents": [
                {
                    "authority_id": authority.authority_id,
                    "canonical_agent": authority.canonical_agent,
                    "runtime_agent_name": f"synthetic-agent-{index}",
                    "runtime_agent_version": "1",
                    "provider_agent_id": f"agent-{index}",
                    "provider_agent_version_id": f"version-{index}",
                    "provider_content_digest": f"sha256:{index:064x}",
                }
                for index, authority in enumerate(authorities, start=1)
            ],
        },
        "validation_authority_ids": authority_ids,
        "reused_authorities": [],
        "deployment_assignments": [],
        "invocation_shard_assignments": [
            {
                "shard_id": 1,
                "authority_ids": ["issue-014"],
                "quota_plan_digest": "sha256:" + ("b" * 64),
                "assignment_digest": "sha256:" + ("d" * 64),
            }
        ],
    }
    value["verification_authority_assignments"] = [
        verification_assignment(value, authority_id)
        for authority_id in authority_ids
    ]
    return value


def test_pending_invocation_exposes_no_verification_slots(
    monkeypatch,
    tmp_path,
) -> None:
    active = _active_validation()
    monkeypatch.setattr(
        "agent_insights_quality.validation_shards.validation_runtime_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        validation_coordinator,
        "discover_local_git_context",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_matching_active",
        lambda _git: active,
    )

    reconciled = validation_coordinator._prepared_result(active)
    status = validation_coordinator.run_test_agent_validation()

    assert reconciled["verification_assignments"] == []
    assert reconciled["verification_authority_concurrency"] == 0
    assert status["status"] == "invocation_pending"
    assert "verification_assignments" not in status
    assert not any("verify-" in command for command in status["next_commands"])


def test_completed_invocation_exposes_eight_verification_slots(
    monkeypatch,
    tmp_path,
) -> None:
    active = _active_validation()
    monkeypatch.setattr(
        "agent_insights_quality.validation_shards.validation_runtime_root",
        lambda: tmp_path,
    )
    assignment = active["invocation_shard_assignments"][0]
    store = ValidationShardStore(
        prepared=active,
        shard_id=assignment["shard_id"],
        authority_ids=assignment["authority_ids"],
        fence=lambda: None,
    )
    store.begin_invocation()
    store.record_invocation_receipt(
        {
            "authority_id": "issue-014",
            "path": "synthetic/receipt.json",
            "receipt_digest": "sha256:" + ("1" * 64),
            "invocation_digest": "sha256:" + ("2" * 64),
        }
    )
    store.complete_invocation()
    monkeypatch.setattr(
        validation_coordinator,
        "discover_local_git_context",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_matching_active",
        lambda _git: active,
    )
    monkeypatch.setattr(
        validation_coordinator,
        "current_authority_verification_results",
        lambda **_kwargs: {},
    )

    reconciled = validation_coordinator._prepared_result(active)
    status = validation_coordinator.run_test_agent_validation()

    assert len(reconciled["verification_assignments"]) == 8
    assert reconciled["verification_authority_concurrency"] == 8
    assert status["status"] == "verification_pending"
    assert len(status["verification_assignments"]) == 8
    assert len(status["next_commands"]) == 8

    completed_ids = active["validation_authority_ids"][:8]
    monkeypatch.setattr(
        validation_coordinator,
        "current_authority_verification_results",
        lambda **_kwargs: {
            authority_id: {"authority_id": authority_id}
            for authority_id in completed_ids
        },
    )
    monkeypatch.setattr(
        validation_coordinator,
        "load_authority_verification_result",
        lambda reference: {
            "authority_id": reference["authority_id"],
            "outcome": "PASS",
        },
    )

    replenished = validation_coordinator.run_test_agent_validation()

    assert [
        item["authority_id"]
        for item in replenished["verification_assignments"]
    ] == active["validation_authority_ids"][8:16]
    assert len(replenished["next_commands"]) == 8


def _run_validation_with_completed_outcomes(
    monkeypatch,
    outcomes: dict[str, str],
) -> tuple[dict, dict]:
    active = _active_validation()
    monkeypatch.setattr(
        validation_coordinator,
        "discover_local_git_context",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_matching_active",
        lambda _git: active,
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_incomplete_invocation_shards",
        lambda _active: [],
    )
    monkeypatch.setattr(
        validation_coordinator,
        "current_authority_verification_results",
        lambda **_kwargs: {
            authority_id: {"authority_id": authority_id}
            for authority_id in outcomes
        },
    )

    def load_result(reference):
        authority_id = reference["authority_id"]
        outcome = outcomes[authority_id]
        return {
            "authority_id": authority_id,
            "outcome": outcome,
            "query_stage": (
                "trace_output_stability" if outcome == "INCOMPLETE" else None
            ),
            "error_code": (
                "telemetry_not_stable" if outcome == "INCOMPLETE" else None
            ),
            "query_diagnostics": (
                {
                    "matched_reference_count": 4,
                    "expected_reference_count": 5,
                    "missing_reference_count": 1,
                }
                if outcome == "INCOMPLETE"
                else None
            ),
        }

    monkeypatch.setattr(
        validation_coordinator,
        "load_authority_verification_result",
        load_result,
    )
    return active, validation_coordinator.run_test_agent_validation()


def test_pending_incomplete_result_still_exposes_pending_commands(
    monkeypatch,
) -> None:
    active = _active_validation()
    incomplete_id = active["validation_authority_ids"][0]

    active, status = _run_validation_with_completed_outcomes(
        monkeypatch,
        {incomplete_id: "INCOMPLETE"},
    )

    assert status["status"] == "verification_pending"
    assert status["completed_authority_count"] == 1
    assert status["pending_authority_count"] == 40
    assert [
        item["authority_id"] for item in status["verification_assignments"]
    ] == active["validation_authority_ids"][1:9]
    assert len(status["next_commands"]) == 8
    assert not any("prepare-" in command for command in status["next_commands"])


def test_pending_failed_result_still_exposes_pending_commands(
    monkeypatch,
) -> None:
    active = _active_validation()
    failed_id = active["validation_authority_ids"][0]

    active, status = _run_validation_with_completed_outcomes(
        monkeypatch,
        {failed_id: "FAIL"},
    )

    assert status["status"] == "verification_pending"
    assert status["completed_authority_count"] == 1
    assert status["pending_authority_count"] == 40
    assert [
        item["authority_id"] for item in status["verification_assignments"]
    ] == active["validation_authority_ids"][1:9]
    assert len(status["next_commands"]) == 8

def test_no_pending_with_incomplete_result_requests_new_prepare(
    monkeypatch,
) -> None:
    active = _active_validation()
    incomplete_id = active["validation_authority_ids"][0]
    outcomes = {
        authority_id: "PASS"
        for authority_id in active["validation_authority_ids"]
    }
    outcomes[incomplete_id] = "INCOMPLETE"

    _, status = _run_validation_with_completed_outcomes(
        monkeypatch,
        outcomes,
    )

    assert status["status"] == "verification_incomplete"
    assert status["completed_authority_count"] == 41
    assert status["pending_authority_count"] == 0
    assert status["first_failed_authority_id"] == incomplete_id
    assert status["first_failed_outcome"] == "INCOMPLETE"
    assert status["query_stage"] == "trace_output_stability"
    assert status["error_code"] == "telemetry_not_stable"
    assert status["next_commands"] == [
        "python -m agent_insights_quality prepare-test-agent-validation"
    ]

def test_no_pending_complete_results_permit_failed_composition(
    monkeypatch,
) -> None:
    active = _active_validation()
    failed_id = active["validation_authority_ids"][0]
    outcomes = {
        authority_id: "PASS"
        for authority_id in active["validation_authority_ids"]
    }
    outcomes[failed_id] = "FAIL"

    _, status = _run_validation_with_completed_outcomes(
        monkeypatch,
        outcomes,
    )

    assert status["status"] == "composition_pending"
    assert status["completed_authority_count"] == 41
    assert status["first_failed_authority_id"] == failed_id
    assert status["first_failed_outcome"] == "FAIL"
    assert status["next_commands"] == [
        "python -m agent_insights_quality compose-test-agent-validation"
    ]


def test_verifier_still_fails_closed_before_invocation_barrier(
    monkeypatch,
    tmp_path,
) -> None:
    active = _active_validation()
    monkeypatch.setattr(
        "agent_insights_quality.validation_shards.validation_runtime_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_active_for_state",
        lambda _state: active,
    )

    with pytest.raises(
        ContractError,
        match="Validation invocation barrier is incomplete",
    ):
        validation_coordinator.verify_test_agent_validation_authority(
            authority_id="issue-014"
        )


def test_next_generation_reuses_forced_pass_and_selects_nonpass_or_missing() -> None:
    authorities = authority_specs(*load_catalogs())[:3]
    passed = authorities[0].authority_id
    failed = authorities[1].authority_id
    missing = authorities[2].authority_id
    selected, reused = _merge_authority_result_selection(
        authorities=authorities,
        selected=[],
        reused=[],
        authority_results={
            passed: {
                "authority_id": passed,
                "path": "synthetic/pass.json",
                "authority_result_digest": "sha256:" + ("1" * 64),
                "authority_evidence_digest": "sha256:" + ("2" * 64),
            },
            failed: None,
        },
        forced={passed, failed, missing},
    )
    assert selected == [failed, missing]
    assert [item["authority_id"] for item in reused] == [passed]


def test_next_generation_reuses_nine_passes_and_selects_remaining_32() -> None:
    authorities = authority_specs(*load_catalogs())
    passed = authorities[:9]
    incomplete_or_failed = authorities[9:11]
    changed_or_missing = authorities[11:]
    authority_results = {
        item.authority_id: {
            "authority_id": item.authority_id,
            "path": f"synthetic/{item.authority_id}.json",
            "authority_result_digest": "sha256:" + ("1" * 64),
            "authority_evidence_digest": "sha256:" + ("2" * 64),
        }
        for item in passed
    }
    authority_results.update(
        {item.authority_id: None for item in incomplete_or_failed}
    )

    selected, reused = _merge_authority_result_selection(
        authorities=authorities,
        selected=[],
        reused=[],
        authority_results=authority_results,
        forced={item.authority_id for item in authorities},
    )

    assert len(selected) == 32
    assert selected == [
        item.authority_id for item in incomplete_or_failed + changed_or_missing
    ]
    assert len(reused) == 9
    assert [item["authority_id"] for item in reused] == [
        item.authority_id for item in passed
    ]


def test_target_batched_verification_has_77_stability_windows() -> None:
    authorities = authority_specs(*load_catalogs())
    assert sum(
        1 if item.authority_kind == "baseline" else 2
        for item in authorities
    ) == 77


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
        quota_plan_digest="sha256:" + ("a" * 64),
    )
    assert len(desired["reused_runtimes"]) == 29
    assert len(desired["deployment_authority_ids"]) == 12
    assert len(desired["deployment_assignments"]) <= 8
    assert {
        authority_id
        for assignment in desired["deployment_assignments"]
        for authority_id in assignment["authority_ids"]
    } == set(desired["deployment_authority_ids"])


def test_support_image_migration_keeps_only_exact_issue_contexts(
    monkeypatch,
) -> None:
    agents, issues = load_catalogs()
    authorities = authority_specs(agents, issues)
    support = next(
        item
        for item in agents["agents"]
        if item["name"] == "support-ticket-agent"
    )
    support_authorities = [
        item for item in authorities if item.canonical_agent == support["name"]
    ]
    desired_authorities = []
    registry_authorities = []
    support_images = {}
    for index, authority in enumerate(support_authorities, start=1):
        provider_digest = f"sha256:{index:064x}"
        desired_authorities.append(
            {
                "authority_id": authority.authority_id,
                "authority_kind": authority.authority_kind,
                "canonical_agent": authority.canonical_agent,
                "logical_version": authority.logical_version,
                "runtime_kind": authority.runtime_kind,
                "framework": authority.framework,
                "runtime_agent_name": f"support-{authority.logical_version}",
                "source_content_digest": authority.source_content_digest,
                "provider_content_digest": "sha256:" + ("f" * 64),
                "version_intent": "sha256:" + ("e" * 64),
            }
        )
        registry_authorities.append(
            {
                "authority_id": authority.authority_id,
                "runtime_kind": authority.runtime_kind,
                "framework": authority.framework,
                "runtime_agent_name": f"support-{authority.logical_version}",
                "source_content_digest": authority.source_content_digest,
                "provider_content_digest": provider_digest,
                "version_intent": validation_coordinator.content_hash(
                    {
                        "runtime_agent_name": f"support-{authority.logical_version}",
                        "logical_version": authority.logical_version,
                        "provider_content_digest": provider_digest,
                    }
                ),
                "runtime": {
                    "authority_id": authority.authority_id,
                    "runtime_kind": authority.runtime_kind,
                    "runtime_agent_name": f"support-{authority.logical_version}",
                    "provider_content_digest": provider_digest,
                },
            }
        )
        support_images[authority.logical_version] = (
            "syntheticregistry.azurecr.io/"
            "agent-insights-quality-support@"
            f"sha256:{index:064x}"
        )
    desired = {
        "commit_sha": "a" * 40,
        "environment_id": "swedencentral-g30",
        "project_name": "aiq-staging-swedencentral",
        "authorities": desired_authorities,
        "support_images": support_images,
    }
    previous = SimpleNamespace(
        value={
            "repository": "synthetic/example",
            "pr_number": 65,
            "project": {"name": "aiq-staging-swedencentral"},
            "runtime_topology": {"agents": []},
            "desired_state_reference": {"path": "synthetic"},
        }
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_load_desired_state",
        lambda _active: desired,
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_read_deployment_registry",
        lambda: {
            "environment_id": "swedencentral-g30",
            "project_name": "aiq-staging-swedencentral",
            "authorities": registry_authorities,
        },
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_support_build_context_digest",
        lambda _root, version: f"sha256:{version:0>64}",
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_support_build_context_digest_at_commit",
        lambda _root, version, _commit: (
            None
            if version == "v0"
            else f"sha256:{version:0>64}"
        ),
    )

    candidates = _support_image_reuse_candidates(
        journal=SimpleNamespace(read_optional=lambda: previous),
        plan={
            "repository": "synthetic/example",
            "pr_number": 65,
            "environment_id": "swedencentral-g30",
            "project_name": "aiq-staging-swedencentral",
        },
        authorities=authorities,
        support_agent=support,
    )

    assert set(candidates) == {
        f"issue-{index:03d}" for index in range(29, 37)
    }
    assert {
        candidate["authority_id"] for candidate in candidates.values()
    } == set(candidates)

    issue_029_runtime = dict(
        next(
            item["runtime"]
            for item in registry_authorities
            if item["authority_id"] == "issue-029"
        )
    )
    issue_029_runtime["provider_content_digest"] = "sha256:" + ("c" * 64)
    previous.value["runtime_topology"]["agents"] = [issue_029_runtime]
    candidates = _support_image_reuse_candidates(
        journal=SimpleNamespace(read_optional=lambda: previous),
        plan={
            "repository": "synthetic/example",
            "pr_number": 65,
            "environment_id": "swedencentral-g30",
            "project_name": "aiq-staging-swedencentral",
        },
        authorities=authorities,
        support_agent=support,
    )
    assert "issue-029" not in candidates


def test_corrected_support_images_select_only_changed_authorities(
    monkeypatch,
) -> None:
    agents, issues = load_catalogs()
    policy = load_validation_policy()
    authorities = authority_specs(agents, issues)
    plan = prepare_validation_plan(
        agents=agents,
        issues=issues,
        policy=policy,
        repository=policy.repository,
        pr_number=65,
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
    changed = {"issue-006", "support-ticket-agent/v0"}
    desired_digests = {
        authority.authority_id: f"sha256:{index:064x}"
        for index, authority in enumerate(authorities, start=1)
    }
    registry_authorities = []
    for authority, target in zip(authorities, planned, strict=True):
        desired_digest = desired_digests[authority.authority_id]
        retained_digest = (
            "sha256:" + ("f" * 64)
            if authority.authority_id in changed
            else desired_digest
        )
        registry_authorities.append(
            {
                "authority_id": authority.authority_id,
                "runtime_kind": authority.runtime_kind,
                "framework": authority.framework,
                "runtime_agent_name": target.runtime_agent_name,
                "source_content_digest": authority.source_content_digest,
                "provider_content_digest": retained_digest,
                "version_intent": validation_coordinator.content_hash(
                    {
                        "runtime_agent_name": target.runtime_agent_name,
                        "logical_version": authority.logical_version,
                        "provider_content_digest": retained_digest,
                    }
                ),
                "runtime": {
                    "authority_id": authority.authority_id,
                    "provider_content_digest": retained_digest,
                },
            }
        )
    monkeypatch.setattr(
        validation_coordinator,
        "_read_deployment_registry",
        lambda: {"authorities": registry_authorities},
    )

    class Deployer:
        @staticmethod
        def desired_content_digest(authority):
            return desired_digests[authority.authority_id]

        @staticmethod
        def find_existing(authority, _target):
            assert authority.authority_id in changed
            return None

    desired = _desired_state(
        plan=plan,
        authorities=authorities,
        planned=planned,
        deployer=Deployer(),
        support_images={
            ("v0" if index == 0 else f"issue-{index + 28:03d}"): (
                "syntheticregistry.azurecr.io/"
                "agent-insights-quality-support@"
                f"sha256:{index:064x}"
            )
            for index in range(9)
        },
        superseded_authority_ids=[],
        forced_invocation_authority_ids=[],
        quota_plan_digest="sha256:" + ("a" * 64),
    )

    assert desired["deployment_authority_ids"] == [
        "issue-006",
        "support-ticket-agent/v0",
    ]
    assert {
        item["authority_id"]
        for item in desired["reused_runtimes"]
        if item["authority_id"].startswith("issue-0")
        and int(item["authority_id"][-3:]) >= 29
    } == {f"issue-{index:03d}" for index in range(29, 37)}


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
        "policy": SimpleNamespace(
            trace_hydration_stabilization_seconds=1,
            trace_hydration_poll_seconds=1,
            trace_hydration_maximum_wait_seconds=2,
        ),
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
                trace_hydration_stabilization_seconds=1,
                trace_hydration_poll_seconds=1,
                trace_hydration_maximum_wait_seconds=2,
            ),
        }
    )
    assert not hasattr(verifier, "invoke")
    assert not hasattr(verifier, "prepare_hosted_routes")
    runtime = verifier._FoundryScenarioVerifier__delegate._runtime
    for forbidden in (
        "_json_request",
        "_invoke_prompt",
        "_invoke_hosted",
        "_create_hosted_session",
        "_activate_hosted_version",
    ):
        assert not hasattr(runtime, forbidden)
