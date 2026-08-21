from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from scenario_runtime import ScenarioRuntime


_MAX_TOOL_TURNS = 4
_TRACER = trace.get_tracer("agent_insights_quality.hosted_agent")


class ModelBackedAgent:
    def __init__(
        self,
        *,
        instructions: str,
        tools: list[dict[str, Any]],
        execute_tool: Callable[[str, dict[str, Any]], str],
    ) -> None:
        self._instructions = instructions
        self._tools = tools
        self._execute_tool = execute_tool
        self._scenario = ScenarioRuntime()
        if self._scenario.instructions:
            self._instructions = f"{self._instructions}\n\n{self._scenario.instructions}"
        self._client: Any | None = None
        self._client_lock = threading.Lock()

    def respond(self, user_input: str) -> str:
        self._scenario.before_request()
        response = self._model_response(input=user_input)
        for _ in range(_MAX_TOOL_TURNS):
            calls = [
                item
                for item in (getattr(response, "output", None) or [])
                if _enum_text(getattr(item, "type", "")) == "function_call"
            ]
            if not calls:
                output_text = str(getattr(response, "output_text", "") or "").strip()
                if not output_text:
                    raise RuntimeError("The model completed without output text.")
                return self._scenario.finalize_output(output_text)
            outputs = []
            for call in calls:
                name = str(getattr(call, "name", "") or "")
                call_id = str(getattr(call, "call_id", "") or "")
                raw_arguments = str(getattr(call, "arguments", "") or "")
                if not name or not call_id or not raw_arguments:
                    raise RuntimeError("The model returned an incomplete function call.")
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise RuntimeError("Tool arguments must be a JSON object.")
                result = self._run_tool(name, arguments)
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": result,
                    }
                )
            prior_output = [
                _response_item(item)
                for item in (getattr(response, "output", None) or [])
            ]
            response = self._model_response(
                input=[*prior_output, *outputs],
            )
        raise RuntimeError("The model exceeded the bounded function-call turn limit.")

    def _model_response(self, **kwargs: Any) -> Any:
        self._scenario.before_model()
        model = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"].strip()
        if not model:
            raise RuntimeError("AZURE_AI_MODEL_DEPLOYMENT_NAME is empty.")
        with _TRACER.start_as_current_span(
            "model.responses.create",
            kind=SpanKind.CLIENT,
        ) as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.request.model", model)
            response = self._openai_client().responses.create(
                model=model,
                instructions=self._instructions,
                tools=self._tools,
                store=False,
                **kwargs,
            )
            span.set_status(Status(StatusCode.OK))
            return response

    def _run_tool(self, name: str, arguments: dict[str, Any]) -> str:
        with _TRACER.start_as_current_span(
            f"tool.{name}",
            kind=SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("tool.name", name)
            span.set_attribute("gen_ai.tool.name", name)
            span.set_attribute(
                "tool.arguments",
                json.dumps(arguments, sort_keys=True, separators=(",", ":")),
            )
            try:
                result = self._scenario.run_tool(
                    name, arguments, self._make_dispatch(name)
                )
            except (KeyError, TypeError, ValueError) as error:
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR, str(error)))
                raise
            span.set_attribute("tool.result", result)
            span.set_status(Status(StatusCode.OK))
            return result

    def _make_dispatch(self, effective_name: str) -> Callable[[str, dict[str, Any]], str]:
        """Return a callable that wraps each actual tool dispatch in its own span."""

        def _dispatch(dispatch_name: str, dispatch_args: dict[str, Any]) -> str:
            span_name = f"tool.dispatch.{dispatch_name}"
            with _TRACER.start_as_current_span(span_name, kind=SpanKind.INTERNAL) as dspan:
                dspan.set_attribute("tool.name", dispatch_name)
                dspan.set_attribute("gen_ai.tool.name", dispatch_name)
                dspan.set_attribute(
                    "tool.arguments",
                    json.dumps(dispatch_args, sort_keys=True, separators=(",", ":")),
                )
                try:
                    dispatch_result = self._execute_tool(dispatch_name, dispatch_args)
                except Exception as error:
                    dspan.record_exception(error)
                    dspan.set_status(Status(StatusCode.ERROR, str(error)))
                    raise
                dspan.set_attribute("tool.result", dispatch_result)
                dspan.set_status(Status(StatusCode.OK))
                return dispatch_result

        return _dispatch

    def _openai_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                endpoint = os.environ["AZURE_AI_PROJECT_ENDPOINT"].strip()
                if not endpoint:
                    raise RuntimeError("AZURE_AI_PROJECT_ENDPOINT is empty.")
                project = AIProjectClient(
                    endpoint=endpoint,
                    credential=DefaultAzureCredential(),
                )
                self._client = project.get_openai_client(max_retries=0)
        return self._client


def _enum_text(value: object) -> str:
    return str(getattr(value, "value", value) or "").casefold()


def _response_item(value: object) -> dict[str, Any]:
    model_dump = getattr(value, "model_dump", None)
    if not callable(model_dump):
        raise RuntimeError("The model returned an unsupported response item.")
    item = model_dump(exclude_none=True)
    if not isinstance(item, dict):
        raise RuntimeError("The model returned a non-object response item.")
    return item
