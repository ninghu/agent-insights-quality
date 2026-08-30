from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_execution import (
    execute_initial_candidate_pass,
    lifecycle_heartbeat,
)


def test_initial_execution_failure_enters_cleanup_with_public_safe_digest() -> None:
    cleanup = []

    class Controller:
        active = SimpleNamespace(value={"state": "LEASED"})

        @staticmethod
        def begin_cleanup(*, failure, now):
            cleanup.append((failure, now))

    now = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    with pytest.raises(ContractError, match="candidate manifest"):
        execute_initial_candidate_pass(
            candidate={"kind": "invalid"},
            authorities=[],
            capacity_plan=None,
            controller=Controller(),
            project_provisioner=None,
            deployer_factory=None,
            runner=None,
            scheduler=None,
            evidence_store=None,
            policy_manifest_digest="sha256:" + ("a" * 64),
            policy=None,
            model_contract={},
            now=lambda: now,
        )
    failure = cleanup[0][0]
    assert set(failure) == {"error_code", "detail_digest", "failed_at"}
    assert failure["error_code"] == "candidate_execution_failed"
    assert failure["failed_at"] == now.isoformat()
    assert failure["detail_digest"].startswith("sha256:")


def test_lifecycle_heartbeat_must_stay_below_reviewed_maximum() -> None:
    with pytest.raises(ContractError, match="below 60"):
        with lifecycle_heartbeat(
            SimpleNamespace(),
            now=lambda: datetime.now(UTC),
            interval_seconds=60,
        ):
            pass
