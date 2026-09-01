from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from azure.monitor.query import LogsQueryStatus

from agent_insights_quality.live import LiveRuntime
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.util import ROOT, ContractError, content_hash
from agent_insights_quality.validation_local import (
    LocalGitContext,
    _assert_repository_root,
    _assert_recovery_substrate,
    _deployment_resume_allowed,
    _persist_durations,
    _profile_for_substrate,
    _run_text,
    _substrate,
    discover_local_git_context,
)
from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.validation_cycle import initial_lifecycle
from agent_insights_quality.validation_manifest import prepare_validation_plan
from agent_insights_quality.validation_policy import load_validation_policy


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
                f"{prefix}/providers/Microsoft.Insights/components/g30"
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


def test_cleanup_profile_uses_persisted_substrate_after_drift() -> None:
    base = RuntimeProfile(
        name="staging",
        project_name="current",
        project_endpoint="https://current.invalid",
        insights_endpoint="https://current.invalid",
        application_insights_resource_id="/subscriptions/current/insights",
        registry_path=ROOT / "current.json",
        account_name="current-account",
        container_registry_name="current-registry",
        registry_storage_account_name="current-storage",
        account_resource_id="/subscriptions/current/account",
        telemetry_resource_set="g30",
    )
    persisted = _profile_for_substrate(
        base,
        {
            "account_name": "persisted-account",
            "account_resource_id": "/subscriptions/persisted/account",
            "registry_name": "persisted-registry",
            "storage_account_name": "persisted-storage",
            "telemetry_resource_id": "/subscriptions/persisted/insights",
        },
    )
    assert persisted.account_name == "persisted-account"
    assert persisted.account_resource_id == "/subscriptions/persisted/account"
    assert persisted.container_registry_name == "persisted-registry"
    assert (
        persisted.application_insights_resource_id
        == "/subscriptions/persisted/insights"
    )


def test_partial_deployment_resume_requires_exact_unchanged_cycle() -> None:
    agents, issues = load_catalogs()
    policy = load_validation_policy()
    plan = prepare_validation_plan(
        agents=agents,
        issues=issues,
        policy=policy,
        repository=policy.repository,
        pr_number=63,
        commit_sha="a" * 40,
        local_run_id="synthetic-resume",
    )
    operator = SimpleNamespace(
        tenant_id="synthetic-tenant",
        subscription_id="synthetic-subscription",
        operator_reference=content_hash("synthetic-operator"),
    )
    prefix = "/subscriptions/synthetic-subscription/resourceGroups/synthetic"
    profile = SimpleNamespace(
        account_name="synthetic-account",
        project_endpoint="https://example.invalid/staging",
        account_resource_id=(
            f"{prefix}/providers/Microsoft.CognitiveServices/accounts/account"
        ),
        container_registry_name="synthetic-registry",
        registry_storage_account_name="synthetic-storage",
        application_insights_resource_id=(
            f"{prefix}/providers/Microsoft.Insights/components/g30"
        ),
    )
    started = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    active = initial_lifecycle(
        plan,
        policy=policy,
        ownership_nonce="synthetic-nonce",
        holder_session_reference=content_hash("session"),
        holder_operator_reference=operator.operator_reference,
        holder_run_reference=content_hash("run"),
        substrate=_substrate(operator, profile),
        now=started,
    )
    active["state"] = "CREATING"
    active["capacity"] = {"plan_digest": content_hash("capacity")}
    active["project"].update(
        {
            "state": "bound",
            "provider_id": (
                f"{profile.account_resource_id}/projects/"
                "aiq-staging-swedencentral"
            ),
            "project_principal_id": "synthetic-project-principal",
            "endpoint_reference": content_hash(profile.project_endpoint),
            "bound_observed_at": started.isoformat(),
        }
    )
    active["deployment"]["support_images"] = [
        {
            "logical_version": (
                "v0" if index == 0 else f"issue-{index + 28:03d}"
            ),
            "image": (
                "synthetic.azurecr.io/agent-insights-quality-support@"
                f"sha256:{index:064x}"
            ),
        }
        for index in range(9)
    ]
    git = LocalGitContext(policy.repository, 63, "a" * 40)
    arguments = {
        "git": git,
        "plan": plan,
        "operator": operator,
        "base_profile": profile,
        "now": started + timedelta(hours=1),
    }
    assert _deployment_resume_allowed(active, **arguments) is True

    changed = deepcopy(active)
    changed["commit_sha"] = "b" * 40
    assert _deployment_resume_allowed(changed, **arguments) is False
    changed = deepcopy(active)
    changed["operator"]["operator_reference"] = content_hash("other")
    assert _deployment_resume_allowed(changed, **arguments) is False
    changed = deepcopy(active)
    changed["digests"]["runtime_topology_digest"] = content_hash("other")
    assert _deployment_resume_allowed(changed, **arguments) is False
    changed = deepcopy(active)
    changed["deployment"]["traffic_started"] = True
    assert _deployment_resume_allowed(changed, **arguments) is False
    assert (
        _deployment_resume_allowed(
            active,
            **{
                **arguments,
                "now": started + timedelta(hours=73),
            },
        )
        is False
    )


def test_stage_timings_are_persisted_and_replaceable(tmp_path) -> None:
    path = tmp_path / "durations.json"
    stages = {
        "lock_preflight_seconds": 1.0,
        "project_connections_seconds": 2.0,
    }
    _persist_durations(
        path,
        cycle_id="validation-0123456789ab",
        durations=stages,
    )
    first = json.loads(path.read_text(encoding="utf-8"))
    stages["project_connections_seconds"] = 3.0
    _persist_durations(
        path,
        cycle_id="validation-0123456789ab",
        durations=stages,
    )
    second = json.loads(path.read_text(encoding="utf-8"))
    assert first["stages"]["project_connections_seconds"] == 2.0
    assert second["stages"]["project_connections_seconds"] == 3.0
