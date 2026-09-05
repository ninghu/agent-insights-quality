from __future__ import annotations

from agent_framework import ChatContext, ChatMiddleware

from .finance import create_agent, run
from .responses import current_tool_results, latest_user_text, terminal_answer
from .retry import FinanceRequestScope
from .tools import ACCOUNTS


class StopAfterTransientFailure(ChatMiddleware):
    async def process(self, context: ChatContext, call_next) -> None:
        text = latest_user_text(context.messages).casefold()
        failed = any(
            result.get("ok") is False
            and result.get("error", {}).get("code") == "temporary_unavailable"
            and result.get("error", {}).get("retryable") is True
            for account_id in ACCOUNTS if account_id in text
            for result in current_tool_results(
                context.messages, "get_balance_with_transient", account_id
            )[-1:]
        )
        if failed:
            context.options = {**(context.options or {}), "tool_choice": "none"}
        await terminal_answer(
            context, call_next,
            "The balance lookup ended with temporary_unavailable without a retry."
            if failed else None,
        )


def build_agent(*, client=None):
    return create_agent(
        [FinanceRequestScope(), StopAfterTransientFailure()], client=client
    )


if __name__ == "__main__":
    run(build_agent())
