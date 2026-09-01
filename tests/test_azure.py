from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality.automation_policy import load_automation_policy
from agent_insights_quality.azure import (
    _lock_approved_validation_policy,
    deploy_analytics_infrastructure,
    deploy_infrastructure,
)
from agent_insights_quality.progress import ProgressReporter
from agent_insights_quality.util import ContractError


def test_deployment_reads_fixed_telemetry_resource_set(
    monkeypatch,
) -> None:
    calls = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        if "signed-in-user" in arguments:
            return SimpleNamespace(returncode=0, stdout="synthetic-principal")
        if arguments[1:4] == ["storage", "account", "list"]:
            return SimpleNamespace(returncode=0, stdout="aiqsweartsynthetic\n")
        if "immutability-policy" in arguments and "show" in arguments:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "state": "Locked",
                        "immutabilityPeriodSinceCreationInDays": 90,
                        "allowProtectedAppendWrites": False,
                    }
                ),
            )
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("agent_insights_quality.azure.subprocess.run", run)
    deploy_infrastructure()
    deployment = next(
        item for item in calls if item[1:4] == ["deployment", "sub", "create"]
    )
    assert any(
        value == "telemetryGeneration=g30"
        for value in deployment
    )
    assert "location=swedencentral" in deployment
    assert "testAgentModelVersion=2026-03-17" in deployment
    assert "terraModelVersion=2026-07-09" in deployment
    assert "testAgentCapacity=4500" in deployment
    assert "insightGenerationCapacity=100" in deployment
    policy = load_automation_policy()
    assert f"storageAccountPrefix={policy.storage_account_prefix}" in deployment
    assert f"storageResourceRole={policy.storage_resource_role}" in deployment
    assert (
        f"qualityArtifactContainerName={policy.quality_artifact_container}"
        in deployment
    )
    assert (
        f"deploymentRegistryContainerName={policy.deployment_registry_container}"
        in deployment
    )
    assert (
        "approvedRecordContainerName="
        + policy.approved_record_container
        in deployment
    )
    storage_lookup = next(
        item for item in calls if item[1:4] == ["storage", "account", "list"]
    )
    query = storage_lookup[storage_lookup.index("--query") + 1]
    for exact_filter in (
        "starts_with(name, 'aiqsweart')",
        "kind=='StorageV2'",
        "location=='swedencentral'",
        "tags.purpose=='agent-insights-quality'",
        "tags.environment=='swedencentral'",
        "tags.location=='swedencentral'",
        "tags.generation=='g30'",
        "tags.resourceRole=='qualification-storage'",
    ):
        assert exact_filter in query
    assert not any("validationPrincipalId=" in value for value in deployment)
    assert not any("validationReceiptPrincipalId=" in value for value in deployment)


def test_infrastructure_locks_unlocked_approved_record_policy(
    monkeypatch,
) -> None:
    calls = []
    policies = iter(
        [
            {
                "state": "Unlocked",
                "etag": "synthetic-etag",
                "immutabilityPeriodSinceCreationInDays": 90,
                "allowProtectedAppendWrites": False,
            },
            {
                "state": "Locked",
                "immutabilityPeriodSinceCreationInDays": 90,
                "allowProtectedAppendWrites": False,
            },
        ]
    )

    def run(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:4] == ["storage", "account", "list"]:
            return SimpleNamespace(returncode=0, stdout="aiqsweartsynthetic\n")
        if "immutability-policy" in arguments and "show" in arguments:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(next(policies)),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr("agent_insights_quality.azure.subprocess.run", run)
    _lock_approved_validation_policy(ProgressReporter("test"))
    lock = next(item for item in calls if "lock" in item)
    assert lock[lock.index("--if-match") + 1] == "synthetic-etag"
    expected_container = load_automation_policy().approved_record_container
    policy_calls = [item for item in calls if "immutability-policy" in item]
    assert [
        next(value for value in item if value in {"show", "lock"})
        for item in policy_calls
    ] == ["show", "lock", "show"]
    assert all(
        item[item.index("--container-name") + 1] == expected_container
        for item in policy_calls
    )
    assert all("--auth-mode" not in item for item in policy_calls)


def test_infrastructure_creates_and_locks_missing_approved_record_policy(
    monkeypatch,
) -> None:
    calls = []
    shows = iter(
        [
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=(
                    "(ResourceNotFound) Operation returned an invalid status 'Not Found'\n"
                    "Code: ResourceNotFound\n"
                    "Message: Operation returned an invalid status 'Not Found'\n"
                ),
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "state": "Unlocked",
                        "etag": "created-etag",
                        "immutabilityPeriodSinceCreationInDays": 90,
                        "allowProtectedAppendWrites": False,
                    }
                ),
                stderr="",
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "state": "Locked",
                        "immutabilityPeriodSinceCreationInDays": 90,
                        "allowProtectedAppendWrites": False,
                    }
                ),
                stderr="",
            ),
        ]
    )

    def run(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:4] == ["storage", "account", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout="aiqsweartsynthetic\n",
                stderr="",
            )
        if "immutability-policy" in arguments and "show" in arguments:
            return next(shows)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr("agent_insights_quality.azure.subprocess.run", run)
    _lock_approved_validation_policy(ProgressReporter("test"))
    policy_calls = [item for item in calls if "immutability-policy" in item]
    assert [
        next(value for value in item if value in {"show", "create", "lock"})
        for item in policy_calls
    ] == ["show", "create", "show", "lock", "show"]
    create = policy_calls[1]
    assert create[create.index("--period") + 1] == "90"
    assert (
        create[create.index("--allow-protected-append-writes") + 1]
        == "false"
    )
    lock = policy_calls[3]
    assert lock[lock.index("--if-match") + 1] == "created-etag"


def test_infrastructure_does_not_mutate_locked_approved_record_policy(
    monkeypatch,
) -> None:
    calls = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:4] == ["storage", "account", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout="aiqsweartsynthetic\n",
                stderr="",
            )
        if "immutability-policy" in arguments and "show" in arguments:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "state": "Locked",
                        "immutabilityPeriodSinceCreationInDays": 90,
                        "allowProtectedAppendWrites": False,
                    }
                ),
                stderr="",
            )
        raise AssertionError(arguments)

    monkeypatch.setattr("agent_insights_quality.azure.subprocess.run", run)
    _lock_approved_validation_policy(ProgressReporter("test"))
    policy_calls = [item for item in calls if "immutability-policy" in item]
    assert len(policy_calls) == 1
    assert "show" in policy_calls[0]


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "message"),
    [
        (
            1,
            "",
            "(AuthorizationFailed) Access denied\nCode: AuthorizationFailed\n",
            "invalid",
        ),
        (
            1,
            "",
            "(ResourceNotFound) Not found without a structured code line\n",
            "invalid",
        ),
        (0, "not-json", "", "invalid"),
    ],
)
def test_policy_read_errors_do_not_create(
    monkeypatch,
    returncode,
    stdout,
    stderr,
    message,
) -> None:
    calls = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:4] == ["storage", "account", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout="aiqsweartsynthetic\n",
                stderr="",
            )
        if "immutability-policy" in arguments and "show" in arguments:
            return SimpleNamespace(
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )
        raise AssertionError(arguments)

    monkeypatch.setattr("agent_insights_quality.azure.subprocess.run", run)
    with pytest.raises(ContractError, match=message):
        _lock_approved_validation_policy(ProgressReporter("test"))
    assert not any("create" in item for item in calls)


def test_ambiguous_storage_discovery_does_not_create_policy(monkeypatch) -> None:
    calls = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        return SimpleNamespace(
            returncode=0,
            stdout="aiqsweartsynthetic\naiqsweartother\n",
            stderr="",
        )

    monkeypatch.setattr("agent_insights_quality.azure.subprocess.run", run)
    with pytest.raises(ContractError, match="missing or ambiguous"):
        _lock_approved_validation_policy(ProgressReporter("test"))
    assert not any("immutability-policy" in item for item in calls)


def test_legacy_storage_discovery_does_not_create_policy(monkeypatch) -> None:
    calls = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        return SimpleNamespace(
            returncode=0,
            stdout="aiqartifactslegacy\n",
            stderr="",
        )

    monkeypatch.setattr("agent_insights_quality.azure.subprocess.run", run)
    with pytest.raises(ContractError, match="missing or ambiguous"):
        _lock_approved_validation_policy(ProgressReporter("test"))
    assert not any("immutability-policy" in item for item in calls)


def test_policy_ensure_is_idempotent_across_repeated_reconciliation(
    monkeypatch,
) -> None:
    calls = []
    shows = iter(
        [
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=(
                    "(ResourceNotFound) Operation returned an invalid status 'Not Found'\n"
                    "Code: ResourceNotFound\n"
                ),
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "state": "Unlocked",
                        "etag": "created-etag",
                        "immutabilityPeriodSinceCreationInDays": 90,
                        "allowProtectedAppendWrites": False,
                    }
                ),
                stderr="",
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "state": "Locked",
                        "immutabilityPeriodSinceCreationInDays": 90,
                        "allowProtectedAppendWrites": False,
                    }
                ),
                stderr="",
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "state": "Locked",
                        "immutabilityPeriodSinceCreationInDays": 90,
                        "allowProtectedAppendWrites": False,
                    }
                ),
                stderr="",
            ),
        ]
    )

    def run(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1:4] == ["storage", "account", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout="aiqsweartsynthetic\n",
                stderr="",
            )
        if "immutability-policy" in arguments and "show" in arguments:
            return next(shows)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr("agent_insights_quality.azure.subprocess.run", run)
    progress = ProgressReporter("test")
    _lock_approved_validation_policy(progress)
    _lock_approved_validation_policy(progress)
    policy_calls = [item for item in calls if "immutability-policy" in item]
    assert [
        next(value for value in item if value in {"show", "create", "lock"})
        for item in policy_calls
    ] == ["show", "create", "show", "lock", "show", "show"]


def test_policy_failure_prevents_infrastructure_completed_status(
    monkeypatch,
) -> None:
    emitted = []

    def run(arguments, **_kwargs):
        if "signed-in-user" in arguments:
            return SimpleNamespace(
                returncode=0,
                stdout="synthetic-principal",
                stderr="",
            )
        if arguments[1:4] == ["storage", "account", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout="aiqsweartsynthetic\n",
                stderr="",
            )
        if "immutability-policy" in arguments and "show" in arguments:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=(
                    "(AuthorizationFailed) Access denied\n"
                    "Code: AuthorizationFailed\n"
                ),
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("agent_insights_quality.azure.subprocess.run", run)
    monkeypatch.setattr(
        "agent_insights_quality.azure.ProgressReporter.emit",
        lambda _self, message: emitted.append(message),
    )
    with pytest.raises(ContractError, match="invalid"):
        deploy_infrastructure()
    assert "full infrastructure reconciliation completed" not in emitted


def test_analytics_deployment_does_not_change_foundry_models(monkeypatch) -> None:
    calls = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        if "signed-in-user" in arguments:
            return SimpleNamespace(returncode=0, stdout="synthetic-principal")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("agent_insights_quality.azure.subprocess.run", run)
    deploy_analytics_infrastructure()
    deployment = calls[-1]
    template = deployment[deployment.index("--template-file") + 1]
    assert Path(template).name == "analytics.bicep"
    assert not any("terraModelVersion" in value for value in deployment)
    assert not any("telemetryGeneration" in value for value in deployment)
