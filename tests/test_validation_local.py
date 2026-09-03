from __future__ import annotations

from types import SimpleNamespace

import pytest
from azure.monitor.query import LogsQueryStatus

from agent_insights_quality.live import LiveRuntime
from agent_insights_quality.util import ROOT, ContractError
from agent_insights_quality.validation_local import (
    _assert_repository_root,
    _run_text,
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
        lambda _arguments, _label: {
            "nameWithOwner": "ninghu/agent-insights-quality"
        },
    )
    observed = []

    def pulls(arguments, _label):
        observed.append(arguments)
        return [
            {
                "number": 63,
                "state": "open",
                "head": {"sha": "a" * 40},
            }
        ]

    monkeypatch.setattr(
        "agent_insights_quality.validation_local._run_json_array",
        pulls,
    )
    context = discover_local_git_context()
    assert context.repository == "ninghu/agent-insights-quality"
    assert context.pr_number == 63
    assert context.commit_sha == "a" * 40
    assert observed == [
        [
            "gh",
            "api",
            "--method",
            "GET",
            (
                "repos/ninghu/agent-insights-quality/commits/"
                + "a" * 40
                + "/pulls?per_page=100&page=1"
            ),
            "--header",
            "Accept: application/vnd.github+json",
            "--header",
            "X-GitHub-Api-Version: 2022-11-28",
        ]
    ]


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
        lambda _arguments, _label: {
            "nameWithOwner": "ninghu/agent-insights-quality"
        },
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_local._run_json_array",
        lambda _arguments, _label: [
            {
                "number": 63,
                "state": "open",
                "head": {"sha": "b" * 40},
            }
        ],
    )
    with pytest.raises(ContractError, match="exact head"):
        discover_local_git_context()


@pytest.mark.parametrize(
    ("pulls", "message"),
    [
        ([], "exact head"),
        (
            [
                {
                    "number": 63,
                    "state": "closed",
                    "head": {"sha": "a" * 40},
                }
            ],
            "exact head",
        ),
        (
            [
                {
                    "number": 63,
                    "state": "open",
                    "head": {"sha": "a" * 40},
                },
                {
                    "number": 64,
                    "state": "open",
                    "head": {"sha": "a" * 40},
                },
            ],
            "exactly one",
        ),
    ],
)
def test_local_git_context_rejects_zero_or_multiple_exact_open_pulls(
    monkeypatch,
    pulls,
    message,
) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_local._assert_repository_root",
        lambda: None,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_local._run_text",
        lambda arguments, _label: "" if "status" in arguments else "a" * 40,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_local._run_json",
        lambda _arguments, _label: {
            "nameWithOwner": "ninghu/agent-insights-quality"
        },
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_local._run_json_array",
        lambda _arguments, _label: pulls,
    )
    with pytest.raises(ContractError, match=message):
        discover_local_git_context()


def test_local_git_context_rejects_malformed_pull_response(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_local._assert_repository_root",
        lambda: None,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_local._run_text",
        lambda arguments, _label: "" if "status" in arguments else "a" * 40,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_local._run_json",
        lambda _arguments, _label: {
            "nameWithOwner": "ninghu/agent-insights-quality"
        },
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_local._run_json_array",
        lambda _arguments, _label: [
            {"number": "63", "state": "open", "head": {"sha": "a" * 40}}
        ],
    )
    with pytest.raises(ContractError, match="response is invalid"):
        discover_local_git_context()


def test_local_preflight_executes_a_read_only_g30_query() -> None:
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
            application_insights_resource_id="synthetic-g30",
        ),
        token_provider=lambda _scope: "synthetic-token",
    )
    runtime._logs_client_instance = Client()
    runtime.assert_telemetry_read_access()
    assert observed["resource_id"] == "synthetic-g30"
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
