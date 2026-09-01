from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
import types
from types import SimpleNamespace

import pytest

from agent_insights_quality.live import (
    _normalize_fixture,
    _tool_rows,
    _trace_assertion_result,
)
from agent_insights_quality.util import ROOT, read_json


def _load_finance_app(monkeypatch, logical_version: str):
    agent_framework = types.ModuleType("agent_framework")
    events = []

    class Agent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run(self):
            response = AgentResponse(
                messages=[Message(role="assistant", contents=["natural response"])]
            )
            events.append(("agent_response", response))
            return response

    class ChatContext:
        pass

    class ChatMiddleware:
        pass

    class FunctionInvocationContext:
        pass

    class FunctionMiddleware:
        pass

    class Content:
        @staticmethod
        def from_text(text):
            return text

    class Message:
        def __init__(self, *, role, contents):
            self.role = role
            self.contents = contents

    class AgentResponse:
        def __init__(self, *, messages):
            self.messages = messages

    class ChatResponse:
        def __init__(self, *, messages):
            self.messages = messages

        @staticmethod
        def from_updates(_updates):
            return None

    class ChatResponseUpdate:
        def __init__(self, *, role, contents):
            self.role = role
            self.contents = contents

    class ResponseStream:
        def __init__(self, updates, *, finalizer):
            self.updates = updates
            self.finalizer = finalizer

        def __aiter__(self):
            return self.updates.__aiter__()

        async def get_final_response(self):
            updates = [update async for update in self]
            result = self.finalizer(updates)
            if inspect.isawaitable(result):
                result = await result
            return result

    class MiddlewareTermination(Exception):
        pass

    def tool(**_kwargs):
        return lambda function: function

    agent_framework.Agent = Agent
    agent_framework.AgentResponse = AgentResponse
    agent_framework.ChatContext = ChatContext
    agent_framework.ChatMiddleware = ChatMiddleware
    agent_framework.FunctionInvocationContext = FunctionInvocationContext
    agent_framework.FunctionMiddleware = FunctionMiddleware
    agent_framework.Content = Content
    agent_framework.Message = Message
    agent_framework.ChatResponse = ChatResponse
    agent_framework.ChatResponseUpdate = ChatResponseUpdate
    agent_framework.ResponseStream = ResponseStream
    agent_framework.MiddlewareTermination = MiddlewareTermination
    agent_framework.tool = tool

    foundry = types.ModuleType("agent_framework.foundry")

    class FoundryChatClient:
        def __init__(self, **_kwargs):
            pass

    foundry.FoundryChatClient = FoundryChatClient
    framework_observability = types.ModuleType("agent_framework.observability")

    def enable_instrumentation(**kwargs):
        events.append(("maf_instrumentation", kwargs))

    framework_observability.enable_instrumentation = enable_instrumentation
    hosting = types.ModuleType("agent_framework_foundry_hosting")

    class ResponsesHostServer:
        def __init__(self, agent):
            self.agent = agent
            events.append(("host_observability", agent))

        def run(self, *, port):
            events.append(("host_run", self.agent, port))
            return self.agent.run()

    hosting.ResponsesHostServer = ResponsesHostServer
    identity = types.ModuleType("azure.identity")
    identity.DefaultAzureCredential = object

    trace_module = types.ModuleType("opentelemetry.trace")
    trace_module.get_tracer = lambda *_args: object()
    opentelemetry = types.ModuleType("opentelemetry")
    opentelemetry.trace = trace_module

    pydantic = types.ModuleType("pydantic")
    pydantic.Field = lambda **_kwargs: None

    package_name = f"finance_{logical_version.replace('-', '_')}_test"
    package = types.ModuleType(package_name)
    package.__path__ = []
    observability = types.ModuleType(f"{package_name}.observability")

    def configure_observability(*args):
        events.append(("app_observability", args))

    observability.configure_observability = configure_observability
    runtime_identity = types.ModuleType(f"{package_name}.runtime_identity")

    class RuntimeIdentity:
        name = "synthetic-finance-agent"
        version = "1"

        @staticmethod
        def start_span(tracer, name):
            return tracer.start_as_current_span(name)

    runtime_identity.require_foundry_runtime_identity = RuntimeIdentity
    tools = types.ModuleType(f"{package_name}.tools")
    tools.ACCOUNTS = {
        "acct-demo-a": {"balance": 1250.5, "spend": 265.5, "currency": "USD"},
        "acct-demo-b": {"balance": 875.0, "spend": 190.0, "currency": "USD"},
    }

    modules = {
        "agent_framework": agent_framework,
        "agent_framework.foundry": foundry,
        "agent_framework.observability": framework_observability,
        "agent_framework_foundry_hosting": hosting,
        "azure.identity": identity,
        "opentelemetry": opentelemetry,
        "opentelemetry.trace": trace_module,
        "pydantic": pydantic,
        package_name: package,
        f"{package_name}.observability": observability,
        f"{package_name}.runtime_identity": runtime_identity,
        f"{package_name}.tools": tools,
    }
    for name, value in modules.items():
        monkeypatch.setitem(sys.modules, name, value)

    finance_root = ROOT / "agents" / "finance-agent"
    version_root = (
        finance_root / "v0"
        if logical_version == "v0"
        else finance_root / "issues" / logical_version
    )
    retry_path = version_root / "source" / "retry.py"
    retry_spec = importlib.util.spec_from_file_location(
        f"{package_name}.retry",
        retry_path,
    )
    assert retry_spec is not None and retry_spec.loader is not None
    retry_module = importlib.util.module_from_spec(retry_spec)
    monkeypatch.setitem(sys.modules, retry_spec.name, retry_module)
    retry_spec.loader.exec_module(retry_module)

    path = version_root / "source" / "app.py"
    spec = importlib.util.spec_from_file_location(f"{package_name}.app", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    module._test_events = events
    return module


@pytest.mark.parametrize(
    "logical_version",
    ["v0", *(f"issue-{issue_number:03}" for issue_number in range(13, 21))],
)
@pytest.mark.parametrize(
    ("sensitive_data_environment", "expected_sensitive_data"),
    [(None, False), ("true", True)],
)
def test_finance_main_enables_maf_before_runtime_construction(
    monkeypatch,
    logical_version,
    sensitive_data_environment,
    expected_sensitive_data,
) -> None:
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.invalid")
    if sensitive_data_environment is None:
        monkeypatch.delenv("ENABLE_SENSITIVE_DATA", raising=False)
    else:
        monkeypatch.setenv("ENABLE_SENSITIVE_DATA", sensitive_data_environment)
    module = _load_finance_app(monkeypatch, logical_version)

    module.main()

    events = module._test_events
    assert [event[0] for event in events] == [
        "app_observability",
        "maf_instrumentation",
        "host_observability",
        "host_run",
        "agent_response",
    ]
    assert events[1][1] == {
        "enable_sensitive_data": expected_sensitive_data,
    }
    assert events[2][1] is events[3][1]
    response = events[4][1]
    assert response.messages[0].role == "assistant"
    assert response.messages[0].contents == ["natural response"]


def _message(text: str):
    return SimpleNamespace(role="user", text=text)


def test_duplicate_context_delegates_real_model_input_and_natural_evidence(
    monkeypatch,
) -> None:
    module = _load_finance_app(monkeypatch, "issue-020")
    original = [_message("Summarize the balance and monthly items for acct-demo-a.")]
    context = SimpleNamespace(messages=list(original), result=None)
    delegated_messages = []

    async def call_next() -> None:
        delegated_messages.extend(context.messages)
        context.result = "natural-model-response"

    asyncio.run(module.DuplicateContext().process(context, call_next))

    assert context.result == "natural-model-response"
    assert delegated_messages == original * 4
    fixture = _normalize_fixture(
        read_json(
            ROOT
            / "agents"
            / "finance-agent"
            / "issues"
            / "issue-020"
            / "traffic.json"
        )["requests"][0]
    )
    input_messages = [
        {"role": message.role, "parts": [{"type": "text", "content": message.text}]}
        for message in delegated_messages
    ]
    natural_model_row = {
        "operation_name": "chat",
        "messages": [json.dumps(input_messages), ""],
    }
    assert _trace_assertion_result([natural_model_row], fixture)[0].passed is True
    authored_non_model_row = {
        "operation_name": "invoke_agent",
        "messages": [json.dumps(input_messages), ""],
    }
    assert _trace_assertion_result([authored_non_model_row], fixture)[0].passed is False


def test_duplicate_context_unmatched_request_delegates_unchanged(monkeypatch) -> None:
    module = _load_finance_app(monkeypatch, "issue-020")
    original = [_message("Show the balance for acct-demo-a.")]
    context = SimpleNamespace(messages=list(original), result=None)
    delegated_messages = []

    async def call_next() -> None:
        delegated_messages.extend(context.messages)
        context.result = "unchanged-model-response"

    asyncio.run(module.DuplicateContext().process(context, call_next))

    assert context.result == "unchanged-model-response"
    assert context.messages == original
    assert delegated_messages == original


def test_exact_transient_retry_reuses_the_same_function_and_arguments(
    monkeypatch,
) -> None:
    module = _load_finance_app(monkeypatch, "v0")
    context = SimpleNamespace(
        function=SimpleNamespace(name="get_balance_with_transient"),
        arguments={"account_id": "acct-demo-a"},
        result=None,
    )
    attempts = []
    results = [
        {
            "ok": False,
            "error": {"code": "temporary_unavailable", "retryable": True},
        },
        {
            "ok": True,
            "account_id": "acct-demo-a",
            "balance": 1250.5,
            "currency": "USD",
        },
    ]

    async def call_next() -> None:
        attempts.append((context.function.name, dict(context.arguments)))
        context.result = [SimpleNamespace(text=json.dumps(results[len(attempts) - 1]))]

    asyncio.run(module.ExactTransientRetry().process(context, call_next))

    assert attempts == [
        ("get_balance_with_transient", {"account_id": "acct-demo-a"}),
        ("get_balance_with_transient", {"account_id": "acct-demo-a"}),
    ]
    retry_module = sys.modules[module.ExactTransientRetry.__module__]
    assert retry_module.tool_result_payload(context.result) == results[-1]


def test_exact_transient_retry_does_not_retry_permanent_errors(monkeypatch) -> None:
    module = _load_finance_app(monkeypatch, "v0")
    context = SimpleNamespace(
        function=SimpleNamespace(name="get_balance"),
        arguments={"account_id": "acct-demo-missing"},
        result=None,
    )
    attempts = 0

    async def call_next() -> None:
        nonlocal attempts
        attempts += 1
        context.result = {
            "ok": False,
            "error": {"code": "account_not_found"},
        }

    asyncio.run(module.ExactTransientRetry().process(context, call_next))

    assert attempts == 1


def test_issue_019_repeats_real_matching_function_invocation_three_times(
    monkeypatch,
) -> None:
    module = _load_finance_app(monkeypatch, "issue-019")
    context = SimpleNamespace(
        function=SimpleNamespace(name="get_balance"),
        arguments={"account_id": "acct-demo-missing"},
        result=None,
    )
    attempts = []

    async def call_next() -> None:
        attempts.append((context.function.name, dict(context.arguments)))
        context.result = {
            "ok": False,
            "error": {"code": "account_not_found"},
        }

    asyncio.run(module.PermanentFailureRetryLoop().process(context, call_next))

    assert attempts == [
        ("get_balance", {"account_id": "acct-demo-missing"}),
        ("get_balance", {"account_id": "acct-demo-missing"}),
        ("get_balance", {"account_id": "acct-demo-missing"}),
    ]
    source = inspect.getsource(module.PermanentFailureRetryLoop)
    assert "start_as_current_span" not in source
    assert "ChatResponse" not in source
    assert "context.messages" not in source


def test_issue_019_leaves_nonmatching_invocations_single_attempt(monkeypatch) -> None:
    module = _load_finance_app(monkeypatch, "issue-019")
    context = SimpleNamespace(
        function=SimpleNamespace(name="get_balance"),
        arguments={"account_id": "acct-demo-a"},
        result=None,
    )
    attempts = 0

    async def call_next() -> None:
        nonlocal attempts
        attempts += 1
        context.result = {
            "ok": True,
            "account_id": "acct-demo-a",
            "balance": 1250.5,
            "currency": "USD",
        }

    asyncio.run(module.PermanentFailureRetryLoop().process(context, call_next))

    assert attempts == 1


def test_finance_tools_emit_privacy_safe_structural_telemetry(monkeypatch) -> None:
    module = _load_finance_app(monkeypatch, "v0")
    attributes = {}
    module.trace.get_current_span = lambda: SimpleNamespace(
        set_attribute=lambda key, value: attributes.__setitem__(key, value)
    )
    module.finish_tool_span(
        "get_balance",
        {
            "ok": True,
            "account_id": "acct-demo-a",
            "balance": 1250.5,
            "currency": "USD",
            "private_detail": "excluded",
        },
    )
    assert json.loads(attributes["aiq.tool.call.arguments"]) == {
        "account_id": "acct-demo-a"
    }
    assert json.loads(attributes["aiq.tool.call.result"]) == {
        "ok": True,
        "account_id": "acct-demo-a",
        "balance": 1250.5,
        "currency": "USD",
    }
    assert "private_detail" not in attributes["aiq.tool.call.result"]
    assert _tool_rows(
        [
            {
                "operation_name": "execute_tool",
                "tool_name": "get_balance",
                "structural_tool": "",
            },
            {
                "operation_name": "execute_tool",
                "tool_name": "get_balance",
                "structural_tool": attributes["aiq.tool.call.result"],
            },
        ],
        "get_balance",
    ) == [
        {
            "operation_name": "execute_tool",
            "tool_name": "get_balance",
            "structural_tool": attributes["aiq.tool.call.result"],
        }
    ]


@pytest.mark.parametrize(
    ("issue_id", "class_name", "request_text"),
    [
        ("issue-013", "ContradictedBalance", "Show the balance for acct-demo-a."),
        (
            "issue-016",
            "StructuredErrorAsBalance",
            "For acct-demo-missing, preserve the tool error.",
        ),
        (
            "issue-017",
            "CompletePartialAggregate",
            "Give the complete budget summary for acct-demo-a and acct-demo-missing.",
        ),
    ],
)
def test_finance_output_defects_run_real_pipeline_before_postprocessing(
    monkeypatch,
    issue_id,
    class_name,
    request_text,
) -> None:
    module = _load_finance_app(monkeypatch, issue_id)
    context = SimpleNamespace(
        messages=[_message(request_text)],
        result=None,
        stream=False,
    )
    calls = []

    async def call_next() -> None:
        calls.append("real-model-tool-pipeline")
        context.result = "real-response"

    with pytest.raises(module.MiddlewareTermination):
        asyncio.run(getattr(module, class_name)().process(context, call_next))
    assert calls == ["real-model-tool-pipeline"]
    source = inspect.getsource(getattr(module, class_name))
    assert "start_span" not in source
    assert "gen_ai." not in source


def test_issue_016_consumes_tool_dispatch_before_stream_replacement(
    monkeypatch,
) -> None:
    module = _load_finance_app(monkeypatch, "issue-016")
    context = SimpleNamespace(
        messages=[_message("For acct-demo-missing, preserve the tool error.")],
        result=None,
        stream=True,
    )
    tool_calls = []

    def get_balance(account_id):
        tool_calls.append(account_id)
        return {"ok": False, "error": {"code": "account_not_found"}}

    monkeypatch.setattr(module, "get_balance", get_balance)

    async def call_next() -> None:
        async def downstream_updates():
            result = module.get_balance("acct-demo-missing")
            yield module.ChatResponseUpdate(
                role="assistant",
                contents=[module.Content.from_text(json.dumps(result))],
            )

        context.result = module.ResponseStream(
            downstream_updates(),
            finalizer=module.ChatResponse.from_updates,
        )

    async def run_middleware():
        with pytest.raises(module.MiddlewareTermination):
            await module.StructuredErrorAsBalance().process(context, call_next)
        return [update async for update in context.result]

    replacement_updates = asyncio.run(run_middleware())

    assert tool_calls == ["acct-demo-missing"]
    assert replacement_updates[0].contents == [
        "The successful balance is account_not_found."
    ]


def test_issue_016_activation_is_absent_from_baseline(monkeypatch) -> None:
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.invalid")
    baseline = _load_finance_app(monkeypatch, "v0")

    assert not hasattr(baseline, "StructuredErrorAsBalance")
    assert [
        type(middleware).__name__
        for middleware in baseline.build_agent().kwargs["middleware"]
    ] == ["ExactTransientRetry"]


def test_issue_014_removes_argument_from_real_function_invocation(
    monkeypatch,
) -> None:
    module = _load_finance_app(monkeypatch, "issue-014")
    context = SimpleNamespace(
        function=SimpleNamespace(name="get_balance"),
        arguments={"account_id": "acct-demo-a"},
        result=None,
    )
    observed = []

    async def call_next() -> None:
        observed.append(dict(context.arguments))

    asyncio.run(module.MissingAccountIdentifier().process(context, call_next))
    assert observed == [{}]
    assert "start_span" not in inspect.getsource(module.MissingAccountIdentifier)


def test_issue_015_swaps_scope_on_real_function_invocation(monkeypatch) -> None:
    module = _load_finance_app(monkeypatch, "issue-015")
    context = SimpleNamespace(
        function=SimpleNamespace(name="get_balance"),
        arguments={"account_id": "acct-demo-a"},
        result=None,
    )
    observed = []

    async def call_next() -> None:
        observed.append(dict(context.arguments))

    asyncio.run(module.OppositeAccountScope().process(context, call_next))
    assert observed == [{"account_id": "acct-demo-b"}]
    assert "start_span" not in inspect.getsource(module.OppositeAccountScope)


def test_issue_018_executes_real_tool_once_without_retry_wrapper(
    monkeypatch,
) -> None:
    module = _load_finance_app(monkeypatch, "issue-018")
    context = SimpleNamespace(
        function=SimpleNamespace(name="get_balance_with_transient"),
        arguments={"account_id": "acct-demo-a"},
        result=None,
    )
    calls = []

    async def call_next() -> None:
        calls.append(dict(context.arguments))

    asyncio.run(module.MissingTransientRetry().process(context, call_next))
    assert calls == [{"account_id": "acct-demo-a"}]
    source = (
        ROOT
        / "agents"
        / "finance-agent"
        / "issues"
        / "issue-018"
        / "source"
        / "app.py"
    ).read_text(encoding="utf-8")
    build = source[source.index("def build_agent") :]
    assert "ExactTransientRetry()" not in build
