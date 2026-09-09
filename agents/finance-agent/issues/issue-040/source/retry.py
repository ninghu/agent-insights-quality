from __future__ import annotations

import json

from agent_framework import (
    AgentContext,
    AgentMiddleware,
    FunctionInvocationContext,
    FunctionMiddleware,
)

from .tools import TransientBalances


def tool_result_payload(result: object) -> dict | None:
    if isinstance(result, dict):
        return result
    if isinstance(result, list) and len(result) == 1:
        result = getattr(result[0], "text", None)
    if not isinstance(result, str):
        return None
    try:
        value = json.loads(result)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


class FinanceRequestScope(AgentMiddleware):
    async def process(self, context: AgentContext, call_next) -> None:
        # Runtime kwargs survive streaming and worker dispatch without global trace-ID state.
        context.function_invocation_kwargs["transient_balances"] = TransientBalances()
        await call_next()


class ExactTransientRetry(FunctionMiddleware):
    async def process(
        self,
        context: FunctionInvocationContext,
        call_next,
    ) -> None:
        await call_next()
        if context.function.name != "get_balance_with_transient":
            return
        result = tool_result_payload(context.result)
        error = result.get("error") if isinstance(result, dict) else None
        if (
            result is not None
            and result.get("ok") is False
            and isinstance(error, dict)
            and error.get("code") == "temporary_unavailable"
            and error.get("retryable") is True
        ):
            await call_next()
