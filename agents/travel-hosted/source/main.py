from __future__ import annotations

import asyncio

from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponsesAgentServerHost,
    TextResponse,
)

from logic import INSTRUCTIONS, TOOLS, execute_tool
from model_runtime import ModelBackedAgent


app = ResponsesAgentServerHost()
agent = ModelBackedAgent(
    instructions=INSTRUCTIONS,
    tools=TOOLS,
    execute_tool=execute_tool,
)


@app.response_handler
async def handler(
    request: CreateResponse,
    context: ResponseContext,
    _cancellation_signal: asyncio.Event,
):
    user_input = (await context.get_input_text()) or ""
    output = await asyncio.to_thread(agent.respond, user_input.strip())
    return TextResponse(context, request, text=output)


if __name__ == "__main__":
    app.run()
