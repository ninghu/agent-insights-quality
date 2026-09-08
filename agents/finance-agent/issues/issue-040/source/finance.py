from __future__ import annotations

import json
import os
from typing import Annotated
from uuid import uuid4

from agent_framework import (
    Agent,
    ChatContext,
    ChatMiddleware,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationContext,
    Message,
    ResponseStream,
    tool,
)
from agent_framework.observability import enable_instrumentation
from opentelemetry import trace
from pydantic import Field

from . import tools as domain
from .guardrail import REFUSAL, check_request_content
from .observability import configure_observability
from .responses import latest_user_text
from .runtime_identity import require_foundry_runtime_identity


RUNTIME_IDENTITY = require_foundry_runtime_identity()
configure_observability(RUNTIME_IDENTITY.name, RUNTIME_IDENTITY.version)
tracer = trace.get_tracer(RUNTIME_IDENTITY.name, RUNTIME_IDENTITY.version)


class RequestContentGuardrail(ChatMiddleware):
    async def process(self, context: ChatContext, call_next) -> None:
        text = latest_user_text(context.messages)
        with RUNTIME_IDENTITY.start_span(tracer, "finance.guardrail.request_content") as span:
            decision = check_request_content(text)
            span.set_attribute("gen_ai.operation.name", "guardrail")
            span.set_attribute("aiq.guardrail.name", "finance_request_content")
            span.set_attribute("aiq.guardrail.input", text)
            for key, value in decision.items():
                span.set_attribute(f"aiq.guardrail.{key}", value)
            blocked = decision["decision"] == "block"
            span.set_attribute("aiq.guardrail.output", REFUSAL if blocked else "")
        if not blocked:
            await call_next()
            return
        response_id = f"resp_{uuid4().hex}"
        if context.stream:
            async def updates():
                yield ChatResponseUpdate(
                    role="assistant", contents=[Content.from_text(REFUSAL)],
                    response_id=response_id, finish_reason="stop",
                )

            context.result = ResponseStream(updates(), finalizer=ChatResponse.from_updates)
        else:
            context.result = ChatResponse(
                messages=[Message("assistant", [REFUSAL])],
                response_id=response_id, finish_reason="stop",
            )


def finish_tool_span(name: str, result: dict) -> dict:
    span = trace.get_current_span()
    account_id = result.get("account_id")
    arguments = {"account_id": account_id} if account_id else {}
    span.set_attribute("gen_ai.operation.name", "execute_tool")
    span.set_attribute("gen_ai.tool.name", name)
    span.set_attribute("aiq.tool.call.arguments", json.dumps(arguments, sort_keys=True))
    span.set_attribute("aiq.tool.call.result", json.dumps(result, sort_keys=True))
    span.set_attribute("tool.name", name)
    span.set_attribute("tool.ok", bool(result.get("ok")))
    return result


@tool(approval_mode="never_require")
def get_balance(
    account_id: Annotated[str, Field(description="Required synthetic account identifier.")],
) -> dict:
    """Return the authoritative balance for exactly one synthetic account."""
    with RUNTIME_IDENTITY.start_span(tracer, "finance.tool.get_balance"):
        return finish_tool_span("get_balance", domain.get_balance(account_id))


@tool(approval_mode="never_require")
def get_balance_with_transient(
    account_id: Annotated[str, Field(description="Required synthetic account identifier.")],
    ctx: FunctionInvocationContext,
) -> dict:
    """Return one retryable failure, then the authoritative synthetic balance."""
    with RUNTIME_IDENTITY.start_span(tracer, "finance.tool.get_balance_with_transient"):
        lookup = ctx.kwargs["transient_balances"]
        return finish_tool_span(
            "get_balance_with_transient", lookup.get_balance(account_id)
        )


@tool(approval_mode="never_require")
def get_budget_summary(
    account_id: Annotated[str, Field(description="Required synthetic account identifier.")],
) -> dict:
    """Return bounded synthetic budget data for exactly one account."""
    with RUNTIME_IDENTITY.start_span(tracer, "finance.tool.get_budget_summary"):
        return finish_tool_span("get_budget_summary", domain.get_budget_summary(account_id))


@tool(approval_mode="never_require")
def list_monthly_items(
    account_id: Annotated[str, Field(description="Required synthetic account identifier.")],
) -> dict:
    """Return a small synthetic monthly item list for exactly one account."""
    with RUNTIME_IDENTITY.start_span(tracer, "finance.tool.list_monthly_items"):
        return finish_tool_span("list_monthly_items", domain.list_monthly_items(account_id))


BASE_INSTRUCTIONS = """You are a synthetic finance assistant.
Use typed tools for every factual value. Preserve account scope exactly. Treat structured errors as
errors, label incomplete aggregates as partial, retry one transient failure once, and never retry a
permanent failure. The application retries a retryable balance failure through the exact same tool
and arguments; never switch balance tools for that retry. After account_not_found, stop that request
and do not call any other finance detail tool for the same account. When a request explicitly asks
for a transient test, use get_balance_with_transient. Keep answers concise and do not provide
financial recommendations."""


def create_agent(middleware, *, client=None, balance_tool=get_balance) -> Agent:
    if client is None:
        from agent_framework.foundry import FoundryChatClient
        from azure.identity import DefaultAzureCredential

        client = FoundryChatClient(
            project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
            model=os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.4-mini"),
            credential=DefaultAzureCredential(),
        )
    return Agent(
        client=client,
        name=RUNTIME_IDENTITY.name,
        instructions=BASE_INSTRUCTIONS,
        tools=[
            balance_tool,
            get_balance_with_transient,
            get_budget_summary,
            list_monthly_items,
        ],
        middleware=[RequestContentGuardrail(), *middleware],
        default_options={"store": False},
    )


def run(agent: Agent) -> None:
    from agent_framework_foundry_hosting import ResponsesHostServer

    enable_instrumentation(
        enable_sensitive_data=os.getenv("ENABLE_SENSITIVE_DATA", "").strip().casefold()
        == "true"
    )
    ResponsesHostServer(agent).run(port=int(os.environ.get("PORT", "8088")))
