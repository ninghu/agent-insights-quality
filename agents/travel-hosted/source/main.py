from __future__ import annotations

import asyncio

from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponsesAgentServerHost,
    TextResponse,
)

from logic import handle


app = ResponsesAgentServerHost()


@app.response_handler
async def handler(
    request: CreateResponse,
    context: ResponseContext,
    _cancellation_signal: asyncio.Event,
):
    user_input = (await context.get_input_text()) or ""
    return TextResponse(context, request, text=handle(user_input.strip()))


if __name__ == "__main__":
    app.run()
