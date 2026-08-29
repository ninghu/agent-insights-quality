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


def _load_support_module(
    monkeypatch,
    logical_version: str = "v0",
) -> tuple[types.ModuleType, _Tracer]:
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

    package_name = f"support_observability_test_{logical_version.replace('-', '_')}"
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

    version_root = ROOT / "agents" / "support-ticket-agent"
    if logical_version == "v0":
        version_root /= "v0"
    else:
        version_root = version_root / "issues" / logical_version
    path = version_root / "source" / "app.py"
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


def test_support_whitespace_output_is_not_terminal_output_evidence(monkeypatch) -> None:
    module, tracer = _load_support_module(monkeypatch)

    async def whitespace_dispatch(_text: str, _max_output_tokens: int) -> str:
        return " \n\t"

    module.dispatch = whitespace_dispatch
    response = _invoke(module, "Read ticket-demo-1.")
    root = _root_span(tracer)
    assert response.text == " \n\t"
    assert root.attributes["aiq.terminal_response.success"] is True
    assert root.attributes["aiq.terminal_response.output_present"] is False


def test_support_partial_history_preserves_request_bound_ticket(monkeypatch) -> None:
    module, tracer = _load_support_module(monkeypatch)

    async def model_response(_prompt: str, _max_output_tokens: int) -> str:
        raise AssertionError("partial history must not depend on model paraphrase")

    module.model_response = model_response
    response = _invoke(
        module,
        "Read ticket-demo-2 while its optional history is unavailable.",
    )
    assert response.text == (
        "Ticket ID ticket-demo-2; revision 1; status open; "
        "summary Synthetic app access; optional history unavailable."
    )
    assert [
        span.name
        for span in tracer.spans
        if span.name.startswith("support.tool.")
    ] == ["support.tool.read_ticket", "support.tool.read_history"]


def test_support_issue_036_derives_two_symptoms_from_one_state_loss(
    monkeypatch,
) -> None:
    module, tracer = _load_support_module(monkeypatch, "issue-036")

    response = _invoke(
        module,
        "Update ticket-demo-2 while preserving shared revision state.",
    )
    propagation = [
        span for span in tracer.spans if span.name == "support.state.propagation"
    ]
    assert len(propagation) == 1
    assert propagation[0].attributes == {
        "state.keys_before": 2,
        "state.keys_after": 0,
    }
    failed_tools = [
        span
        for span in tracer.spans
        if span.attributes.get("tool.ok") is False
    ]
    assert [span.name for span in failed_tools] == [
        "support.tool.read_ticket",
        "support.tool.update_ticket",
    ]
    assert [span.attributes["error.type"] for span in failed_tools] == [
        "ticket_id_missing",
        "revision_missing",
    ]
    assert response.text == (
        "Shared state propagation failed: ticket routing failed because the ticket "
        "identifier was lost; ticket update failed because the revision was lost."
    )


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
