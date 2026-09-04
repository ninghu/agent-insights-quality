from __future__ import annotations

import json

import pytest

from agent_insights_quality import cli
from agent_insights_quality.util import ContractError


def test_validation_cli_exposes_stage_primitives_without_generation_inputs() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["run-test-agent-validation"])
    assert vars(args) == {"command": "run-test-agent-validation"}
    choices = parser._subparsers._group_actions[0].choices
    for command in (
        "prepare-test-agent-validation",
        "recover-test-agent-validation",
        "deploy-test-agent-validation-shard",
        "reconcile-test-agent-validation-deployment",
        "invoke-test-agent-validation-shard",
        "prepare-test-agent-validation-assessment",
        "release-test-agent-validation-assessment",
        "import-test-agent-validation-assessment",
        "compose-test-agent-validation",
    ):
        assert command in choices
    assert "cleanup-test-agent-validation" not in choices
    invoke = parser.parse_args(
        ["invoke-test-agent-validation-shard", "--shard-id", "3"]
    )
    assert vars(invoke) == {
        "command": "invoke-test-agent-validation-shard",
        "shard_id": 3,
    }


def test_run_validation_cli_uses_automatic_local_discovery(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "run_test_agent_validation",
        lambda: {
            "status": "ready",
            "result": "PASS",
            "authority_count": 2,
        },
    )
    args = cli.build_parser().parse_args(["run-test-agent-validation"])
    result = json.loads(cli._dispatch(args) or "{}")
    assert result == {
        "status": "ready",
        "result": "PASS",
        "authority_count": 2,
    }


def test_assessment_cli_resolves_only_the_hidden_active_assignment(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "prepare_test_agent_validation_assessment",
        lambda: {"status": "assessment_ready"},
    )
    args = cli.build_parser().parse_args(
        ["prepare-test-agent-validation-assessment"]
    )
    result = json.loads(cli._dispatch(args) or "{}")
    assert vars(args) == {
        "command": "prepare-test-agent-validation-assessment"
    }
    assert result == {"status": "assessment_ready"}

    monkeypatch.setattr(
        cli,
        "import_test_agent_validation_assessment",
        lambda: {"status": "verified", "outcome": "PASS"},
    )
    args = cli.build_parser().parse_args(
        ["import-test-agent-validation-assessment"]
    )
    assert vars(args) == {
        "command": "import-test-agent-validation-assessment"
    }
    assert json.loads(cli._dispatch(args) or "{}") == {
        "status": "verified",
        "outcome": "PASS",
    }

    monkeypatch.setattr(
        cli,
        "release_test_agent_validation_assessment",
        lambda: {"status": "assessment_released"},
    )
    args = cli.build_parser().parse_args(
        ["release-test-agent-validation-assessment"]
    )
    assert vars(args) == {
        "command": "release-test-agent-validation-assessment"
    }
    assert json.loads(cli._dispatch(args) or "{}") == {
        "status": "assessment_released"
    }


def test_recovery_cli_uses_hidden_successor_generation(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "recover_test_agent_validation",
        lambda: {
            "status": "recovery_verification_pending",
            "recovery_authority_count": 1,
        },
    )
    args = cli.build_parser().parse_args(["recover-test-agent-validation"])
    assert vars(args) == {"command": "recover-test-agent-validation"}
    assert json.loads(cli._dispatch(args) or "{}") == {
        "status": "recovery_verification_pending",
        "recovery_authority_count": 1,
    }


def test_daily_provisioning_requires_the_coordinator_lifecycle(
    monkeypatch,
) -> None:
    args = cli.build_parser().parse_args(["provision", "--profile", "daily"])
    with pytest.raises(ContractError, match="daily-prepare then daily-provision"):
        cli._dispatch(args)

    monkeypatch.setattr(
        cli,
        "provision_daily",
        lambda: {"state": "PREPARED", "pending_agent_lanes": []},
    )
    result = json.loads(cli._dispatch(cli.build_parser().parse_args(["daily-provision"])))
    assert result["state"] == "PREPARED"
