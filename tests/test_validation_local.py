from __future__ import annotations

from types import SimpleNamespace

import pytest
from azure.monitor.query import LogsQueryStatus

from agent_insights_quality.live import LiveRuntime
from agent_insights_quality.util import ROOT, ContractError
from agent_insights_quality.validation_local import (
    _assert_repository_root,
    _assert_recovery_substrate,
    _run_text,
    _substrate,
    discover_local_git_context,
)


def test_local_git_context_is_automatic_and_exact(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_local._assert_repository_root",
        lambda: None,
    )
    responses = {
        ("git", "status", "--porcelain=v1", "--untracked-files=all"): "",
        ("git", "rev-parse", "HEAD"): "a" * 40,
    }
    monkeypatch.setattr(
        "agent_insights_quality.validation_local._run_text",
        lambda arguments, _label: responses[tuple(arguments)],
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_local._run_json",
        lambda arguments, _label: (
            {"nameWithOwner": "ninghu/agent-insights-quality"}
            if arguments[1:3] == ["repo", "view"]
            else {"number": 63, "headRefOid": "a" * 40, "state": "OPEN"}
        ),
    )
    context = discover_local_git_context()
    assert context.repository == "ninghu/agent-insights-quality"
    assert context.pr_number == 63
    assert context.commit_sha == "a" * 40


def test_local_git_context_rejects_dirty_or_drifted_head(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_local._assert_repository_root",
        lambda: None,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_local._run_text",
        lambda arguments, _label: (
            " M changed.py" if "status" in arguments else "a" * 40
        ),
    )
    with pytest.raises(ContractError, match="clean worktree"):
        discover_local_git_context()

    monkeypatch.setattr(
        "agent_insights_quality.validation_local._run_text",
        lambda arguments, _label: "" if "status" in arguments else "a" * 40,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_local._run_json",
        lambda arguments, _label: (
            {"nameWithOwner": "ninghu/agent-insights-quality"}
            if arguments[1:3] == ["repo", "view"]
            else {"number": 63, "headRefOid": "b" * 40, "state": "OPEN"}
        ),
    )
    with pytest.raises(ContractError, match="exact head"):
        discover_local_git_context()


def test_local_preflight_executes_a_read_only_g29_query() -> None:
    observed = {}

    class Client:
        @staticmethod
        def query_resource(resource_id, query, *, timespan):
            observed.update(
                resource_id=resource_id,
                query=query,
                timespan=timespan,
            )
            return SimpleNamespace(status=LogsQueryStatus.SUCCESS)

    runtime = LiveRuntime(
        SimpleNamespace(
            name="validation-test",
            application_insights_resource_id="synthetic-g29",
        ),
        token_provider=lambda _scope: "synthetic-token",
    )
    runtime._logs_client_instance = Client()
    runtime.assert_telemetry_read_access()
    assert observed["resource_id"] == "synthetic-g29"
    assert observed["query"] == "print readiness=1"


def test_validation_rejects_imported_and_ambient_worktree_mismatch(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_local._run_text",
        lambda _arguments, _label: str(ROOT),
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_local.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=str(ROOT.parent / "different-worktree"),
        ),
    )
    with pytest.raises(ContractError, match="Current worktree"):
        _assert_repository_root()


def test_validation_commands_are_anchored_to_imported_root(monkeypatch) -> None:
    observed = {}

    def run(_arguments, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="ok")

    monkeypatch.setattr(
        "agent_insights_quality.validation_local.subprocess.run",
        run,
    )
    assert _run_text(["git", "status"], "status") == "ok"
    assert observed["cwd"] == ROOT


def test_recovery_substrate_binds_subscription_and_resources() -> None:
    prefix = "/subscriptions/synthetic-subscription/resourceGroups/synthetic"
    value = _substrate(
        SimpleNamespace(
            tenant_id="synthetic-tenant",
            subscription_id="synthetic-subscription",
        ),
        SimpleNamespace(
            account_name="synthetic-account",
            account_resource_id=(
                f"{prefix}/providers/Microsoft.CognitiveServices/accounts/account"
            ),
            container_registry_name="synthetic-registry",
            registry_storage_account_name="synthetic-storage",
            application_insights_resource_id=(
                f"{prefix}/providers/Microsoft.Insights/components/g29"
            ),
        ),
    )
    assert value["subscription_id"] == "synthetic-subscription"
    operator = SimpleNamespace(
        tenant_id="synthetic-tenant",
        subscription_id="synthetic-subscription",
    )
    profile = SimpleNamespace(
        account_name="synthetic-account",
        account_resource_id=value["account_resource_id"],
        container_registry_name="synthetic-registry",
        registry_storage_account_name="synthetic-storage",
        application_insights_resource_id=value["telemetry_resource_id"],
    )
    _assert_recovery_substrate(value, operator, profile)
    changed = dict(value)
    changed["account_name"] = "different-account"
    with pytest.raises(ContractError, match="interrupted validation substrate"):
        _assert_recovery_substrate(changed, operator, profile)
