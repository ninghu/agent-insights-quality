from __future__ import annotations

import json

import pytest

from agent_insights_quality import cli
from agent_insights_quality.util import ContractError


def test_validation_cli_exposes_only_two_automatic_user_commands() -> None:
    parser = cli.build_parser()
    run = parser.parse_args(["run-test-agent-validation"])
    approve = parser.parse_args(["approve-test-agent-validation"])
    assert vars(run) == {"command": "run-test-agent-validation"}
    assert vars(approve) == {"command": "approve-test-agent-validation"}


def test_run_validation_cli_uses_automatic_local_discovery(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "run_test_agent_validation",
        lambda: {
            "status": "clean",
            "commit_sha": "a" * 40,
            "authority_count": 41,
        },
    )
    args = cli.build_parser().parse_args(["run-test-agent-validation"])
    result = json.loads(cli._dispatch(args) or "{}")
    assert result == {
        "status": "clean",
        "commit_sha": "a" * 40,
        "authority_count": 41,
    }


def test_approval_cli_uses_latest_clean_result_without_manual_paths(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "approve_test_agent_validation",
        lambda: {
            "status": "approved",
            "repository": "ninghu/agent-insights-quality",
            "pr_number": 63,
            "commit_sha": "a" * 40,
            "record_digest": "sha256:" + ("b" * 64),
        },
    )
    args = cli.build_parser().parse_args(["approve-test-agent-validation"])
    result = json.loads(cli._dispatch(args) or "{}")
    assert result["status"] == "approved"
    assert result["pr_number"] == 63


def test_daily_provisioning_is_new_only_for_approved_record(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli.RuntimeProfile,
        "from_env",
        lambda _profile: object(),
    )
    monkeypatch.setattr(cli, "provision_profile", lambda **_kwargs: {})
    observed = []
    monkeypatch.setattr(
        cli,
        "validate_approved_record_for_checkout",
        lambda path, **kwargs: observed.append((path, kwargs)),
    )
    args = cli.build_parser().parse_args(["provision", "--profile", "daily"])
    monkeypatch.setenv("AIQ_STAGING_PROMOTION_RECEIPT", "legacy.json")
    with pytest.raises(ContractError, match="approved Test Agent Validation"):
        cli._dispatch(args)
    monkeypatch.setenv("AIQ_APPROVED_VALIDATION_RECORD", "approved.json")
    cli._dispatch(args)
    assert str(observed[0][0]) == "approved.json"
