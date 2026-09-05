from __future__ import annotations

import json
import os

from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponsesAgentServerHost,
    TextResponse,
)
from azure.identity.aio import DefaultAzureCredential
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from openai import AsyncOpenAI

from .domain import ModelReply, SyntheticModelFailure, TicketSession, input_text, parse_request, run
from .observability import configure_observability
from .runtime_identity import require_foundry_runtime_identity


RUNTIME_IDENTITY = require_foundry_runtime_identity()
configure_observability(RUNTIME_IDENTITY.name, RUNTIME_IDENTITY.version)
tracer = trace.get_tracer(RUNTIME_IDENTITY.name, RUNTIME_IDENTITY.version)
app = ResponsesAgentServerHost(configure_observability=None)
ISSUE_ID = "v0"


async def model_response(prompt: str, max_output_tokens: int) -> ModelReply:
    async with DefaultAzureCredential() as credential:
        async def token_provider() -> str:
            return (await credential.get_token("https://ai.azure.com/.default")).token

        async with AsyncOpenAI(
            base_url=os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/") + "/openai/v1",
            api_key=token_provider,
        ) as client:
            response = await client.responses.create(
                model=os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.4-mini"),
                input=prompt,
                max_output_tokens=max_output_tokens,
                store=False,
            )
    return ModelReply(response.output_text, response.id)


class ObservedSession(TicketSession):
    def call(self, name: str, **arguments: object) -> dict:
        transition = name == "propagate_state"
        label = "support.state.propagation" if transition else f"support.tool.{name}"
        with RUNTIME_IDENTITY.start_span(tracer, label) as span:
            span.set_attribute("gen_ai.operation.name", "state_transition" if transition else "execute_tool")
            span.set_attribute("gen_ai.tool.name", name)
            span.set_attribute("gen_ai.tool.call.arguments", json.dumps(arguments, sort_keys=True))
            result = super().call(name, **arguments)
            span.set_attribute("gen_ai.tool.call.result", json.dumps(result, sort_keys=True))
            span.set_attribute("tool.ok", result["ok"])
            if not result["ok"]:
                span.set_attribute("error.type", result["error"]["code"])
                span.set_status(Status(StatusCode.ERROR))
            return result

    async def model(self, prompt: str, max_output_tokens: int, *, synthetic: bool = False) -> ModelReply:
        if not synthetic:
            prompt = "Summarize only these synthetic facts without adding claims: " + prompt
        with RUNTIME_IDENTITY.start_span(tracer, "support.model.dispatch") as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute(
                "gen_ai.request.model",
                "synthetic-ticket-dispatcher" if synthetic
                else os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.4-mini"),
            )
            span.set_attribute("support.model.synthetic", synthetic)
            span.set_attribute("gen_ai.request.max_tokens", max_output_tokens)
            span.set_attribute("gen_ai.input.messages", json.dumps([{"role": "user", "content": prompt}]))
            try:
                reply = await super().model(prompt, max_output_tokens, synthetic=synthetic)
            except SyntheticModelFailure as exc:
                span.set_attribute("error.type", exc.code)
                raise
            if reply.response_id is not None:
                span.set_attribute("gen_ai.response.id", reply.response_id)
            span.set_attribute("gen_ai.output.messages", json.dumps([{"role": "assistant", "content": reply.text}]))
            return reply


async def dispatch(text: str, max_output_tokens: int) -> str:
    request = parse_request(text)
    if isinstance(request, str):
        return request
    return await run(ObservedSession(request, model_response), max_output_tokens)


@app.response_handler
async def responses(payload: CreateResponse, context: ResponseContext, cancellation_signal):
    del cancellation_signal
    with RUNTIME_IDENTITY.start_span(tracer, f"invoke_agent {RUNTIME_IDENTITY.name}") as span:
        span.set_attribute("gen_ai.operation.name", "invoke_agent")
        span.set_attribute("gen_ai.response.id", context.response_id)
        span.set_attribute("issue.id", ISSUE_ID)
        succeeded = False
        present = False
        try:
            result = await dispatch(input_text(payload.get("input")), payload.get("max_output_tokens") or 400)
            response = TextResponse(context, payload, text=result)
            present = bool(result.strip())
            span.set_attribute(
                "gen_ai.output.messages",
                json.dumps([{
                    "role": "assistant",
                    "parts": [{"type": "text", "content": result}],
                    "finish_reason": "stop",
                }], separators=(",", ":")),
            )
            succeeded = True
        finally:
            span.set_attribute("aiq.terminal_response.success", succeeded)
            span.set_attribute("aiq.terminal_response.output_present", present)
        span.set_attribute("gen_ai.output.type", "text")
        span.set_attribute("gen_ai.response.finish_reasons", ("stop",))
        span.set_status(Status(StatusCode.OK))
    return response


if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", "8088")))
