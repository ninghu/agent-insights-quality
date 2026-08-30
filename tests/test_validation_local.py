from __future__ import annotations

from types import SimpleNamespace

import pytest
from azure.monitor.query import LogsQueryStatus

from agent_insights_quality.live import LiveRuntime
from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_local import discover_local_git_context


def test_local_git_context_is_automatic_and_exact(monkeypatch) -> None:
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
