"""Hosted deployment settings, separate from CLI orchestration and Prompt assets."""

from collections.abc import Mapping

from agent_insights_quality.contracts import Environment, JsonObject
from agent_insights_quality.errors import QualityError


def hosted_environment(environment: Environment, connection_string: str) -> dict[str, str]:
    if not isinstance(connection_string, str) or not connection_string:
        raise QualityError("telemetry_connection_unavailable")
    return {
        "FOUNDRY_PROJECT_ENDPOINT": environment.project_endpoint,
        "APPLICATIONINSIGHTS_CONNECTION_STRING": connection_string,
        "ENABLE_SENSITIVE_DATA": "true",
    }


def hosted_definition(
    environment: Environment, variables: Mapping[str, str],
) -> JsonObject:
    configured = {"AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-5.4-mini", **variables}
    return {
        "kind": "hosted",
        "protocol_versions": [{"protocol": "responses", "version": "1.0.0"}],
        "cpu": "1",
        "memory": "2Gi",
        "environment_variables": {
            key: environment.project_endpoint if value == "${FOUNDRY_PROJECT_ENDPOINT}" else value
            for key, value in configured.items()
        },
    }
