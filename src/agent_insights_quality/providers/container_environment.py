"""Custom-container overrides exclude values reserved for platform injection."""

from collections.abc import Mapping


def container_environment(variables: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value for key, value in variables.items()
        if not key.startswith(("FOUNDRY_", "AGENT_"))
        and key != "APPLICATIONINSIGHTS_CONNECTION_STRING"
    }
