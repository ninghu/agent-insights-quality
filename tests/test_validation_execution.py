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


def test_phase_two_and_traffic_follow_phase_one_and_two_clean_windows() -> None:
    source = inspect.getsource(_execute_validation_plan)
    first_route = source.index("runner.prepare_hosted_routes")
    second_route = source.index("runner.prepare_hosted_routes", first_route + 1)
    assert first_route < source.index('wait_clean_interval("phase_1")')
    assert source.index('wait_clean_interval("phase_1")') < source.index(
        "phase_one_evidence = execute_validation_phase"
    )
    assert source.index(
        "phase_one_evidence = execute_validation_phase"
    ) < source.index("controller.begin_phase_two_deployment")
    assert second_route < source.index('wait_clean_interval("phase_2")')
    assert source.index('wait_clean_interval("phase_2")') < source.index(
        "phase_two_evidence = execute_validation_phase"
    )


def test_lifecycle_heartbeat_must_stay_below_reviewed_maximum() -> None:
    with pytest.raises(ContractError, match="below 60"):
        with lifecycle_heartbeat(
            SimpleNamespace(),
            now=lambda: datetime.now(UTC),
            interval_seconds=60,
        ):
            pass
