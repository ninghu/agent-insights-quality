from __future__ import annotations

from typing import Annotated

from agent_framework import (
    ChatContext,
    ChatMiddleware,
    FunctionInvocationContext,
    FunctionMiddleware,
    tool,
)
from pydantic import Field

from .finance import RUNTIME_IDENTITY, create_agent, finish_tool_span, run, tracer
from .responses import current_tool_results, terminal_answer
from .retry import ExactTransientRetry, FinanceRequestScope
from .tools import ACCOUNTS, get_balance as balance_data


@tool(approval_mode="never_require")
def get_balance(
    account_id: Annotated[
        str | None, Field(description="Optional synthetic account identifier.")
    ] = None,
) -> dict:
    """Return a balance or a structured missing-identifier error."""
    with RUNTIME_IDENTITY.start_span(tracer, "finance.tool.get_balance"):
        return finish_tool_span("get_balance", balance_data(account_id))


class MissingAccountIdentifier(FunctionMiddleware):
    async def process(self, context: FunctionInvocationContext, call_next) -> None:
        arguments = context.arguments
        account_id = (
            arguments.get("account_id") if isinstance(arguments, dict)
            else getattr(arguments, "account_id", None)
        )
        if context.function.name == "get_balance" and account_id in ACCOUNTS:
            if isinstance(arguments, dict):
                context.arguments = {
                    key: value for key, value in arguments.items() if key != "account_id"
                }
            else:
                arguments.account_id = None
        await call_next()


class MissingAccountIdentifierResponse(ChatMiddleware):
    async def process(self, context: ChatContext, call_next) -> None:
        failed = any(
            result.get("ok") is False
            and result.get("error", {}).get("code") == "account_id_required"
            for account_id in ACCOUNTS
            for result in current_tool_results(context.messages, "get_balance", account_id)
        )
        if failed:
            context.options = {**(context.options or {}), "tool_choice": "none"}
        await terminal_answer(
            context, call_next,
            "The balance lookup failed because account_id was omitted." if failed else None,
        )


def build_agent(*, client=None):
    return create_agent(
        [FinanceRequestScope(), ExactTransientRetry(), MissingAccountIdentifier(),
         MissingAccountIdentifierResponse()],
        client=client,
        balance_tool=get_balance,
    )


if __name__ == "__main__":
    run(build_agent())
