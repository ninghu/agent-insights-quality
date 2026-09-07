"""Low-level Azure adapters. Importing this package does not import any Azure SDK."""

from agent_insights_quality.providers.acr import (
    AcrImageBuilder,
    AzureCommandRunner,
    CommandResult,
    CommandRunner,
)
from agent_insights_quality.providers.artifacts import ImageBuilder
from agent_insights_quality.providers.environment import resolve_environment
from agent_insights_quality.providers.runtime import AzureRuntime
from agent_insights_quality.providers.sol import AzureSol, SolResponseError
from agent_insights_quality.providers.telemetry import AzureLogsReader, LogsReader
from agent_insights_quality.providers.transport import (
    AzureHttpTransport,
    HttpRequest,
    HttpResponse,
    Transport,
)

__all__ = [
    "AcrImageBuilder",
    "AzureCommandRunner",
    "AzureHttpTransport",
    "AzureLogsReader",
    "AzureRuntime",
    "AzureSol",
    "CommandResult",
    "CommandRunner",
    "HttpRequest",
    "HttpResponse",
    "ImageBuilder",
    "LogsReader",
    "SolResponseError",
    "Transport",
    "resolve_environment",
]
