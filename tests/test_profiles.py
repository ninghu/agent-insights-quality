from __future__ import annotations

import json
from types import SimpleNamespace

from agent_insights_quality.profiles import TELEMETRY_GENERATION, RuntimeProfile


def test_profile_discovers_fixed_azure_resources(monkeypatch) -> None:
    resources = [
        {
            "type": "Microsoft.CognitiveServices/accounts",
            "kind": "AIServices",
            "name": "synthetic-daily-account",
            "tags": {"profile": "daily"},
        },
        {
            "type": "Microsoft.CognitiveServices/accounts",
            "kind": "AIServices",
            "name": "synthetic-staging-account",
            "tags": {"profile": "staging"},
        },
        {
            "type": "Microsoft.ContainerRegistry/registries",
            "name": "syntheticregistry",
        },
        {
            "type": "Microsoft.Insights/components",
            "name": "daily-insights",
            "id": "/subscriptions/hidden/daily",
            "tags": {"profile": "daily", "generation": TELEMETRY_GENERATION},
        },
        {
            "type": "Microsoft.Insights/components",
            "name": "staging-insights",
            "id": "/subscriptions/hidden/staging",
            "tags": {"profile": "staging", "generation": TELEMETRY_GENERATION},
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
    assert profile.project_endpoint.endswith("/api/projects/agent-insights-quality")
