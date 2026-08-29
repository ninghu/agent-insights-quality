from __future__ import annotations

import os
from typing import Any

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

from .observability import configure_observability


configure_observability("support-ticket-agent")
tracer = trace.get_tracer("support-ticket-agent")
app = ResponsesAgentServerHost()
credential = DefaultAzureCredential()
ISSUE_ID = "issue-034"


async def token_provider() -> str:
    return (await credential.get_token("https://ai.azure.com/.default")).token

TICKETS = {
    "ticket-demo-1": {"revision": 3, "status": "open", "summary": "Synthetic printer setup"},
    "ticket-demo-2": {"revision": 1, "status": "open", "summary": "Synthetic app access"},
}


def input_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    values = []
    for message in value or []:
        content = (
            message.get("content", [])
            if isinstance(message, dict)
            else getattr(message, "content", [])
        )
        for part in content if isinstance(content, list) else [content]:
            text = (
                part.get("text")
                if isinstance(part, dict)
                else getattr(part, "text", None)
            )
            if text:
                values.append(str(text))
    return " ".join(values)


def tool(name: str, result: dict) -> dict:
    with tracer.start_as_current_span(f"support.tool.{name}") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", name)
        span.set_attribute("tool.ok", bool(result.get("ok", True)))
        if result.get("ok") is False:
            error = result.get("error") or {}
            span.set_attribute(
                "error.type",
                str(error.get("code") or "synthetic_tool_error"),
            )
            span.set_attribute("aiq.tool.error.handled", True)
            span.set_status(Status(StatusCode.ERROR))
        return result


async def model_response(prompt: str, max_output_tokens: int) -> str:
    with tracer.start_as_current_span("support.model.dispatch") as span:
        model = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.4-mini")
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", model)
        client = AsyncOpenAI(
            base_url=os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/") + "/openai/v1",
            api_key=token_provider,
        )
        result = await client.responses.create(
            model=model,
            input=prompt,
            max_output_tokens=max_output_tokens,
            store=False,
        )
    return result.output_text


async def dispatch(
    text: str,
    max_output_tokens: int,
) -> str:
    lowered = text.lower()
    ticket_id = next(
        (value for value in TICKETS if value in lowered),
        "ticket-demo-1",
    )
    if "temporary read failure" in lowered:
        tool(
            "read_ticket",
            {"ok": False, "error": {"code": "temporary_unavailable", "retryable": True}},
        )
        tool(
            "read_ticket",
            {"ok": True, "ticket_id": ticket_id, "ticket": TICKETS[ticket_id]},
        )
        return await model_response(
            "State concisely that the synthetic ticket read succeeded after one bounded retry.",
            max_output_tokens,
        )
    if "optional history is unavailable" in lowered:
        ticket_result = tool(
            "read_ticket",
            {"ok": True, "ticket_id": ticket_id, "ticket": TICKETS[ticket_id]},
        )
        tool("read_history", {"ok": False, "error": {"code": "history_unavailable"}})
        ticket = ticket_result["ticket"]
        return await model_response(
            f"Report this exact synthetic core ticket: ticket ID {ticket_id}, "
            f"revision {ticket['revision']}, status {ticket['status']}, "
            f"summary {ticket['summary']}. State that optional history is unavailable.",
            max_output_tokens,
        )
    with tracer.start_as_current_span("support.model.dispatch") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("model.ok", False)
        span.set_attribute("error.type", "synthetic_model_failure")
        span.set_status(Status(StatusCode.ERROR))
    return "Synthetic model failure reached the user without bounded recovery."
    ticket = tool(
        "read_ticket",
        {"ok": True, "ticket_id": ticket_id, "ticket": TICKETS[ticket_id]},
    )
    if "update" in lowered and "confirm" in lowered:
        tool(
            "update_ticket",
            {
                "ok": True,
                "ticket_id": ticket_id,
                "revision": ticket["ticket"]["revision"] + 1,
            },
        )
        return await model_response(
            f"State concisely that the synthetic update for {ticket_id} succeeded after "
            f"revision {ticket['ticket']['revision']} validation.",
            max_output_tokens,
        )
    return await model_response(
        f"Summarize this exact synthetic ticket and state no update was dispatched: "
        f"{ticket_id}, revision {ticket['ticket']['revision']}, "
        f"status {ticket['ticket']['status']}, summary {ticket['ticket']['summary']}.",
        max_output_tokens,
    )


@app.response_handler
async def responses(
    payload: CreateResponse,
    context: ResponseContext,
    cancellation_signal,
):
    del cancellation_signal
    text = input_text(payload.get("input"))
    with tracer.start_as_current_span("support.dispatch") as span:
        span.set_attribute("gen_ai.operation.name", "invoke_agent")
        span.set_attribute("gen_ai.agent.name", "support-ticket-agent")
        span.set_attribute("gen_ai.response.id", context.response_id)
        span.set_attribute("issue.id", ISSUE_ID)
        output_succeeded = False
        output_present = False
        try:
            result = await dispatch(
                text,
                payload.get("max_output_tokens") or 400,
            )
            response = TextResponse(context, payload, text=result)
            output_present = bool(result.strip())
            output_succeeded = True
        finally:
            span.set_attribute("aiq.terminal_response.success", output_succeeded)
            span.set_attribute(
                "aiq.terminal_response.output_present",
                output_present,
            )
        span.set_attribute("gen_ai.output.type", "text")
        span.set_attribute("gen_ai.response.finish_reasons", ("stop",))
        span.set_status(Status(StatusCode.OK))
    return response


if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", "8088")))
