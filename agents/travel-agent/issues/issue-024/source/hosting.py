from __future__ import annotations

import json

from langchain_azure_ai.agents.hosting import ResponsesHostServer
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from .runtime_identity import FoundryRuntimeIdentity


class TravelResponsesHostServer(ResponsesHostServer):
    def __init__(self, graph, *, identity: FoundryRuntimeIdentity, **kwargs):
        self.identity = identity
        self.tracer = trace.get_tracer(identity.name, identity.version)
        super().__init__(graph, **kwargs)

    async def handle_create(self, request, context, cancellation_signal):
        # Keep the real host invocation current across all graph tasks. Callback
        # spans alone do not activate OTel context for custom async tools/models.
        with self.identity.start_span(self.tracer, "travel.invoke") as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.response.id", context.response_id)
            span.set_attribute(
                "gen_ai.input.messages",
                json.dumps(request.as_dict().get("input")),
            )
            async for event in super().handle_create(
                request, context, cancellation_signal
            ):
                if event["type"] == "response.failed":
                    span.set_status(StatusCode.ERROR)
                elif event["type"] == "response.completed":
                    span.set_attribute(
                        "gen_ai.output.messages",
                        json.dumps(event.as_dict()["response"]["output"]),
                    )
                yield event
