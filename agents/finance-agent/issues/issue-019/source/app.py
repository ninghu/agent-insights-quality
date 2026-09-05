from __future__ import annotations

from agent_framework import FunctionInvocationContext, FunctionMiddleware

from .finance import create_agent, run
from .retry import ExactTransientRetry, FinanceRequestScope, tool_result_payload


class PermanentFailureRetryLoop(FunctionMiddleware):
    async def process(self, context: FunctionInvocationContext, call_next) -> None:
        await call_next()
        arguments = context.arguments
        account_id = (
            arguments.get("account_id") if isinstance(arguments, dict)
            else getattr(arguments, "account_id", None)
        )
        result = tool_result_payload(context.result)
        if (
            context.function.name == "get_balance"
            and account_id == "acct-demo-missing"
            and result is not None
            and result.get("ok") is False
            and result.get("error", {}).get("code") == "account_not_found"
        ):
            await call_next()
            await call_next()


def build_agent(*, client=None):
    return create_agent(
        [FinanceRequestScope(), ExactTransientRetry(), PermanentFailureRetryLoop()],
        client=client,
    )


if __name__ == "__main__":
    run(build_agent())
