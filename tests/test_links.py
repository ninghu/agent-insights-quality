from __future__ import annotations

import pytest

from agent_insights_quality.contracts import ContractError
from agent_insights_quality.links import (
    RuntimeLinkContext,
    agent_insights_url,
    trace_url,
    validate_agent_insights_url,
)


CONTEXT = RuntimeLinkContext(
    subscription="runtime-sub",
    resource_group="runtime-rg",
    account="runtime-account",
    project="aiq-20260820",
)


def test_agent_insights_link_contract() -> None:
    prefix = (
        "https://ai.azure.com/nextgen/r/"
        "runtime-sub,runtime-rg,,runtime-account,aiq-20260820/"
        "build/agents/aiq-001-weather-v1"
    )
    assert agent_insights_url(
        CONTEXT,
        "aiq-001-weather-v1",
        standalone_tab=True,
    ) == f"{prefix}/insights"
    assert agent_insights_url(
        CONTEXT,
        "aiq-001-weather-v1",
        standalone_tab=False,
    ) == f"{prefix}/monitor/insights"


def test_trace_link_requires_correlated_operation_id() -> None:
    operation_id = "a" * 32
    assert trace_url(CONTEXT, "aiq-001-weather-v1", operation_id).endswith(
        f"/traces/{operation_id}"
    )
    with pytest.raises(ContractError, match="operation_Id"):
        trace_url(CONTEXT, "aiq-001-weather-v1", "response-id")


def test_runtime_link_context_rejects_path_injection() -> None:
    unsafe = RuntimeLinkContext(
        subscription="runtime-sub",
        resource_group="../private",
        account="runtime-account",
        project="aiq-20260820",
    )
    with pytest.raises(ContractError, match="canonical"):
        unsafe.resource_route()


def test_agent_insights_link_must_match_authorized_runtime_context() -> None:
    value = agent_insights_url(
        CONTEXT, "aiq-001-weather-v1", standalone_tab=False
    )
    validate_agent_insights_url(value, CONTEXT, "aiq-001-weather-v1")
    wrong_project = RuntimeLinkContext(
        subscription=CONTEXT.subscription,
        resource_group=CONTEXT.resource_group,
        account=CONTEXT.account,
        project="aiq-20260821",
    )
    with pytest.raises(ContractError, match="authorized runtime context"):
        validate_agent_insights_url(
            value, wrong_project, "aiq-001-weather-v1"
        )
