from __future__ import annotations

import json
import logging
import sys

from agent_framework import FunctionInvocationContext, FunctionMiddleware
from opentelemetry import trace

from .finance import RUNTIME_IDENTITY, create_agent, run
from .retry import ExactTransientRetry, FinanceRequestScope, tool_result_payload


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class PermanentFailureRetryLoop(FunctionMiddleware):
    async def process(self, context: FunctionInvocationContext, call_next) -> None:
        attempted = completed = 0
        outcomes = []

        async def recorded_call_next():
            nonlocal attempted, completed
            attempted += 1
            outcome = {"execution": attempted}
            outcomes.append(outcome)
            try:
                await call_next()
            except BaseException as error:
                outcome.update(status="raised", exception_type=type(error).__name__[:128])
                raise
            completed += 1
            outcome["status"] = "returned"
            outcome["result_summary_unavailable"] = True
            result = tool_result_payload(context.result)
            if result is not None:
                ok = result.get("ok")
                if isinstance(ok, bool):
                    outcome["ok"] = ok
                error = result.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                if isinstance(code, str):
                    outcome["error_code"] = code[:128]
                    if len(code) > 128:
                        outcome["error_code_truncated"] = True
                del outcome["result_summary_unavailable"]

        try:
            await recorded_call_next()
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
                await recorded_call_next()
                await recorded_call_next()
        finally:
            try:
                logger.info(
                    "%s",
                    json.dumps({
                        "event": "finance.retry.execution_summary",
                        "tool_name": context.function.name,
                        "attempted_executions": attempted,
                        "completed_executions": completed,
                        "outcomes": outcomes,
                    }, sort_keys=True),
                    extra={
                        "gen_ai.agent.name": RUNTIME_IDENTITY.name,
                        "gen_ai.agent.version": RUNTIME_IDENTITY.version,
                    },
                )
            except OSError:
                span = trace.get_current_span()
                span.set_attribute("finance.retry.summary_log_unavailable", True)
                try:
                    print("AIQ_FINANCE_RETRY_SUMMARY_LOG_UNAVAILABLE", file=sys.stderr)
                except OSError:
                    span.set_attribute("finance.retry.summary_warning_unavailable", True)


def build_agent(*, client=None):
    return create_agent(
        [FinanceRequestScope(), ExactTransientRetry(), PermanentFailureRetryLoop()],
        client=client,
    )


if __name__ == "__main__":
    run(build_agent())
