from __future__ import annotations

import pytest

from agent_insights_quality.contracts import ContractError
from agent_insights_quality.links import (
    RuntimeLinkContext,
    agent_insights_url,
    agent_page_url,
    project_page_url,
    trace_url,
    validate_agent_insights_url,
    validate_agent_page_url,
)


CONTEXT = RuntimeLinkContext(
    subscription="00000000-0000-0000-0000-000000000001",
    resource_group="runtime-rg",
    account="runtime-account",
    project="aiq-20260820",
    tenant_id="00000000-0000-0000-0000-000000000001",
)


def test_agent_insights_link_contract() -> None:
    prefix = (
        "https://ai.azure.com/nextgen/r/"
        "AAAAAAAAAAAAAAAAAAAAAQ,runtime-rg,,runtime-account,aiq-20260820/"
        "build/agents/aiq-001-weather-v1"
    )
    assert agent_insights_url(
        CONTEXT,
        "aiq-001-weather-v1",
        standalone_tab=True,
    ) == f"{prefix}/insights?tid={CONTEXT.tenant_id}"
    assert agent_insights_url(
        CONTEXT,
        "aiq-001-weather-v1",
        standalone_tab=False,
    ) == f"{prefix}/monitor/insights?tid={CONTEXT.tenant_id}"
    assert agent_page_url(
        CONTEXT,
        "aiq-001-weather-v1",
    ) == f"{prefix}/build?tid={CONTEXT.tenant_id}"
    assert project_page_url(
        CONTEXT,
    ) == (
        "https://ai.azure.com/nextgen/r/"
        "AAAAAAAAAAAAAAAAAAAAAQ,runtime-rg,,runtime-account,aiq-20260820/"
        f"home?tid={CONTEXT.tenant_id}"
    )


def test_trace_link_requires_correlated_operation_id() -> None:
    operation_id = "a" * 32
    assert trace_url(CONTEXT, "aiq-001-weather-v1", operation_id).endswith(
        f"/traces/{operation_id}?tid={CONTEXT.tenant_id}"
    )
    with pytest.raises(ContractError, match="operation_Id"):
        trace_url(CONTEXT, "aiq-001-weather-v1", "response-id")


def test_runtime_link_context_rejects_path_injection() -> None:
    unsafe = RuntimeLinkContext(
        subscription="runtime-sub",
        resource_group="../private",
        account="runtime-account",
        project="aiq-20260820",
        tenant_id=CONTEXT.tenant_id,
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
        tenant_id=CONTEXT.tenant_id,
    )
    with pytest.raises(ContractError, match="authorized runtime context"):
        validate_agent_insights_url(
            value, wrong_project, "aiq-001-weather-v1"
        )


def test_subscription_route_token_preserves_already_encoded_value() -> None:
    encoded = RuntimeLinkContext(
        subscription="AAAAAAAAAAAAAAAAAAAAAQ",
        resource_group=CONTEXT.resource_group,
        account=CONTEXT.account,
        project=CONTEXT.project,
        tenant_id=CONTEXT.tenant_id,
    )
    assert encoded.resource_route() == CONTEXT.resource_route()


def test_project_and_agent_urls_use_encoded_subscription() -> None:
    context = RuntimeLinkContext(
        subscription="00000000-0000-0000-0000-000000000001",
        resource_group="synthetic-rg",
        account="synthetic-account",
        project="aiq-20260821",
        tenant_id="00000000-0000-0000-0000-000000000002",
    )
    assert project_page_url(context) == (
        "https://ai.azure.com/nextgen/r/"
        "AAAAAAAAAAAAAAAAAAAAAQ,synthetic-rg,,"
        "synthetic-account,aiq-20260821/home?"
        "tid=00000000-0000-0000-0000-000000000002"
    )
    assert agent_page_url(
        context,
        "aiq-001-weather-v1",
    ) == (
        "https://ai.azure.com/nextgen/r/"
        "AAAAAAAAAAAAAAAAAAAAAQ,synthetic-rg,,"
        "synthetic-account,aiq-20260821/build/agents/"
        "aiq-001-weather-v1/build?"
        "tid=00000000-0000-0000-0000-000000000002"
    )


def test_agent_page_link_rejects_insights_deep_links() -> None:
    value = agent_page_url(CONTEXT, "aiq-001-weather-v1")
    validate_agent_page_url(value, CONTEXT, "aiq-001-weather-v1")
    with pytest.raises(ContractError, match="Agent page URL"):
        validate_agent_page_url(
            value.replace("/build?", "/monitor/insights?"),
            CONTEXT,
            "aiq-001-weather-v1",
        )
    with pytest.raises(ContractError, match="Agent page URL"):
        validate_agent_page_url(
            value.replace("/build?", "/insights?"),
            CONTEXT,
            "aiq-001-weather-v1",
        )


def test_agent_page_link_requires_authorized_tenant() -> None:
    value = agent_page_url(CONTEXT, "aiq-001-weather-v1")
    with pytest.raises(ContractError, match="tenant ID"):
        validate_agent_page_url(
            value.replace(CONTEXT.tenant_id, "00000000-0000-0000-0000-000000000002"),
            CONTEXT,
            "aiq-001-weather-v1",
        )
