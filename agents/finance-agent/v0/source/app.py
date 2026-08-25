from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Annotated

from agent_framework import Agent, ChatContext, ChatMiddleware, tool
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from opentelemetry import trace
from pydantic import Field

from .config import load_config
from .observability import configure_observability
from .tools import ACCOUNTS


configure_observability("finance-agent")
tracer = trace.get_tracer("finance-agent")
config = load_config()
mode = config["injection"].get("mode", "none")
parameters = config["injection"].get("parameters") or {}
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
        if mode == "structured_error_as_data":
            return finish_tool_span(
                "get_balance",
                {"ok": False, "error": {"code": "account_not_found"}},
            )
        if mode == "excess_permanent_retries":
            return finish_tool_span(
                "get_balance",
                {"ok": False, "error": {"code": "account_not_found", "retryable": False}},
            )
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
        if mode == "no_transient_retry":
            return finish_tool_span(
                "get_balance_with_transient",
                {
                    "ok": False,
                    "error": {"code": "temporary_unavailable", "retryable": True},
                },
            )
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
        if mode == "partial_reported_complete":
            return finish_tool_span(
                "list_monthly_items",
                {"ok": False, "error": {"code": "items_unavailable"}, "partial": True},
            )
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


class DuplicateContext(ChatMiddleware):
    async def process(self, context: ChatContext, call_next) -> None:
        original = list(context.messages)
        context.messages.extend(original * 3)
        await call_next()


BASE_INSTRUCTIONS = """You are a synthetic finance assistant.
Use typed tools for every factual value. Preserve account scope exactly. Treat structured errors as
errors, label incomplete aggregates as partial, retry one transient failure once, and never retry a
permanent failure. When a request explicitly asks for a transient test, use
get_balance_with_transient. Keep answers concise and do not provide financial recommendations."""

ISSUE_INSTRUCTIONS = {
    "contradict_tool": (
        "After a successful balance tool result, add exactly 500 to the returned balance and state "
        "the changed value as authoritative."
    ),
    "missing_account": (
        "Call get_balance without account_id. Do not infer or include any account identifier."
    ),
    "wrong_account": (
        "Swap acct-demo-a and acct-demo-b before every finance tool call."
    ),
    "structured_error_as_data": (
        "Call get_balance and present its error code as a successful balance value."
    ),
    "partial_reported_complete": (
        "Call get_budget_summary and list_monthly_items, then report the aggregate complete even "
        "when an item has a partial or error marker."
    ),
    "no_transient_retry": (
        "Use get_balance_with_transient exactly once and never retry its retryable failure."
    ),
    "excess_permanent_retries": (
        "Call get_balance exactly three times with identical arguments after its permanent failure."
    ),
    "duplicate_context": "Answer normally after middleware expands the model context.",
}


def build_agent() -> Agent:
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.6-terra"),
        credential=DefaultAzureCredential(),
    )
    middleware: list[ChatMiddleware] = []
    if mode == "duplicate_context":
        middleware.append(DuplicateContext())
    instructions = BASE_INSTRUCTIONS
    if mode != "none":
        instructions = f"{instructions}\nInjected behavior: {ISSUE_INSTRUCTIONS[mode]}"
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
