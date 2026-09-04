from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality.automation_policy import load_automation_policy
from agent_insights_quality.azure import (
    ANALYTICS_DEPLOYMENT_NAME,
    SWEDEN_DEPLOYMENT_NAME,
    _deploy_template,
    deploy_analytics_infrastructure,
    deploy_infrastructure,
)
from agent_insights_quality.util import ContractError


def test_deployment_reads_fixed_telemetry_resource_set(monkeypatch) -> None:
    calls = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        if "signed-in-user" in arguments:
            return SimpleNamespace(returncode=0, stdout="synthetic-principal")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("agent_insights_quality.azure.subprocess.run", run)
    deploy_infrastructure()
    deployment = next(
        item for item in calls if item[1:4] == ["deployment", "sub", "create"]
    )
    policy = load_automation_policy()
    assert deployment[deployment.index("--name") + 1] == SWEDEN_DEPLOYMENT_NAME
    assert deployment[deployment.index("--location") + 1] == "swedencentral"
    assert "telemetryGeneration=g30" in deployment
    assert "location=swedencentral" in deployment
    assert "testAgentModelVersion=2026-03-17" in deployment
    assert "terraModelVersion=2026-07-09" in deployment
    assert "testAgentCapacity=4500" in deployment
    assert "insightGenerationCapacity=100" in deployment
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
    assert not any("approvedRecord" in value for value in deployment)


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
    assert deployment[deployment.index("--name") + 1] == ANALYTICS_DEPLOYMENT_NAME
    assert deployment[deployment.index("--location") + 1] == "westus2"
    assert not any("terraModelVersion" in value for value in deployment)
    assert not any("telemetryGeneration" in value for value in deployment)


@pytest.mark.parametrize(
    ("deployment_name", "deployment_location"),
    [
        (SWEDEN_DEPLOYMENT_NAME, "westus2"),
        (ANALYTICS_DEPLOYMENT_NAME, "swedencentral"),
        ("user-controlled-name", "swedencentral"),
        ("main", "swedencentral"),
    ],
)
def test_deploy_template_rejects_unreviewed_name_location_pairs(
    deployment_name,
    deployment_location,
) -> None:
    with pytest.raises(ContractError, match="name and location"):
        _deploy_template(
            Path("synthetic.bicep"),
            [],
            deployment_name=deployment_name,
            deployment_location=deployment_location,
        )
