from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from agent_insights_quality.automation_policy import FIXED_TELEMETRY_RESOURCE_SET
from agent_insights_quality.profiles import RuntimeProfile, _run_azure_read
from agent_insights_quality.util import ContractError


_ACTIVE_INSIGHTS_ID = "/subscriptions/hidden/active"
_ACCOUNT_ID = "/subscriptions/hidden/account"


def _profile(name: str = "daily") -> RuntimeProfile:
    return RuntimeProfile(
        name=name,
        project_name=f"synthetic-{name}-project",
        project_endpoint=f"https://example.invalid/{name}",
        insights_endpoint=f"https://example.invalid/{name}",
        application_insights_resource_id=_ACTIVE_INSIGHTS_ID,
        registry_path=SimpleNamespace(),
        account_resource_id=_ACCOUNT_ID,
    )


def _arm_connection(
    *,
    target: str = _ACTIVE_INSIGHTS_ID,
) -> dict:
    return {
        "properties": {
            "target": target,
            "category": "AppInsights",
            "authType": "ApiKey",
            "metadata": {
                "ApiType": "Azure",
                "ResourceId": target,
            },
        }
    }


def _mock_connection_reads(
    monkeypatch,
    *,
    profile: RuntimeProfile,
    project_connection_name: str,
    project_connection: dict | None = None,
) -> list[list[str]]:
    observed: list[list[str]] = []
    project_id = (
        f"{profile.account_resource_id}/projects/{profile.project_name}/connections/"
        f"{project_connection_name}"
    )
    project_value = project_connection or _arm_connection()

    def run(arguments, **_kwargs):
        observed.append(arguments)
        url = arguments[arguments.index("--url") + 1]
        if project_id not in url:
            raise AssertionError(f"Unexpected ARM connection URL: {url}")
        return SimpleNamespace(returncode=0, stdout=json.dumps(project_value))

    monkeypatch.setattr(
        "agent_insights_quality.profiles.subprocess.run",
        run,
    )
    return observed


def test_profile_selects_sweden_storage_amid_legacy_unversioned_storage(
    monkeypatch,
) -> None:
    resources = [
        {
            "type": "Microsoft.CognitiveServices/accounts",
            "kind": "AIServices",
            "name": "aiq-daily-swedencentral",
            "id": "/subscriptions/hidden/daily-account",
            "location": "swedencentral",
            "tags": {
                "profile": "daily",
                "environment": "swedencentral",
                "location": "swedencentral",
                "generation": "g30",
            },
        },
        {
            "type": "Microsoft.CognitiveServices/accounts",
            "kind": "AIServices",
            "name": "aiq-staging-swedencentral",
            "id": "/subscriptions/hidden/staging-account",
            "location": "swedencentral",
            "tags": {
                "profile": "staging",
                "environment": "swedencentral",
                "location": "swedencentral",
                "generation": "g30",
            },
        },
        {
            "type": "Microsoft.CognitiveServices/accounts",
            "kind": "AIServices",
            "name": "agent-insights-quality",
            "id": "/subscriptions/hidden/legacy-daily-account",
            "location": "westus2",
            "tags": {"profile": "daily", "generation": "g29"},
        },
        {
            "type": "Microsoft.ContainerRegistry/registries",
            "name": "syntheticregistry",
        },
        {
            "type": "Microsoft.Storage/storageAccounts",
            "kind": "StorageV2",
            "name": "aiqartifactslegacy",
            "location": "westus2",
            "tags": {"purpose": "agent-insights-quality"},
            "properties": {"blobVersioningEnabled": False},
        },
        {
            "type": "Microsoft.Storage/storageAccounts",
            "kind": "StorageV2",
            "name": "aiqsweartsynthetic",
            "location": "swedencentral",
            "tags": {
                "purpose": "agent-insights-quality",
                "environment": "swedencentral",
                "location": "swedencentral",
                "generation": "g30",
                "resourceRole": "qualification-storage",
            },
        },
        {
            "type": "Microsoft.Insights/components",
            "name": "daily-insights",
            "id": "/subscriptions/hidden/daily",
            "location": "swedencentral",
            "tags": {
                "profile": "daily",
                "environment": "swedencentral",
                "location": "swedencentral",
                "generation": FIXED_TELEMETRY_RESOURCE_SET,
            },
        },
        {
            "type": "Microsoft.Insights/components",
            "name": "staging-insights",
            "id": "/subscriptions/hidden/staging",
            "location": "swedencentral",
            "tags": {
                "profile": "staging",
                "environment": "swedencentral",
                "location": "swedencentral",
                "generation": FIXED_TELEMETRY_RESOURCE_SET,
            },
        },
        {
            "type": "Microsoft.Insights/components",
            "name": "legacy-daily-insights",
            "id": "/subscriptions/hidden/legacy-daily",
            "location": "westus2",
            "tags": {"profile": "daily", "generation": "g29"},
        },
    ]
    monkeypatch.setattr(
        "agent_insights_quality.profiles.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(resources),
        ),
    )
    profile = RuntimeProfile.from_env("daily")
    assert profile.project_name == "aiq-daily-swedencentral"
    assert profile.account_name == "aiq-daily-swedencentral"
    assert profile.container_registry_name == "syntheticregistry"
    assert profile.registry_storage_account_name == "aiqsweartsynthetic"
    assert profile.project_endpoint.endswith(
        "/api/projects/aiq-daily-swedencentral"
    )
    assert profile.telemetry_resource_set == FIXED_TELEMETRY_RESOURCE_SET


def test_profile_rejects_ambiguous_sweden_storage(monkeypatch) -> None:
    tags = {
        "purpose": "agent-insights-quality",
        "environment": "swedencentral",
        "location": "swedencentral",
        "generation": "g30",
        "resourceRole": "qualification-storage",
    }
    resources = [
        {
            "type": "Microsoft.CognitiveServices/accounts",
            "kind": "AIServices",
            "name": "aiq-daily-swedencentral",
            "id": "/subscriptions/hidden/daily-account",
            "location": "swedencentral",
            "tags": {
                "profile": "daily",
                "environment": "swedencentral",
                "location": "swedencentral",
                "generation": "g30",
            },
        },
        {
            "type": "Microsoft.ContainerRegistry/registries",
            "name": "syntheticregistry",
        },
        *[
            {
                "type": "Microsoft.Storage/storageAccounts",
                "kind": "StorageV2",
                "name": f"aiqsweartsynthetic{suffix}",
                "location": "swedencentral",
                "tags": tags,
            }
            for suffix in ("a", "b")
        ],
        {
            "type": "Microsoft.Insights/components",
            "id": "/subscriptions/hidden/daily-insights",
            "location": "swedencentral",
            "tags": {
                "profile": "daily",
                "environment": "swedencentral",
                "location": "swedencentral",
                "generation": "g30",
            },
        },
    ]
    monkeypatch.setattr(
        "agent_insights_quality.profiles._azure_resources",
        lambda: resources,
    )
    with pytest.raises(ContractError, match="could not be resolved uniquely"):
        RuntimeProfile.from_env("daily")


def test_profile_requires_official_project_connection(
    monkeypatch,
) -> None:
    profile = _profile()
    observed = _mock_connection_reads(
        monkeypatch,
        profile=profile,
        project_connection_name="application-insights-daily",
    )
    profile.assert_insights_connection()
    assert len(observed) == 1
    assert "/projects/" in " ".join(observed[0])
    assert "--resource" not in observed[0]


@pytest.mark.parametrize("profile_name", ["daily", "staging"])
def test_profile_preflight_uses_deterministic_connection_names(
    monkeypatch,
    profile_name,
) -> None:
    profile = _profile(profile_name)
    connection_name = f"application-insights-{profile_name}"
    observed = _mock_connection_reads(
        monkeypatch,
        profile=profile,
        project_connection_name=connection_name,
    )
    profile.assert_insights_connection()
    assert connection_name in " ".join(observed[0])


@pytest.mark.parametrize("defect", ["missing", "wrong-shape", "wrong-target"])
def test_profile_rejects_invalid_project_connection(
    monkeypatch,
    defect,
) -> None:
    profile = _profile()
    project_connection = _arm_connection()
    if defect == "wrong-shape":
        invalid = _arm_connection()
        invalid["properties"]["category"] = "CustomKeys"
    elif defect == "wrong-target":
        invalid = _arm_connection(target="/subscriptions/hidden/wrong")
    else:
        invalid = _arm_connection()
    project_connection = invalid
    _mock_connection_reads(
        monkeypatch,
        profile=profile,
        project_connection_name="application-insights-daily",
        project_connection=project_connection,
    )
    if defect == "missing":
        original_run = subprocess.run

        def missing(arguments, **kwargs):
            if arguments[1:3] == ["rest", "--method"]:
                return SimpleNamespace(returncode=3, stdout="")
            return original_run(arguments, **kwargs)

        monkeypatch.setattr(
            "agent_insights_quality.profiles.subprocess.run",
            missing,
        )
    with pytest.raises(ContractError, match="Project"):
        profile.assert_insights_connection()


def test_validation_preflight_requires_durable_project_connection(
    monkeypatch,
) -> None:
    profile = _profile("staging")
    project_id = (
        f"{_ACCOUNT_ID}/projects/{profile.project_name}/connections/"
        "application-insights-staging"
    )
    observed = _mock_connection_reads(
        monkeypatch,
        profile=profile,
        project_connection_name="application-insights-staging",
    )
    profile.assert_insights_connection("application-insights-staging")
    assert project_id in " ".join(observed[0])


def test_profile_requires_reviewed_test_agent_model(monkeypatch) -> None:
    profile = RuntimeProfile(
        name="staging",
        project_name="aiq-staging-swedencentral",
        project_endpoint="https://example.invalid",
        insights_endpoint="https://example.invalid",
        application_insights_resource_id="/subscriptions/hidden/active",
        registry_path=SimpleNamespace(),
        account_name="aiq-staging-swedencentral",
    )
    monkeypatch.setattr(
        "agent_insights_quality.profiles.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "name": "gpt-5.4-mini",
                    "properties": {
                        "provisioningState": "Succeeded",
                        "model": {
                            "name": "gpt-5.4-mini",
                            "version": "2026-03-17",
                        },
                    },
                    "sku": {
                        "name": "DataZoneStandard",
                        "capacity": 4500,
                    },
                }
            ),
        ),
    )
    profile.assert_test_agent_model(
        {
            "deployment_name": "gpt-5.4-mini",
            "model_id": "gpt-5.4-mini",
            "model_version": "2026-03-17",
        }
    )


def test_azure_resource_reads_retry_transient_failures(monkeypatch) -> None:
    attempts = 0

    def run(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise subprocess.TimeoutExpired("az", 120)
        return SimpleNamespace(
            returncode=0,
            stdout="[]",
        )

    monkeypatch.setattr("agent_insights_quality.profiles.subprocess.run", run)
    monkeypatch.setattr("agent_insights_quality.profiles.time.sleep", lambda _: None)
    assert _run_azure_read(["az"]).returncode == 0
    assert attempts == 2


def test_profile_region_comes_from_live_project_and_azure_metadata(
    monkeypatch,
) -> None:
    profile = RuntimeProfile(
        name="daily",
        project_name="aiq-daily-swedencentral",
        project_endpoint="https://example.invalid",
        insights_endpoint="https://example.invalid",
        application_insights_resource_id="/subscriptions/hidden/active",
        registry_path=SimpleNamespace(),
        account_resource_id="/subscriptions/hidden/account",
    )
    responses = iter(
        [
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"location": "swedencentral"}),
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {"name": "eastus", "displayName": "East US"},
                        {
                            "name": "swedencentral",
                            "displayName": "Sweden Central",
                        },
                    ]
                ),
            ),
        ]
    )
    calls = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        return next(responses)

    monkeypatch.setattr("agent_insights_quality.profiles.subprocess.run", run)
    assert profile.resolve_test_region() == "SwedenCentral"
    assert calls[0][1:4] == ["rest", "--method", "get"]
    assert "/projects/aiq-daily-swedencentral?" in calls[0][
        calls[0].index("--url") + 1
    ]
    assert calls[1][1:3] == ["account", "list-locations"]


def test_profile_region_has_no_registry_or_config_fallback(monkeypatch) -> None:
    profile = RuntimeProfile(
        name="daily",
        project_name="aiq-daily-swedencentral",
        project_endpoint="https://example.invalid",
        insights_endpoint="https://example.invalid",
        application_insights_resource_id="/subscriptions/hidden/active",
        registry_path=SimpleNamespace(),
        account_resource_id="/subscriptions/hidden/account",
    )
    monkeypatch.setattr(
        "agent_insights_quality.profiles.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({}),
        ),
    )
    with pytest.raises(ContractError, match="location is missing"):
        profile.resolve_test_region()


def test_profile_region_fails_when_metadata_cannot_resolve(monkeypatch) -> None:
    profile = RuntimeProfile(
        name="daily",
        project_name="aiq-daily-swedencentral",
        project_endpoint="https://example.invalid",
        insights_endpoint="https://example.invalid",
        application_insights_resource_id="/subscriptions/hidden/active",
        registry_path=SimpleNamespace(),
        account_resource_id="/subscriptions/hidden/account",
    )
    responses = iter(
        [
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"location": "swedencentral"}),
            ),
            SimpleNamespace(returncode=0, stdout="[]"),
        ]
    )
    monkeypatch.setattr(
        "agent_insights_quality.profiles.subprocess.run",
        lambda *_args, **_kwargs: next(responses),
    )
    with pytest.raises(ContractError, match="did not resolve uniquely"):
        profile.resolve_test_region()
