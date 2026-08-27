from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_insights_quality.automation_policy import FIXED_TELEMETRY_RESOURCE_SET
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.util import ContractError


def test_profile_discovers_fixed_azure_resources(monkeypatch) -> None:
    resources = [
        {
            "type": "Microsoft.CognitiveServices/accounts",
            "kind": "AIServices",
            "name": "synthetic-daily-account",
            "id": "/subscriptions/hidden/daily-account",
            "tags": {"profile": "daily"},
        },
        {
            "type": "Microsoft.CognitiveServices/accounts",
            "kind": "AIServices",
            "name": "synthetic-staging-account",
            "id": "/subscriptions/hidden/staging-account",
            "tags": {"profile": "staging"},
        },
        {
            "type": "Microsoft.ContainerRegistry/registries",
            "name": "syntheticregistry",
        },
        {
            "type": "Microsoft.Storage/storageAccounts",
            "name": "syntheticstorage",
            "tags": {"purpose": "agent-insights-quality"},
        },
        {
            "type": "Microsoft.Insights/components",
            "name": "daily-insights",
            "id": "/subscriptions/hidden/daily",
            "tags": {
                "profile": "daily",
                "generation": FIXED_TELEMETRY_RESOURCE_SET,
            },
        },
        {
            "type": "Microsoft.Insights/components",
            "name": "staging-insights",
            "id": "/subscriptions/hidden/staging",
            "tags": {
                "profile": "staging",
                "generation": FIXED_TELEMETRY_RESOURCE_SET,
            },
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
    assert profile.project_name == "agent-insights-quality"
    assert profile.account_name == "synthetic-daily-account"
    assert profile.container_registry_name == "syntheticregistry"
    assert profile.registry_storage_account_name == "syntheticstorage"
    assert profile.project_endpoint.endswith("/api/projects/agent-insights-quality")
    assert profile.telemetry_resource_set == FIXED_TELEMETRY_RESOURCE_SET


def test_profile_requires_matching_project_telemetry_connection(monkeypatch) -> None:
    profile = RuntimeProfile(
        name="daily",
        project_name="agent-insights-quality",
        project_endpoint="https://example.invalid",
        insights_endpoint="https://example.invalid",
        application_insights_resource_id="/subscriptions/hidden/active",
        registry_path=SimpleNamespace(),
        account_resource_id="/subscriptions/hidden/account",
    )
    monkeypatch.setattr(
        "agent_insights_quality.profiles.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"properties": {"target": "/subscriptions/hidden/active"}}
            ),
        ),
    )
    profile.assert_insights_connection()


def test_profile_rejects_mismatched_project_telemetry_connection(
    monkeypatch,
) -> None:
    profile = RuntimeProfile(
        name="daily",
        project_name="agent-insights-quality",
        project_endpoint="https://example.invalid",
        insights_endpoint="https://example.invalid",
        application_insights_resource_id="/subscriptions/hidden/active",
        registry_path=SimpleNamespace(),
        account_resource_id="/subscriptions/hidden/account",
    )
    monkeypatch.setattr(
        "agent_insights_quality.profiles.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"properties": {"target": "/subscriptions/hidden/wrong"}}
            ),
        ),
    )
    with pytest.raises(ContractError, match="does not match"):
        profile.assert_insights_connection()
