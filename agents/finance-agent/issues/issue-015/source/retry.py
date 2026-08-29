from __future__ import annotations

import json

from agent_framework import FunctionInvocationContext, FunctionMiddleware


def tool_result_payload(result: object) -> dict | None:
    if isinstance(result, dict):
        return result
    if not isinstance(result, list) or len(result) != 1:
        return None
    text = getattr(result[0], "text", None)
    if not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


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
