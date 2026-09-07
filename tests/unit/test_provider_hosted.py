from dataclasses import replace

import pytest

from agent_insights_quality.contracts import Environment
from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers.hosted import hosted_definition, hosted_environment


@pytest.fixture
def environment():
    return Environment(
        "staging", "synthetic", "project", "https://example.invalid/api/projects/project",
        "/synthetic/telemetry", "storage", "registry", "swedencentral", "SwedenCentral",
    )


@pytest.mark.parametrize("profile", ["staging", "daily"])
def test_hosted_environment_preserves_the_production_settings(environment, profile):
    environment = replace(environment, profile=profile)
    assert hosted_environment(environment, "InstrumentationKey=synthetic") == {
        "FOUNDRY_PROJECT_ENDPOINT": environment.project_endpoint,
        "APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=synthetic",
        "ENABLE_SENSITIVE_DATA": "true",
    }


@pytest.mark.parametrize("connection", [None, "", 123])
def test_missing_connection_is_not_an_uninstrumented_deployment(environment, connection):
    with pytest.raises(QualityError, match="telemetry_connection_unavailable"):
        hosted_environment(environment, connection)


def test_hosted_definition_retains_defaults_and_does_not_mutate_input(environment):
    variables = {"FOUNDRY_PROJECT_ENDPOINT": "${FOUNDRY_PROJECT_ENDPOINT}", "OTHER": "synthetic"}
    definition = hosted_definition(environment, variables)
    assert definition == {
        "kind": "hosted",
        "protocol_versions": [{"protocol": "responses", "version": "1.0.0"}],
        "cpu": "1", "memory": "2Gi",
        "environment_variables": {
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-5.4-mini",
            "FOUNDRY_PROJECT_ENDPOINT": environment.project_endpoint,
            "OTHER": "synthetic",
        },
    }
    assert variables["FOUNDRY_PROJECT_ENDPOINT"] == "${FOUNDRY_PROJECT_ENDPOINT}"
