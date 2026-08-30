from __future__ import annotations

import json
from types import SimpleNamespace

from agent_insights_quality import cli


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
        lambda _profile: SimpleNamespace(
            registry_storage_account_name="synthetic-storage"
        ),
    )
    monkeypatch.setattr(cli, "provision_profile", lambda **_kwargs: {})
    monkeypatch.setattr(
        cli,
        "local_azure_operator",
        lambda: SimpleNamespace(credential="verified-credential"),
    )
    monkeypatch.setattr(
        cli,
        "AzureValidationBlobStore",
        lambda account, *, credential: (account, credential),
    )
    observed = []
    monkeypatch.setattr(
        cli,
        "fetch_approved_record_for_checkout",
        lambda store, **kwargs: observed.append((store, kwargs)),
    )
    args = cli.build_parser().parse_args(["provision", "--profile", "daily"])
    monkeypatch.setenv("AIQ_STAGING_PROMOTION_RECEIPT", "legacy.json")
    cli._dispatch(args)
    assert observed == [
        (
            ("synthetic-storage", "verified-credential"),
            {"expected_repository": "ninghu/agent-insights-quality"},
        )
    ]
