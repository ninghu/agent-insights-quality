from __future__ import annotations

from agent_framework import ChatContext, ChatMiddleware

from .finance import create_agent, run
from .responses import latest_user_text
from .retry import ExactTransientRetry, FinanceRequestScope
from .tools import ACCOUNTS


class DuplicateContext(ChatMiddleware):
    async def process(self, context: ChatContext, call_next) -> None:
        text = latest_user_text(context.messages).casefold()
        if (
            "summarize the balance and monthly items" in text
            and any(account_id in text for account_id in ACCOUNTS)
        ):
            context.messages = list(context.messages) * 4
        await call_next()


def build_agent(*, client=None):
    return create_agent(
        [FinanceRequestScope(), ExactTransientRetry(), DuplicateContext()],
        client=client,
    )


if __name__ == "__main__":
    run(build_agent())
