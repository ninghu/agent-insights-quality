from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

from agent_insights_quality.contracts import ContractError


_AGENT_NAME = re.compile(r"^aiq-[0-9]{3}-[a-z][a-z0-9-]*(?:[-_.][A-Za-z0-9]+)*$")
_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
_CONTEXT_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._()~-]{0,127}$")
_TENANT_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass(frozen=True)
class RuntimeLinkContext:
    """Private runtime values used only while rendering direct links."""

    subscription: str
    resource_group: str
    account: str
    project: str
    tenant_id: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RuntimeLinkContext":
        expected = {
            "subscription",
            "resource_group",
            "account",
            "project",
            "tenant_id",
        }
        if set(value) != expected or not all(
            isinstance(value[key], str) for key in expected
        ):
            raise ContractError(
                "Runtime link context requires exact subscription, resource_group, account, "
                "project, and tenant_id strings"
            )
        return cls(**{key: value[key] for key in expected})

    def resource_route(self) -> str:
        values = (self.subscription, self.resource_group, self.account, self.project)
        if any(not _CONTEXT_COMPONENT.fullmatch(value) for value in values):
            raise ContractError(
                "Runtime link components must be canonical non-empty path segments"
            )
        return (
            "https://ai.azure.com/nextgen/r/"
            f"{self.subscription},{self.resource_group},,{self.account},{self.project}"
        )

    def tenant_query(self) -> str:
        if not _TENANT_ID.fullmatch(self.tenant_id):
            raise ContractError("Runtime tenant_id must be a canonical UUID")
        return urlencode({"tid": self.tenant_id})


def agent_insights_url(
    context: RuntimeLinkContext,
    agent_name: str,
    *,
    standalone_tab: bool,
) -> str:
    suffix = "insights" if standalone_tab else "monitor/insights"
    return f"{_agent_route(context, agent_name)}/{suffix}?{context.tenant_query()}"


def agent_page_url(
    context: RuntimeLinkContext,
    agent_name: str,
) -> str:
    return f"{_agent_route(context, agent_name)}/build?{context.tenant_query()}"


def _agent_route(
    context: RuntimeLinkContext,
    agent_name: str,
) -> str:
    if not _AGENT_NAME.fullmatch(agent_name):
        raise ContractError("Agent name must use a stable aiq-NNN prefix")
    return f"{context.resource_route()}/build/agents/{quote(agent_name, safe='')}"


def project_page_url(context: RuntimeLinkContext) -> str:
    return f"{context.resource_route()}/home?{context.tenant_query()}"


def trace_url(context: RuntimeLinkContext, agent_name: str, operation_id: str) -> str:
    if not _AGENT_NAME.fullmatch(agent_name):
        raise ContractError("Agent name must use a stable aiq-NNN prefix")
    if not _OPERATION_ID.fullmatch(operation_id):
        raise ContractError("Trace links require the correlated 32-character operation_Id")
    return (
        f"{_agent_route(context, agent_name)}/traces/{operation_id}?"
        f"{context.tenant_query()}"
    )


def _validate_runtime_url(
    value: str,
    expected_context: RuntimeLinkContext,
) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "ai.azure.com"
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ContractError("Agent URL does not match the approved runtime route")
    try:
        query = parse_qs(parsed.query, strict_parsing=True)
    except ValueError as error:
        raise ContractError("Agent URL tenant query is invalid") from error
    if query != {"tid": [expected_context.tenant_id]}:
        raise ContractError("Agent URL must contain the authorized tenant ID")


def validate_agent_insights_url(
    value: str,
    expected_context: RuntimeLinkContext,
    expected_agent_name: str,
) -> None:
    _validate_runtime_url(value, expected_context)
    allowed = {
        agent_insights_url(expected_context, expected_agent_name, standalone_tab=True),
        agent_insights_url(expected_context, expected_agent_name, standalone_tab=False),
    }
    if value not in allowed:
        raise ContractError(
            "Agent Insights URL does not match the authorized runtime context and report agent"
        )


def validate_agent_page_url(
    value: str,
    expected_context: RuntimeLinkContext,
    expected_agent_name: str,
) -> None:
    _validate_runtime_url(value, expected_context)
    if value != agent_page_url(expected_context, expected_agent_name):
        raise ContractError(
            "Agent page URL does not match the authorized runtime context and report agent"
        )
