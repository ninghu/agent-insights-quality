from __future__ import annotations

import json
from types import SimpleNamespace

from agent_insights_quality import cli


def test_validation_cli_exposes_stage_primitives_without_generation_inputs() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["run-test-agent-validation"])
    assert vars(args) == {"command": "run-test-agent-validation"}
    choices = parser._subparsers._group_actions[0].choices
    for command in (
        "prepare-test-agent-validation",
        "deploy-test-agent-validation-shard",
        "reconcile-test-agent-validation-deployment",
        "invoke-test-agent-validation-shard",
        "prepare-test-agent-validation-assessment",
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
            "authority_count": 41,
        },
    )
    args = cli.build_parser().parse_args(["run-test-agent-validation"])
    result = json.loads(cli._dispatch(args) or "{}")
    assert result == {
        "status": "ready",
        "result": "PASS",
        "authority_count": 41,
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


def test_approval_cli_uses_latest_ready_result_without_manual_paths(
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
