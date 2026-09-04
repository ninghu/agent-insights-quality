from __future__ import annotations

import copy
import inspect
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.live import LiveRuntime
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.util import ContractError, atomic_json, content_hash
from agent_insights_quality.validation_coordinator import (
    _assignments,
    _current_invocation_requirements,
    _desired_state,
    _forced_invocation_authority_ids,
    _merge_authority_result_selection,
    _runner,
    _support_image_reuse_candidates,
    _verifier,
)
from agent_insights_quality import (
    validation_coordinator,
    validation_copilot,
    validation_runtime,
)
from agent_insights_quality.validation_assignments import verification_assignment
from agent_insights_quality.validation_copilot import COPILOT_CLAIM_LEASE
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


def test_public_history_lists_all_authorities_without_private_bindings() -> None:
    history = validation_coordinator._public_authority_history(
        _active_validation()
    )

    assert len({item["canonical_agent"] for item in history}) == 5
    assert {item["status"] for item in history} == {"missing"}
    assert all(
        set(item)
        == {
            "authority_id",
            "canonical_agent",
            "status",
            "changed",
            "verification_required_reason",
        }
        for item in history
    )


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
        fresh_current_invocations=[],
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


def _requirements_for_endpoint_pass(
    monkeypatch,
    *,
    endpoint_pass: bool,
    repeated_nonpass: bool = False,
) -> tuple[str, list[str], list[str]]:
    active = _active_validation()
    authority_id = "issue-001"
    active["invocation_authority_ids"] = []
    journal = SimpleNamespace(
        read_active=lambda: SimpleNamespace(value=active),
    )
    monkeypatch.setattr(
        validation_coordinator,
        "current_authority_verification_results",
        lambda **_kwargs: {authority_id: {"authority_id": authority_id}},
    )
    monkeypatch.setattr(
        validation_coordinator,
        "load_authority_verification_result",
        lambda _reference: {
            "outcome": "INCOMPLETE",
            "authority_evidence": {
                "scenarios": [
                    {
                        "issue_attempts": [
                            {
                                "setup_steps": [
                                    {"endpoint_pass": endpoint_pass}
                                ],
                                "probe_steps": [{"endpoint_pass": True}],
                            }
                        ],
                        "v0_attempts": [],
                    }
                ]
            },
        },
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_recovery_source_has_same_nonpass",
        lambda _active, _result: repeated_nonpass,
    )
    incomplete, endpoint_bad = _current_invocation_requirements(
        journal=journal,
        plan={
            "repository": active["repository"],
            "pr_number": active["pr_number"],
        },
        authorities=authority_specs(*load_catalogs()),
    )
    return authority_id, incomplete, endpoint_bad


def test_endpoint_bad_incomplete_overrides_supplemental_receipt(
    monkeypatch,
) -> None:
    authority_id, incomplete, endpoint_bad = (
        _requirements_for_endpoint_pass(
            monkeypatch,
            endpoint_pass=False,
        )
    )
    forced = _forced_invocation_authority_ids(
        migration={"incomplete_authority_ids": []},
        supplemental={
            "imported_authority_ids": [authority_id],
            "incomplete_authority_ids": [],
        },
        incomplete_current_invocations=incomplete,
        fresh_current_invocations=endpoint_bad,
    )

    assert incomplete == endpoint_bad == [authority_id]
    assert forced == [authority_id]


def test_telemetry_only_incomplete_remains_verify_only(
    monkeypatch,
) -> None:
    authority_id, incomplete, endpoint_bad = (
        _requirements_for_endpoint_pass(
            monkeypatch,
            endpoint_pass=True,
        )
    )
    forced = _forced_invocation_authority_ids(
        migration={"incomplete_authority_ids": []},
        supplemental={
            "imported_authority_ids": [authority_id],
            "incomplete_authority_ids": [],
        },
        incomplete_current_invocations=incomplete,
        fresh_current_invocations=endpoint_bad,
    )

    assert incomplete == endpoint_bad == []
    assert forced == []


def test_repeated_nonpass_receipt_forces_one_fresh_traffic_set(
    monkeypatch,
) -> None:
    authority_id, incomplete, fresh = _requirements_for_endpoint_pass(
        monkeypatch,
        endpoint_pass=True,
        repeated_nonpass=True,
    )
    forced = _forced_invocation_authority_ids(
        migration={"incomplete_authority_ids": []},
        supplemental={
            "imported_authority_ids": [authority_id],
            "incomplete_authority_ids": [],
        },
        incomplete_current_invocations=incomplete,
        fresh_current_invocations=fresh,
    )

    assert incomplete == fresh == forced == [authority_id]


def test_invocation_recovery_does_not_cross_pull_requests(monkeypatch) -> None:
    active = _active_validation()
    journal = SimpleNamespace(
        read_active=lambda: SimpleNamespace(value=active),
        superseded_run_ids=lambda _active: pytest.fail(
            "cross-PR history must not be read"
        ),
    )

    assert _current_invocation_requirements(
        journal=journal,
        plan={
            "repository": active["repository"],
            "pr_number": active["pr_number"] + 1,
        },
        authorities=authority_specs(*load_catalogs()),
    ) == ([], [])


def test_fresh_advisory_generation_does_not_reuse_global_receipt_history(
    monkeypatch,
) -> None:
    active = _active_validation()
    authority_ids = list(active["invocation_authority_ids"])
    journal = SimpleNamespace(
        read_active=lambda: SimpleNamespace(value=active),
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_source_invocation_receipt_references",
        lambda _active: [],
    )
    monkeypatch.setattr(
        validation_coordinator,
        "current_authority_verification_results",
        lambda **_kwargs: {},
    )

    assert _current_invocation_requirements(
        journal=journal,
        plan={
            "repository": active["repository"],
            "pr_number": active["pr_number"],
        },
        authorities=authority_specs(*load_catalogs()),
    ) == (authority_ids, [])


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
        "invocation_authority_ids": ["issue-014"],
        "reused_invocations": [],
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


def _copilot_scheduling_context(monkeypatch, tmp_path) -> dict:
    active = _active_validation()
    active["invocation_shard_assignments"] = []
    authorities = authority_specs(*load_catalogs())
    paired_baselines = {
        item.canonical_agent: item.authority_id
        for item in authorities
        if item.authority_kind == "baseline"
    }
    context = {
        "prepared": active,
        "authorities": authorities,
        "paired_baselines": paired_baselines,
        "plan": {},
        "deployed": {
            item.authority_id: object() for item in authorities
        },
        "scheduler": object(),
    }
    claimant = {"value": content_hash("claimant-0")}
    results: dict[str, dict[str, str]] = {}
    result_values: dict[str, dict] = {}
    package_calls: list[str] = []

    monkeypatch.setattr(
        validation_copilot,
        "validation_runtime_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_active_for_state",
        lambda _state: active,
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_incomplete_invocation_shards",
        lambda _active: [],
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_load_prepared",
        lambda: context,
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_assert_active_generation",
        lambda _prepared: None,
    )
    monkeypatch.setattr(
        validation_coordinator,
        "copilot_claimant_reference",
        lambda: claimant["value"],
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_invocation_receipts_for_verification",
        lambda _context, authority_ids: (
            [
                {
                    "authority_id": authority_ids[0],
                    "path": "private/receipt.json",
                    "receipt_digest": content_hash(
                        {"receipt": authority_ids[0]}
                    ),
                    "invocation_digest": content_hash(
                        {"invocation": authority_ids[0]}
                    ),
                }
            ],
            [{"invocation": {"authority_id": authority_ids[0]}}],
        ),
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_verifier",
        lambda _context: object(),
    )

    def current_results(*, authority_ids, **_kwargs):
        return {
            authority_id: results[authority_id]
            for authority_id in authority_ids
            if authority_id in results
        }

    monkeypatch.setattr(
        validation_coordinator,
        "current_authority_verification_results",
        current_results,
    )

    def write_package(*, authority, started_at, fence, **_kwargs):
        fence()
        package_calls.append(authority.authority_id)
        package_hash = content_hash(
            {
                "authority_id": authority.authority_id,
                "started_at": started_at.isoformat(),
            }
        )
        private_root = validation_copilot.evaluation_root()
        path = (
            private_root
            / "packages"
            / f"{package_hash.removeprefix('sha256:')}.json"
        )
        atomic_json(path, {"authority_id": authority.authority_id})
        fence()
        return {
            "package_hash": package_hash,
            "path": path,
            "assessment_path": validation_copilot.assessment_path(
                package_hash
            ),
        }

    monkeypatch.setattr(
        validation_coordinator,
        "write_private_package",
        write_package,
    )
    monkeypatch.setattr(
        validation_coordinator,
        "load_bound_private_package",
        lambda pointer, **_kwargs: {
            "package_hash": pointer["package_hash"],
            "authority_id": pointer["authority_id"],
            "created_at": pointer["claimed_at"],
        },
    )
    monkeypatch.setattr(
        validation_coordinator,
        "load_copilot_evaluation",
        lambda _path, **_kwargs: (
            {},
            {
                "model": "gpt-5.6-sol",
                "package_hash": "synthetic",
                "prompt_digest": "synthetic",
                "evaluation_digest": "synthetic",
            },
        ),
    )
    monkeypatch.setattr(
        validation_coordinator,
        "authority_evidence_from_evaluation",
        lambda **_kwargs: {"pass": True},
    )
    monkeypatch.setattr(
        validation_coordinator,
        "authority_verification_outcome",
        lambda _evidence: ("PASS", None, None),
    )

    def write_result(*, authority, **_kwargs):
        authority_id = authority.authority_id
        reference = {
            "authority_id": authority_id,
            "path": f"private/{authority_id}.json",
            "authority_result_digest": content_hash(
                {"result": authority_id}
            ),
            "authority_evidence_digest": content_hash(
                {"evidence": authority_id}
            ),
        }
        results[authority_id] = reference
        result_values[authority_id] = {
            "authority_id": authority_id,
            "outcome": "PASS",
            "query_stage": None,
            "error_code": None,
            "query_diagnostics": None,
            "artifact_digest": reference["authority_result_digest"],
        }
        return reference

    monkeypatch.setattr(
        validation_coordinator,
        "write_authority_verification_result",
        write_result,
    )
    monkeypatch.setattr(
        validation_coordinator,
        "load_authority_verification_result",
        lambda reference: result_values[reference["authority_id"]],
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
    return {
        "active": active,
        "claimant": claimant,
        "results": results,
        "package_calls": package_calls,
    }


def test_eight_unique_copilot_claims_fill_capacity_and_ninth_is_blocked(
    monkeypatch,
    tmp_path,
) -> None:
    state = _copilot_scheduling_context(monkeypatch, tmp_path)
    prepared = state["active"]
    prepared_results = []

    for index in range(8):
        state["claimant"]["value"] = content_hash(f"claimant-{index}")
        prepared_results.append(
            validation_coordinator.prepare_test_agent_validation_assessment()
        )

    assert {
        item["status"] for item in prepared_results
    } == {"assessment_ready"}
    assert len(
        {item["package_path"] for item in prepared_results}
    ) == 8
    assert len(
        {item["assessment_path"] for item in prepared_results}
    ) == 8
    claims = validation_copilot.active_copilot_claims(prepared=prepared)
    assert len(claims) == 8
    assert len({item["authority_id"] for item in claims}) == 8
    assert len({item["claimant_reference"] for item in claims}) == 8

    state["claimant"]["value"] = content_hash("claimant-8")
    blocked = (
        validation_coordinator.prepare_test_agent_validation_assessment()
    )
    assert blocked == {
        "status": "assessment_capacity_full",
        "pending_authority_count": len(
            _active_validation()["validation_authority_ids"]
        ),
        "active_authority_evaluator_count": 8,
        "available_authority_evaluator_slots": 0,
    }
    assert "package_path" not in blocked
    assert "assessment_path" not in blocked

    status = validation_coordinator.run_test_agent_validation()
    assert status["maximum_active_subsessions"] == 8
    assert status["active_authority_evaluator_count"] == 8
    assert status["available_authority_evaluator_slots"] == 0
    assert status["next_commands"] == []
    assert "claimant_reference" not in status
    assert "package_path" not in status
    assert "assessment_path" not in status

    state["claimant"]["value"] = content_hash("claimant-0")
    resumed = (
        validation_coordinator.prepare_test_agent_validation_assessment()
    )
    assert resumed["package_path"] == prepared_results[0]["package_path"]
    assert resumed["assessment_path"] == prepared_results[0]["assessment_path"]
    assert len(state["package_calls"]) == 8


def test_copilot_import_isolated_by_worktree_and_replenishes_capacity(
    monkeypatch,
    tmp_path,
) -> None:
    state = _copilot_scheduling_context(monkeypatch, tmp_path)
    first_claimant = content_hash("first-worktree")
    second_claimant = content_hash("second-worktree")
    state["claimant"]["value"] = first_claimant
    first = validation_coordinator.prepare_test_agent_validation_assessment()

    state["claimant"]["value"] = second_claimant
    with pytest.raises(
        ContractError,
        match="worktree has no active",
    ):
        validation_coordinator.import_test_agent_validation_assessment()

    state["claimant"]["value"] = first_claimant
    imported = (
        validation_coordinator.import_test_agent_validation_assessment()
    )
    assert imported["status"] == "verified"
    assert imported["outcome"] == "PASS"
    assert (
        validation_copilot.active_copilot_claims(
            prepared=state["active"]
        )
        == []
    )

    state["claimant"]["value"] = second_claimant
    replacement = (
        validation_coordinator.prepare_test_agent_validation_assessment()
    )
    assert replacement["status"] == "assessment_ready"
    assert replacement["package_path"] != first["package_path"]
    assert replacement["active_authority_evaluator_count"] == 1
    assert len(state["results"]) == 1


def test_copilot_can_release_only_its_own_claim_for_immediate_reuse(
    monkeypatch,
    tmp_path,
) -> None:
    state = _copilot_scheduling_context(monkeypatch, tmp_path)
    first_claimant = content_hash("first-worktree")
    second_claimant = content_hash("second-worktree")
    state["claimant"]["value"] = first_claimant
    first = validation_coordinator.prepare_test_agent_validation_assessment()

    released = validation_coordinator.release_test_agent_validation_assessment()

    assert released["status"] == "assessment_released"
    assert validation_copilot.active_copilot_claims(
        prepared=state["active"]
    ) == []
    with pytest.raises(ContractError, match="was released"):
        validation_coordinator.import_test_agent_validation_assessment()

    state["claimant"]["value"] = second_claimant
    replacement = (
        validation_coordinator.prepare_test_agent_validation_assessment()
    )
    assert replacement["status"] == "assessment_ready"
    assert replacement["package_path"] != first["package_path"]

    stale_pointer = validation_copilot.load_claim_pointer(
        claimant_reference=second_claimant,
    )
    validation_copilot.release_active_pointer(stale_pointer)
    with pytest.raises(ContractError, match="changed before release"):
        validation_copilot.release_active_pointer(
            stale_pointer,
        )


def test_copilot_release_rejects_another_worktrees_claim(
    monkeypatch,
    tmp_path,
) -> None:
    state = _copilot_scheduling_context(monkeypatch, tmp_path)
    first_claimant = content_hash("first-worktree")
    second_claimant = content_hash("second-worktree")
    state["claimant"]["value"] = first_claimant
    validation_coordinator.prepare_test_agent_validation_assessment()

    state["claimant"]["value"] = second_claimant
    with pytest.raises(ContractError, match="no Copilot assessment claim"):
        validation_coordinator.release_test_agent_validation_assessment()
    assert len(
        validation_copilot.active_copilot_claims(prepared=state["active"])
    ) == 1


def test_completed_copilot_result_cannot_be_released(
    monkeypatch,
    tmp_path,
) -> None:
    state = _copilot_scheduling_context(monkeypatch, tmp_path)
    validation_coordinator.prepare_test_agent_validation_assessment()
    validation_coordinator.import_test_agent_validation_assessment()

    with pytest.raises(ContractError, match="Completed.*cannot be released"):
        validation_coordinator.release_test_agent_validation_assessment()
    assert len(state["results"]) == 1


def test_preassignment_lock_contention_is_structured_and_retryable(
    monkeypatch,
    tmp_path,
) -> None:
    state = _copilot_scheduling_context(monkeypatch, tmp_path)

    class BusyLock:
        def __enter__(self):
            raise validation_coordinator.ValidationLockBusy("synthetic busy")

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(
        validation_coordinator,
        "evaluation_lock",
        lambda: BusyLock(),
    )

    result = validation_coordinator.prepare_test_agent_validation_assessment()

    assert result == {
        "status": "assessment_busy",
        "retryable": True,
        "next_command": (
            "python -m agent_insights_quality "
            "prepare-test-agent-validation-assessment"
        ),
    }
    assert state["results"] == {}
    assert validation_copilot.active_copilot_claims(
        prepared=state["active"]
    ) == []


def test_copilot_claim_fence_failure_never_persists_incomplete_result(
    monkeypatch,
    tmp_path,
) -> None:
    state = _copilot_scheduling_context(monkeypatch, tmp_path)
    monkeypatch.setattr(
        validation_coordinator,
        "write_private_package",
        lambda **_kwargs: (_ for _ in ()).throw(
            validation_copilot.CopilotClaimError("synthetic contention")
        ),
    )

    with pytest.raises(
        validation_copilot.CopilotClaimError,
        match="synthetic contention",
    ):
        validation_coordinator.prepare_test_agent_validation_assessment()

    assert state["results"] == {}
    claims = validation_copilot.active_copilot_claims(
        prepared=state["active"]
    )
    assert claims == []


def test_distinct_copilot_claims_prepare_concurrently_outside_global_lock(
    monkeypatch,
    tmp_path,
) -> None:
    state = _copilot_scheduling_context(monkeypatch, tmp_path)
    claimants = threading.local()
    barrier = threading.Barrier(2)
    original_write = validation_coordinator.write_private_package
    results: list[dict] = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()
    monkeypatch.setattr(
        validation_coordinator,
        "copilot_claimant_reference",
        lambda: claimants.value,
    )
    monkeypatch.setattr(
        validation_coordinator,
        "evaluation_lock",
        lambda: validation_coordinator.LocalValidationLock(
            tmp_path / "coordinator.lock",
            wait_seconds=0.25,
        ),
    )

    def concurrent_write(**kwargs):
        barrier.wait(timeout=2)
        return original_write(**kwargs)

    monkeypatch.setattr(
        validation_coordinator,
        "write_private_package",
        concurrent_write,
    )

    def prepare(index: int) -> None:
        claimants.value = content_hash(f"concurrent-worktree-{index}")
        try:
            result = (
                validation_coordinator.prepare_test_agent_validation_assessment()
            )
        except BaseException as error:
            with results_lock:
                errors.append(error)
        else:
            with results_lock:
                results.append(result)

    threads = [
        threading.Thread(target=prepare, args=(index,))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert {item["status"] for item in results} == {"assessment_ready"}
    claims = validation_copilot.active_copilot_claims(
        prepared=state["active"]
    )
    assert len(claims) == 2
    assert len({item["authority_id"] for item in claims}) == 2
    assert len({item["claimant_reference"] for item in claims}) == 2


def test_copilot_import_rejects_stale_generation(
    monkeypatch,
    tmp_path,
) -> None:
    state = _copilot_scheduling_context(monkeypatch, tmp_path)
    validation_coordinator.prepare_test_agent_validation_assessment()
    state["active"]["run_id"] = "validation-fedcba987654"

    with pytest.raises(
        ContractError,
        match="Stale Copilot assessment session",
    ):
        validation_coordinator.import_test_agent_validation_assessment()


def test_abandoned_copilot_claim_is_reclaimable_after_bounded_lease(
    tmp_path,
) -> None:
    prepared = _active_validation()
    started = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    authority_id = prepared["validation_authority_ids"][0]
    first_claimant = validation_copilot.copilot_claimant_reference(
        tmp_path / "first-worktree"
    )
    second_claimant = validation_copilot.copilot_claimant_reference(
        tmp_path / "second-worktree"
    )
    first = validation_copilot.write_active_pointer(
        prepared=prepared,
        authority_id=authority_id,
        claimant_reference=first_claimant,
        claimed_at=started,
        root=tmp_path,
    )
    assert str(tmp_path.resolve()) not in str(first)
    assert validation_copilot.active_copilot_claims(
        prepared=prepared,
        now=started + timedelta(minutes=1),
        root=tmp_path,
    ) == [first]

    expired_at = started + COPILOT_CLAIM_LEASE
    assert (
        validation_copilot.active_copilot_claims(
            prepared=prepared,
            now=expired_at,
            root=tmp_path,
        )
        == []
    )
    with pytest.raises(ContractError, match="lease expired"):
        validation_copilot.load_active_pointer(
            claimant_reference=first_claimant,
            now=expired_at,
            require_ready=False,
            root=tmp_path,
        )

    replacement = validation_copilot.write_active_pointer(
        prepared=prepared,
        authority_id=authority_id,
        claimant_reference=second_claimant,
        claimed_at=expired_at,
        root=tmp_path,
    )
    assert validation_copilot.active_copilot_claims(
        prepared=prepared,
        now=expired_at + timedelta(minutes=1),
        root=tmp_path,
    ) == [replacement]


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

    assert "verification_assignments" not in reconciled
    assert reconciled["verification_authority_concurrency"] == 0
    assert status["status"] == "invocation_pending"
    assert "verification_assignments" not in status
    assert not any("verify-" in command for command in status["next_commands"])


def test_completed_invocation_exposes_eight_copilot_verification_slots(
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
    monkeypatch.setattr(
        validation_coordinator,
        "active_copilot_claims",
        lambda **_kwargs: [],
    )

    reconciled = validation_coordinator._prepared_result(active)
    status = validation_coordinator.run_test_agent_validation()

    assert reconciled["verification_authority_concurrency"] == 8
    assert reconciled["verification_pending_authority_count"] == len(
        _active_validation()["validation_authority_ids"]
    )
    assert status["status"] == "verification_pending"
    assert status["maximum_active_subsessions"] == 8
    assert status["active_authority_evaluator_count"] == 0
    assert status["available_authority_evaluator_slots"] == 8
    assert status["next_commands"] == [
        "python -m agent_insights_quality "
        "prepare-test-agent-validation-assessment"
    ]

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

    assert replenished["pending_authority_count"] == 33
    assert replenished["next_commands"] == [
        "python -m agent_insights_quality "
        "prepare-test-agent-validation-assessment"
    ]


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
    monkeypatch.setattr(
        validation_coordinator,
        "active_copilot_claims",
        lambda **_kwargs: [],
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
    assert status["next_commands"] == [
        "python -m agent_insights_quality "
        "prepare-test-agent-validation-assessment"
    ]


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
    assert status["next_commands"] == [
        "python -m agent_insights_quality "
        "prepare-test-agent-validation-assessment"
    ]

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
    assert status["pending_authority_count"] == 0
    assert status["first_failed_authority_id"] == incomplete_id
    assert status["first_failed_outcome"] == "INCOMPLETE"
    assert status["query_stage"] == "trace_output_stability"
    assert status["error_code"] == "telemetry_not_stable"
    assert status["next_commands"] == [
        "python -m agent_insights_quality recover-test-agent-validation"
    ]


def test_recovery_advances_and_reconciles_to_zero_traffic_recheck(
    monkeypatch,
) -> None:
    source = _active_validation()
    source["journal_digest"] = content_hash({"source": source["run_id"]})
    incomplete_id = source["validation_authority_ids"][0]
    successor = copy.deepcopy(source)
    successor["run_id"] = "validation-fedcba987654"
    successor["validation_authority_ids"] = [incomplete_id]
    successor["reused_authorities"] = [
        {
            "authority_id": authority_id,
            "path": f"synthetic/{authority_id}.json",
            "authority_result_digest": content_hash({"result": authority_id}),
            "authority_evidence_digest": content_hash(
                {"evidence": authority_id}
            ),
        }
        for authority_id in source["validation_authority_ids"][1:]
    ]
    successor["invocation_authority_ids"] = []
    successor["reused_invocations"] = [
        {
            "authority_id": incomplete_id,
            "path": "synthetic/invocation.json",
            "receipt_digest": content_hash("receipt"),
            "invocation_digest": content_hash("invocation"),
        }
    ]
    successor["invocation_shard_assignments"] = []
    successor["verification_authority_assignments"] = [
        verification_assignment(successor, incomplete_id)
    ]
    calls: list[tuple[str, str | None]] = []
    git = SimpleNamespace(
        repository=source["repository"],
        pr_number=source["pr_number"],
        commit_sha=source["commit_sha"],
    )
    monkeypatch.setattr(
        validation_coordinator,
        "discover_local_git_context",
        lambda: git,
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_matching_active",
        lambda _git: source,
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_validation_recovery_candidate",
        lambda _active, *, git: {
            "incomplete_authority_ids": [incomplete_id]
        },
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_prepare_test_agent_validation",
        lambda *, recovery_source_digest: (
            calls.append(("prepare", recovery_source_digest))
            or {"deployment_shards": []}
        ),
    )
    monkeypatch.setattr(
        validation_coordinator,
        "reconcile_test_agent_validation_deployment",
        lambda: calls.append(("reconcile", None)),
    )
    monkeypatch.setattr(
        validation_coordinator,
        "_active_for_state",
        lambda _state: successor,
    )
    monkeypatch.setattr(
        validation_coordinator.LifecycleJournal,
        "superseded_run_ids",
        lambda _journal, _successor: [source["run_id"]],
    )

    result = validation_coordinator.recover_test_agent_validation()

    assert calls == [
        ("prepare", source["journal_digest"]),
        ("reconcile", None),
    ]
    assert result["status"] == "recovery_verification_pending"
    assert result["recovery_authority_count"] == 1
    assert result["deployment_shards"] == []
    assert result["invoke_shards"] == []
    assert result["verification_pending_authority_count"] == 1
    assert result["reused_authority_count"] == 40
    assert result["next_commands"] == [
        "python -m agent_insights_quality "
        "prepare-test-agent-validation-assessment"
    ]


def test_recovery_successor_preserves_immutable_ancestry_and_scope() -> None:
    source = _active_validation()
    source_before = copy.deepcopy(source)
    incomplete_ids = source["validation_authority_ids"][:2]
    successor = copy.deepcopy(source)
    successor["run_id"] = "validation-fedcba987654"
    successor["validation_authority_ids"] = incomplete_ids
    successor["reused_authorities"] = [
        {"authority_id": authority_id}
        for authority_id in source["validation_authority_ids"]
        if authority_id not in incomplete_ids
    ]
    successor["invocation_authority_ids"] = [incomplete_ids[1]]
    successor["invocation_shard_assignments"] = [
        {
            "shard_id": 1,
            "authority_ids": [incomplete_ids[1]],
            "quota_plan_digest": successor["digests"]["quota_plan_digest"],
        }
    ]
    successor["verification_authority_assignments"] = [
        verification_assignment(successor, authority_id)
        for authority_id in incomplete_ids
    ]

    validation_coordinator._validate_recovery_successor(
        source=source,
        successor=successor,
        incomplete_authority_ids=incomplete_ids,
        ancestor_run_ids=[source["run_id"]],
    )

    assert source == source_before
    successor["invocation_authority_ids"].append(
        source["validation_authority_ids"][2]
    )
    with pytest.raises(ContractError, match="successor selection is invalid"):
        validation_coordinator._validate_recovery_successor(
            source=source,
            successor=successor,
            incomplete_authority_ids=incomplete_ids,
            ancestor_run_ids=[source["run_id"]],
        )


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
        validation_coordinator.prepare_test_agent_validation_assessment()


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


def test_next_generation_reuses_four_imports_with_fourteen_retained_passes() -> None:
    authorities = authority_specs(*load_catalogs())
    retained = authorities[:14]
    imported = authorities[14:18]
    passed = [*retained, *imported]
    authority_results = {
        item.authority_id: {
            "authority_id": item.authority_id,
            "path": f"synthetic/{item.authority_id}.json",
            "authority_result_digest": content_hash(
                {"result": item.authority_id}
            ),
            "authority_evidence_digest": content_hash(
                {"evidence": item.authority_id}
            ),
        }
        for item in passed
    }

    selected, reused = _merge_authority_result_selection(
        authorities=authorities,
        selected=[],
        reused=[],
        authority_results=authority_results,
        forced={item.authority_id for item in authorities},
    )

    assert selected == [
        item.authority_id for item in authorities[len(passed) :]
    ]
    assert [item["authority_id"] for item in reused] == [
        item.authority_id for item in passed
    ]


def test_next_generation_reuses_twenty_passes_and_selects_remaining_21() -> None:
    authorities = authority_specs(*load_catalogs())
    passed = authorities[:20]
    incomplete = authorities[20:22]
    changed_or_missing = authorities[22:]
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
        {item.authority_id: None for item in incomplete}
    )

    selected, reused = _merge_authority_result_selection(
        authorities=authorities,
        selected=[],
        reused=[],
        authority_results=authority_results,
        forced={item.authority_id for item in authorities},
    )

    assert len(selected) == 21
    assert selected == [
        item.authority_id for item in incomplete + changed_or_missing
    ]
    assert len(reused) == 20
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
