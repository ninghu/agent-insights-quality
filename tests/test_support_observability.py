from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from types import SimpleNamespace

import pytest

from agent_insights_quality.util import ROOT


class _Span:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, object] = {}
        self.status = None
        self.exception: type[BaseException] | None = None

    def __enter__(self):
        return self

    def __exit__(self, error_type, _error, _traceback):
        self.exception = error_type
        return False

    def set_attribute(self, name: str, value: object) -> None:
        self.attributes[name] = value

    def set_status(self, status: object) -> None:
        self.status = status


class _Tracer:
    def __init__(self) -> None:
        self.spans: list[_Span] = []

    def start_as_current_span(self, name: str) -> _Span:
        span = _Span(name)
        self.spans.append(span)
        return span


def _load_support_module(monkeypatch) -> tuple[types.ModuleType, _Tracer]:
    tracer = _Tracer()
    trace_module = types.ModuleType("opentelemetry.trace")

    class Status:
        def __init__(self, code: str) -> None:
            self.code = code

    class StatusCode:
        ERROR = "error"
        OK = "ok"

    trace_module.get_tracer = lambda _name: tracer
    trace_module.Status = Status
    trace_module.StatusCode = StatusCode
    opentelemetry = types.ModuleType("opentelemetry")
    opentelemetry.trace = trace_module

    responses_module = types.ModuleType("azure.ai.agentserver.responses")

    class ResponsesAgentServerHost:
        @staticmethod
        def response_handler(function):
            return function

        @staticmethod
        def run(**_kwargs):
            return None

    class TextResponse:
        def __init__(self, _context, _payload, *, text: str) -> None:
            self.text = text

    responses_module.CreateResponse = dict
    responses_module.ResponseContext = object
    responses_module.ResponsesAgentServerHost = ResponsesAgentServerHost
    responses_module.TextResponse = TextResponse

    identity_module = types.ModuleType("azure.identity.aio")

    class DefaultAzureCredential:
        async def get_token(self, _scope: str):
            return SimpleNamespace(token="synthetic-token")

    identity_module.DefaultAzureCredential = DefaultAzureCredential
    openai_module = types.ModuleType("openai")
    openai_module.AsyncOpenAI = object

    package_name = "support_observability_test"
    package = types.ModuleType(package_name)
    package.__path__ = []
    observability = types.ModuleType(f"{package_name}.observability")
    observability.configure_observability = lambda _name: None

    modules = {
        "opentelemetry": opentelemetry,
        "opentelemetry.trace": trace_module,
        "azure.ai.agentserver.responses": responses_module,
        "azure.identity.aio": identity_module,
        "openai": openai_module,
        package_name: package,
        f"{package_name}.observability": observability,
    }
    for name, value in modules.items():
        monkeypatch.setitem(sys.modules, name, value)

    path = ROOT / "agents" / "support-ticket-agent" / "v0" / "source" / "app.py"
    spec = importlib.util.spec_from_file_location(f"{package_name}.app", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module, tracer


def _root_span(tracer: _Tracer) -> _Span:
    roots = [span for span in tracer.spans if span.name == "support.dispatch"]
    assert len(roots) == 1
    return roots[0]


def _invoke(module, text: str):
    return asyncio.run(
        module.responses(
            {"input": text, "max_output_tokens": 100},
            SimpleNamespace(response_id="synthetic-response"),
            None,
        )
    )


def test_support_normal_output_sets_terminal_root_signals(monkeypatch) -> None:
    module, tracer = _load_support_module(monkeypatch)

    async def model_response(_prompt: str, _max_output_tokens: int) -> str:
        return "Synthetic ticket is open; no update was dispatched."

    module.model_response = model_response
    response = _invoke(module, "Read ticket-demo-1.")
    root = _root_span(tracer)
    assert response.text
    assert root.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert root.attributes["aiq.terminal_response.success"] is True
    assert root.attributes["aiq.terminal_response.output_present"] is True
    assert all(
        "aiq.terminal_response.success" not in span.attributes
        for span in tracer.spans
        if span is not root
    )


def test_support_handled_tool_error_retains_terminal_success(monkeypatch) -> None:
    module, tracer = _load_support_module(monkeypatch)

    async def model_response(_prompt: str, _max_output_tokens: int) -> str:
        return "Synthetic read succeeded after one bounded retry."

    module.model_response = model_response
    _invoke(module, "Temporary read failure for ticket-demo-1.")
    root = _root_span(tracer)
    failed_tools = [
        span
        for span in tracer.spans
        if span.attributes.get("tool.ok") is False
    ]
    assert len(failed_tools) == 1
    assert failed_tools[0].attributes["aiq.tool.error.handled"] is True
    assert failed_tools[0].status.code == "error"
    assert root.attributes["aiq.terminal_response.success"] is True
    assert root.attributes["aiq.terminal_response.output_present"] is True


def test_support_empty_output_is_not_terminal_output_evidence(monkeypatch) -> None:
    module, tracer = _load_support_module(monkeypatch)

    async def empty_dispatch(_text: str, _max_output_tokens: int) -> str:
        return ""

    module.dispatch = empty_dispatch
    response = _invoke(module, "Read ticket-demo-1.")
    root = _root_span(tracer)
    assert response.text == ""
    assert root.attributes["aiq.terminal_response.success"] is True
    assert root.attributes["aiq.terminal_response.output_present"] is False


def test_support_unhandled_exception_never_reports_terminal_success(
    monkeypatch,
) -> None:
    module, tracer = _load_support_module(monkeypatch)

    async def failed_dispatch(_text: str, _max_output_tokens: int) -> str:
        raise RuntimeError("synthetic unhandled failure")

    module.dispatch = failed_dispatch
    with pytest.raises(RuntimeError, match="synthetic unhandled failure"):
        _invoke(module, "Read ticket-demo-1.")
    root = _root_span(tracer)
    assert root.exception is RuntimeError
    assert root.attributes["aiq.terminal_response.success"] is False
    assert root.attributes["aiq.terminal_response.output_present"] is False
