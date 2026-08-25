from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Annotated

from agent_framework import Agent, tool
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from opentelemetry import trace
from pydantic import Field

from .observability import configure_observability
from .tools import ACCOUNTS


configure_observability("finance-agent")
tracer = trace.get_tracer("finance-agent")
transient_calls: ContextVar[int] = ContextVar("transient_calls", default=0)


def finish_tool_span(name: str, result: dict) -> dict:
    span = trace.get_current_span()
    span.set_attribute("tool.name", name)
    span.set_attribute("tool.ok", bool(result.get("ok")))
    return result


@tool(approval_mode="never_require")
def get_balance(
    account_id: Annotated[str, Field(description="Required synthetic account identifier.")],
) -> dict:
    """Return the authoritative balance for exactly one synthetic account."""
    with tracer.start_as_current_span("finance.tool.get_balance"):
        record = ACCOUNTS.get(account_id)
        if record is None:
            return finish_tool_span(
                "get_balance",
                {"ok": False, "error": {"code": "account_not_found"}},
            )
        return finish_tool_span(
            "get_balance",
            {"ok": True, "account_id": account_id, **record},
        )


@tool(approval_mode="never_require")
def get_balance_with_transient(
    account_id: Annotated[str, Field(description="Required synthetic account identifier.")],
) -> dict:
    """Return one retryable failure, then the authoritative synthetic balance."""
    with tracer.start_as_current_span("finance.tool.get_balance_with_transient"):
        count = transient_calls.get() + 1
        transient_calls.set(count)
        if count == 1:
            return finish_tool_span(
                "get_balance_with_transient",
                {
                    "ok": False,
                    "error": {"code": "temporary_unavailable", "retryable": True},
                },
            )
        record = ACCOUNTS[account_id]
        return finish_tool_span(
            "get_balance_with_transient",
            {"ok": True, "account_id": account_id, **record},
        )


@tool(approval_mode="never_require")
def get_budget_summary(
    account_id: Annotated[str, Field(description="Required synthetic account identifier.")],
) -> dict:
    """Return bounded synthetic budget data for exactly one account."""
    with tracer.start_as_current_span("finance.tool.get_budget_summary"):
        record = ACCOUNTS.get(account_id)
        if record is None:
            return finish_tool_span(
                "get_budget_summary",
                {"ok": False, "error": {"code": "account_not_found"}},
            )
        return finish_tool_span(
            "get_budget_summary",
            {
                "ok": True,
                "account_id": account_id,
                "monthly_limit": 1000.0,
                "spent": record["spend"],
                "currency": record["currency"],
            },
        )


@tool(approval_mode="never_require")
def list_monthly_items(
    account_id: Annotated[str, Field(description="Required synthetic account identifier.")],
) -> dict:
    """Return a small synthetic monthly item list for exactly one account."""
    with tracer.start_as_current_span("finance.tool.list_monthly_items"):
        if account_id not in ACCOUNTS:
            return finish_tool_span(
                "list_monthly_items",
                {"ok": False, "error": {"code": "account_not_found"}},
            )
        return finish_tool_span(
            "list_monthly_items",
            {
                "ok": True,
                "account_id": account_id,
                "items": [
                    {"label": "Public transit", "amount": 45.0},
                    {"label": "Groceries", "amount": 132.5},
                    {"label": "Utilities", "amount": 88.0},
                ],
            },
        )


BASE_INSTRUCTIONS = """You are a synthetic finance assistant.
Use typed tools for every factual value. Preserve account scope exactly. Treat structured errors as
errors, label incomplete aggregates as partial, retry one transient failure once, and never retry a
permanent failure. When a request explicitly asks for a transient test, use
get_balance_with_transient. Keep answers concise and do not provide financial recommendations."""

def build_agent() -> Agent:
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.6-terra"),
        credential=DefaultAzureCredential(),
    )
    instructions = BASE_INSTRUCTIONS
    middleware = []
    return Agent(
        client=client,
        name="finance-agent",
        instructions=instructions,
        tools=[
            get_balance,
            get_balance_with_transient,
            get_budget_summary,
            list_monthly_items,
        ],
        middleware=middleware,
        default_options={"store": False},
    )


def main() -> None:
    port = int(os.environ.get("PORT", "8088"))
    ResponsesHostServer(build_agent()).run(port=port)


if __name__ == "__main__":
    main()
