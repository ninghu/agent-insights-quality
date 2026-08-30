from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_insights_quality.azure import (
    deploy_analytics_infrastructure,
    deploy_infrastructure,
    resolve_latest_model_version,
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


def test_latest_test_agent_model_version_is_selected(monkeypatch) -> None:
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
                            {"name": "OpenAI.gpt-5.4-mini.2026-02-01"},
                            {"name": "OpenAI.gpt-5.4-mini.2026-03-17"},
                            {"name": "OpenAI.gpt-5.6-terra.2026-07-09"},
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
    assert resolve_latest_model_version("gpt-5.4-mini") == "2026-03-17"


def test_deployment_reads_fixed_telemetry_resource_set(
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
    monkeypatch.setattr(
        "agent_insights_quality.azure.resolve_latest_model_version",
        lambda model: "2026-03-17" if model == "gpt-5.4-mini" else "unexpected",
    )
    monkeypatch.setattr("agent_insights_quality.azure.subprocess.run", run)
    deploy_infrastructure(
        {
            "AIQ_VALIDATION_PRINCIPAL_ID": "synthetic-validation-principal",
            "AIQ_VALIDATION_RECEIPT_PRINCIPAL_ID": (
                "synthetic-receipt-principal"
            ),
        }
    )
    deployment = calls[-1]
    assert any(
        value == "telemetryGeneration=g29"
        for value in deployment
    )
    assert "testAgentModelVersion=2026-03-17" in deployment
    assert (
        "validationPrincipalId=synthetic-validation-principal" in deployment
    )
    assert (
        "validationReceiptPrincipalId=synthetic-receipt-principal"
        in deployment
    )


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
