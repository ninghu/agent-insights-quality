from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

from agent_insights_quality.contracts import ContractError


_AGENT_NAME = re.compile(r"^aiq-[0-9]{3}-[a-z][a-z0-9-]*(?:[-_.][A-Za-z0-9]+)*$")
_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class RuntimeLinkContext:
    """Private runtime values used only while rendering direct links."""

    subscription: str
    resource_group: str
    account: str
    project: str

    def resource_route(self) -> str:
        values = (self.subscription, self.resource_group, self.account, self.project)
        if any(not value or "/" in value or "," in value for value in values):
            raise ContractError("Runtime link components must be non-empty and contain no slash or comma")
        return (
            "https://ai.azure.com/nextgen/r/"
            f"{self.subscription},{self.resource_group},,{self.account},{self.project}"
        )


def agent_insights_url(
    context: RuntimeLinkContext,
    agent_name: str,
    *,
    standalone_tab: bool,
) -> str:
    if not _AGENT_NAME.fullmatch(agent_name):
        raise ContractError("Agent name must use a stable aiq-NNN prefix")
    suffix = "insights" if standalone_tab else "monitor/insights"
    return f"{context.resource_route()}/build/agents/{quote(agent_name, safe='')}/{suffix}"


def trace_url(context: RuntimeLinkContext, agent_name: str, operation_id: str) -> str:
    if not _AGENT_NAME.fullmatch(agent_name):
        raise ContractError("Agent name must use a stable aiq-NNN prefix")
    if not _OPERATION_ID.fullmatch(operation_id):
        raise ContractError("Trace links require the correlated 32-character operation_Id")
    return (
        f"{context.resource_route()}/build/agents/{quote(agent_name, safe='')}"
        f"/traces/{operation_id}"
    )


def validate_agent_insights_url(value: str, expected_agent_name: str | None = None) -> None:
    parsed = urlparse(value)
    match = re.fullmatch(
        r"/nextgen/r/[^/,]+,[^/,]+,,[^/,]+,[^/,]+/"
        r"build/agents/(?P<agent>[^/]+)/(?:(?:monitor/)?insights)",
        parsed.path,
    )
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "ai.azure.com"
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        raise ContractError("Agent Insights URL does not match the approved runtime route")
    encoded_agent = match.group("agent")
    decoded_agent = unquote(encoded_agent)
    if (
        not _AGENT_NAME.fullmatch(decoded_agent)
        or quote(decoded_agent, safe="") != encoded_agent
        or (expected_agent_name is not None and decoded_agent != expected_agent_name)
    ):
        raise ContractError(
            "Agent Insights URL agent segment does not match the corresponding report agent"
        )
