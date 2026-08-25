from __future__ import annotations

import json
from types import SimpleNamespace

from agent_insights_quality.azure import (
    deploy_infrastructure,
    resolve_latest_terra_version,
)


def test_latest_terra_version_is_selected(monkeypatch) -> None:
    responses = iter(
        [
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"id": "hidden-subscription"}),
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "value": [
                            {"name": "OpenAI.gpt-5.6-terra.2026-06-26"},
                            {"name": "OpenAI.gpt-5.6-terra.2026-07-09"},
                            {"name": "OpenAI.gpt-5.6-sol.2026-08-01"},
                        ]
                    }
                ),
            ),
        ]
    )
    monkeypatch.setattr(
        "agent_insights_quality.azure.subprocess.run",
        lambda *args, **kwargs: next(responses),
    )
    assert resolve_latest_terra_version() == "2026-07-09"


def test_deployment_reads_telemetry_generation_from_automation_policy(
    monkeypatch,
) -> None:
    calls = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        if "signed-in-user" in arguments:
            return SimpleNamespace(returncode=0, stdout="synthetic-principal")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(
        "agent_insights_quality.azure.resolve_latest_terra_version",
        lambda: "2026-07-09",
    )
    monkeypatch.setattr("agent_insights_quality.azure.subprocess.run", run)
    deploy_infrastructure()
    deployment = calls[-1]
    assert any(
        value.startswith("telemetryGeneration=g")
        for value in deployment
    )
