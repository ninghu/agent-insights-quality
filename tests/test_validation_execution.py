from __future__ import annotations

from datetime import UTC, datetime
import inspect
from types import SimpleNamespace

import pytest

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_execution import (
    _execute_validation_plan,
    _validation_phases,
    execute_validation_plan,
    lifecycle_heartbeat,
)
from agent_insights_quality.validation_manifest import authority_specs
from agent_insights_quality.validation_live import FoundryScenarioAttemptRunner
from agent_insights_quality.validation_policy import load_validation_policy


def test_execution_failure_enters_cleanup_with_public_safe_digest() -> None:
    cleanup = []

    class Controller:
        active = SimpleNamespace(value={"state": "LOCKED"})

        @staticmethod
        def begin_cleanup(*, failure, now):
            cleanup.append((failure, now))

    now = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    with pytest.raises(ContractError, match="validation plan"):
        execute_validation_plan(
            plan={"kind": "invalid"},
            authorities=[],
            capacity_plan=None,
            controller=Controller(),
            project_provisioner=None,
            deployer_factory=None,
            support_image_factory=None,
            runner=None,
            scheduler=None,
            policy=None,
            model_contract={},
            assert_commit=lambda: None,
            now=lambda: now,
        )
    failure = cleanup[0][0]
    assert set(failure) == {"error_code", "detail_digest", "failed_at"}
    assert failure["error_code"] == "validation_execution_failed"
    assert failure["failed_at"] == now.isoformat()
    assert failure["detail_digest"].startswith("sha256:")


def test_two_phase_contract_uses_only_fixed_v0_canaries_first() -> None:
    agents, issues = load_catalogs()
    policy = load_validation_policy()
    phase_one, phase_two = _validation_phases(
        authority_specs(agents, issues),
        policy,
    )
    assert [item.authority_id for item in phase_one] == [
        "weather-agent/v0",
        "finance-agent/v0",
    ]
    assert len(phase_two) == 39
    assert {item.authority_id for item in phase_one}.isdisjoint(
        {item.authority_id for item in phase_two}
    )


def test_no_pretraffic_clean_window_remains() -> None:
    source = inspect.getsource(_execute_validation_plan)
    assert "wait_clean_interval" not in source
    assert "clean_interval_seconds" not in source
    assert source.count("runner.prepare_hosted_routes") == 3
    preparation = inspect.getsource(
        FoundryScenarioAttemptRunner.prepare_hosted_routes
    )
    attempt = inspect.getsource(FoundryScenarioAttemptRunner._run)
    assert "refresh_route=True" not in preparation
    assert attempt.index("refresh_route=True") < attempt.index(
        "_create_hosted_session"
    )


def test_issue_recovery_keeps_superseded_generation_until_final_cleanup() -> None:
    source = inspect.getsource(_execute_validation_plan)
    recovery = source[
        source.index("def recover_issue(") : source.index(
            "def record_completion("
        )
    ]
    assert "issue_execution_recovery_intent" in recovery
    assert "recovery_runtime_plan" in recovery
    assert "retry_transient_failures=False" in recovery
    assert "force_new_authority_ids={authority.authority_id}" in recovery
    assert "CleanupEngine" not in recovery
    assert "authority_replacement_ready" in recovery
    assert "wait_clean_interval" not in recovery
    assert "phase_two_deployed[authority.authority_id] = replacement" in recovery
    assert "deployed[authority.authority_id] = replacement" in recovery
    phase_call = source[source.index("phase_two_evidence =") :]
    assert "recover_issue=recover_issue" in phase_call
    assert "record_completion=record_completion" in phase_call


def test_lifecycle_heartbeat_must_stay_below_reviewed_maximum() -> None:
    with pytest.raises(ContractError, match="below 60"):
        with lifecycle_heartbeat(
            SimpleNamespace(),
            now=lambda: datetime.now(UTC),
            interval_seconds=60,
        ):
            pass
