from __future__ import annotations

import json
import os

from azure.ai.agentserver.core import get_request_context
from langchain_azure_ai.agents.hosting import ResponsesHostServer
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from .runtime_identity import FoundryRuntimeIdentity


class TravelResponsesHostServer(ResponsesHostServer):
    def __init__(self, graph, *, identity: FoundryRuntimeIdentity, **kwargs):
        self.identity = identity
        self.tracer = trace.get_tracer(identity.name, identity.version)
        self._native_response_threads: dict[tuple[str | None, str], str] = {}
        super().__init__(graph, **kwargs)

    async def build_runnable_config(self, request, context):
        platform = get_request_context()
        thread_id = None
        if not context.conversation_id and request.previous_response_id:
            thread_id = self._native_response_threads.get(
                (platform.user_id, request.previous_response_id)
            )
        if (
            not context.conversation_id
            and not request.previous_response_id
            and (
                request.get("agent_session_id") or os.getenv("FOUNDRY_AGENT_SESSION_ID")
            )
            and platform.session_id
        ):
            # Native session affinity is distinct from a Responses conversation.
            # Without this binding the SDK creates a new checkpoint per response.
            thread_id = "foundry-session:" + json.dumps(
                [platform.user_id, platform.session_id], separators=(",", ":")
            )
        if thread_id is not None:
            # Preserve a stored native turn's checkpoint when a client later
            # chooses an explicit response chain. Never alias another user's state.
            if context.mode_flags.store:
                self._native_response_threads[
                    (platform.user_id, context.response_id)
                ] = thread_id
            return {"configurable": {"thread_id": thread_id}}
        return await super().build_runnable_config(request, context)

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
