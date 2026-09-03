from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
import types
from types import SimpleNamespace

import pytest
from agent_framework import (
    BaseChatClient as FrameworkBaseChatClient,
    ChatMiddlewareLayer as FrameworkChatMiddlewareLayer,
    ChatResponse as FrameworkChatResponse,
    ChatResponseUpdate as FrameworkChatResponseUpdate,
    Content as FrameworkContent,
    FunctionInvocationLayer as FrameworkFunctionInvocationLayer,
    Message as FrameworkMessage,
    ResponseStream as FrameworkResponseStream,
)

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


def _load_finance_app_with_framework(monkeypatch, logical_version: str):
    finance_root = ROOT / "agents" / "finance-agent"
    version_root = (
        finance_root / "v0"
        if logical_version == "v0"
        else finance_root / "issues" / logical_version
    )
    package_name = f"finance_{logical_version.replace('-', '_')}_framework_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(version_root / "source")]

    foundry = types.ModuleType("agent_framework.foundry")
    foundry.FoundryChatClient = object
    hosting = types.ModuleType("agent_framework_foundry_hosting")
    hosting.ResponsesHostServer = object
    identity = types.ModuleType("azure.identity")
    identity.DefaultAzureCredential = object
    observability = types.ModuleType(f"{package_name}.observability")
    observability.configure_observability = lambda *_args: None

    monkeypatch.setenv("FOUNDRY_AGENT_NAME", "synthetic-finance-agent")
    monkeypatch.setenv("FOUNDRY_AGENT_VERSION", logical_version)
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, "agent_framework.foundry", foundry)
    monkeypatch.setitem(sys.modules, "agent_framework_foundry_hosting", hosting)
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    monkeypatch.setitem(sys.modules, f"{package_name}.observability", observability)

    path = version_root / "source" / "app.py"
    spec = importlib.util.spec_from_file_location(f"{package_name}.app", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
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


class _ScriptedFinanceClient(
    FrameworkFunctionInvocationLayer,
    FrameworkChatMiddlewareLayer,
    FrameworkBaseChatClient,
):
    def __init__(
        self,
        events,
        *,
        account_id,
        expected_result,
        natural_response,
    ):
        self.events = events
        self.account_id = account_id
        self.expected_result = expected_result
        self.natural_response = natural_response
        super().__init__()

    def _inner_get_response(self, *, messages, stream, options, **_kwargs):
        assert stream is True
        function_results = [
            content
            for message in messages
            for content in message.contents
            if content.type == "function_result"
            and content.call_id == "synthetic-call"
        ]
        if function_results:
            result = json.loads(function_results[0].result)
            assert result == self.expected_result
            event = ("natural_model_response", self.natural_response)
            content = FrameworkContent.from_text(self.natural_response)
        else:
            event = (
                "model_function_call",
                "get_balance",
                {"account_id": self.account_id},
            )
            content = FrameworkContent.from_function_call(
                "synthetic-call",
                "get_balance",
                arguments={"account_id": self.account_id},
            )

        async def updates():
            self.events.append(event)
            yield FrameworkChatResponseUpdate(
                role="assistant",
                contents=[content],
            )

        return FrameworkResponseStream(
            updates(),
            finalizer=FrameworkChatResponse.from_updates,
        )


class _ScriptedBudgetClient(
    FrameworkFunctionInvocationLayer,
    FrameworkChatMiddlewareLayer,
    FrameworkBaseChatClient,
):
    def __init__(self, events):
        self.events = events
        super().__init__()

    def _inner_get_response(self, *, messages, stream, options, **_kwargs):
        assert stream is True
        function_results = {
            content.call_id: json.loads(content.result)
            for message in messages
            for content in message.contents
            if content.type == "function_result"
        }
        if function_results:
            assert {"known-budget", "missing-budget"} <= set(function_results)
            assert function_results["known-budget"]["ok"] is True
            assert function_results["known-budget"]["account_id"] == "acct-demo-a"
            assert function_results["missing-budget"] == {
                "ok": False,
                "account_id": "acct-demo-missing",
                "error": {"code": "account_not_found"},
            }
            if "monthly-items" in function_results:
                assert function_results["monthly-items"]["ok"] is True
                event = (
                    "natural_model_response",
                    "The partial budget summary is ready.",
                )
                contents = [FrameworkContent.from_text(event[1])]
            else:
                event = ("model_follow_up_function_call", "list_monthly_items")
                contents = [
                    FrameworkContent.from_function_call(
                        "monthly-items",
                        "list_monthly_items",
                        arguments={"account_id": "acct-demo-a"},
                    )
                ]
        else:
            event = ("model_function_calls", "get_budget_summary")
            contents = [
                FrameworkContent.from_function_call(
                    "known-budget",
                    "get_budget_summary",
                    arguments={"account_id": "acct-demo-a"},
                ),
                FrameworkContent.from_function_call(
                    "missing-budget",
                    "get_budget_summary",
                    arguments={"account_id": "acct-demo-missing"},
                ),
            ]

        async def updates():
            self.events.append(event)
            yield FrameworkChatResponseUpdate(
                role="assistant",
                contents=contents,
            )

        return FrameworkResponseStream(
            updates(),
            finalizer=FrameworkChatResponse.from_updates,
        )


def _run_finance_framework_pipeline(
    monkeypatch,
    logical_version: str,
    *,
    account_id="acct-demo-missing",
    expected_result=None,
    natural_response="The account was not found.",
    request_text="Show the balance for acct-demo-missing and preserve the tool error.",
):
    module = _load_finance_app_with_framework(monkeypatch, logical_version)
    events = []
    expected_result = expected_result or {
        "ok": False,
        "account_id": "acct-demo-missing",
        "error": {"code": "account_not_found"},
    }
    client = _ScriptedFinanceClient(
        events,
        account_id=account_id,
        expected_result=expected_result,
        natural_response=natural_response,
    )
    original_finish_tool_span = module.finish_tool_span

    def record_tool_execution(name, result, account_id=None):
        events.append(
            (
                "tool_execution",
                name,
                account_id or result.get("account_id"),
                result.get("error", {}).get("code"),
            )
        )
        return original_finish_tool_span(name, result, account_id)

    monkeypatch.setattr(module, "finish_tool_span", record_tool_execution)
    monkeypatch.setattr(module, "FoundryChatClient", lambda **_kwargs: client)
    monkeypatch.setattr(module, "DefaultAzureCredential", lambda: object())
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.invalid")
    agent = module.build_agent()
    prior_result = {
        "ok": False,
        "account_id": "acct-demo-missing",
        "error": {"code": "account_not_found"},
    }
    messages = [
        FrameworkMessage(
            role="user",
            contents=["Earlier, preserve the tool error for acct-demo-missing."],
        ),
        FrameworkMessage(
            role="assistant",
            contents=[
                FrameworkContent.from_function_call(
                    "prior-call",
                    "get_balance",
                    arguments={"account_id": "acct-demo-missing"},
                )
            ],
        ),
        FrameworkMessage(
            role="tool",
            contents=[
                FrameworkContent.from_function_result(
                    "prior-call",
                    result=prior_result,
                )
            ],
        ),
        FrameworkMessage(
            role="user",
            contents=[request_text],
        ),
    ]

    async def run_agent():
        response = await agent.run(
            messages,
            stream=True,
        ).get_final_response()
        events.append(("final_response", response.text))
        return response

    return asyncio.run(run_agent()), events


def test_issue_017_dispatches_both_tools_before_claiming_complete(
    monkeypatch,
) -> None:
    module = _load_finance_app_with_framework(monkeypatch, "issue-017")
    events = []
    client = _ScriptedBudgetClient(events)
    original_finish_tool_span = module.finish_tool_span

    def record_tool_execution(name, result, account_id=None):
        events.append(
            (
                "tool_execution",
                name,
                account_id or result.get("account_id"),
                result.get("ok"),
            )
        )
        return original_finish_tool_span(name, result, account_id)

    monkeypatch.setattr(module, "finish_tool_span", record_tool_execution)
    monkeypatch.setattr(module, "FoundryChatClient", lambda **_kwargs: client)
    monkeypatch.setattr(module, "DefaultAzureCredential", lambda: object())
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.invalid")
    agent = module.build_agent()

    async def run_agent():
        response = await agent.run(
            "Give the complete budget summary for acct-demo-a and acct-demo-missing.",
            stream=True,
        ).get_final_response()
        events.append(("final_response", response.text))
        return response

    response = asyncio.run(run_agent())

    assert response.text == (
        "The complete budget summary covers acct-demo-a and acct-demo-missing."
    )
    assert events[0] == ("model_function_calls", "get_budget_summary")
    assert set(events[1:3]) == {
        ("tool_execution", "get_budget_summary", "acct-demo-a", True),
        ("tool_execution", "get_budget_summary", "acct-demo-missing", False),
    }
    assert events[3] == ("model_follow_up_function_call", "list_monthly_items")
    assert events[4] == (
        "tool_execution",
        "list_monthly_items",
        "acct-demo-a",
        True,
    )
    assert events[5:] == [
        ("natural_model_response", "The partial budget summary is ready."),
        ("final_response", response.text),
    ]


def test_issue_017_leaves_malformed_tool_results_on_the_natural_path(
    monkeypatch,
) -> None:
    module = _load_finance_app(monkeypatch, "issue-017")
    user = SimpleNamespace(
        role="user",
        text="Give the complete budget summary for acct-demo-a and acct-demo-missing.",
        contents=[],
    )
    calls = SimpleNamespace(
        role="assistant",
        text="",
        contents=[
            SimpleNamespace(
                type="function_call",
                call_id="known-budget",
                name="get_budget_summary",
                parse_arguments=lambda: {"account_id": "acct-demo-a"},
            ),
            SimpleNamespace(
                type="function_call",
                call_id="missing-budget",
                name="get_budget_summary",
                parse_arguments=lambda: {"account_id": "acct-demo-missing"},
            ),
        ],
    )
    results = SimpleNamespace(
        role="tool",
        text="",
        contents=[
            SimpleNamespace(
                type="function_result",
                call_id="known-budget",
                result=json.dumps({"ok": True, "account_id": "acct-demo-a"}),
            ),
            SimpleNamespace(
                type="function_result",
                call_id="missing-budget",
                result="Error: Function failed.",
            ),
        ],
    )
    context = SimpleNamespace(
        messages=[user, calls, results],
        result=None,
        stream=False,
    )

    async def call_next() -> None:
        context.result = module.ChatResponse(
            messages=[
                module.Message(
                    role="assistant",
                    contents=["The budget lookup failed."],
                )
            ]
        )

    asyncio.run(
        module.CompletePartialAggregate().process(context, call_next)
    )

    assert context.result.messages[0].contents == ["The budget lookup failed."]


@pytest.mark.parametrize(
    ("account_id", "balance", "spend", "contradicted_balance"),
    [
        ("acct-demo-a", 1250.5, 430.25, 1750.5),
        ("acct-demo-b", 875.0, 210.0, 1375.0),
    ],
)
def test_issue_013_real_framework_contradicts_completed_balance_result(
    monkeypatch,
    account_id,
    balance,
    spend,
    contradicted_balance,
) -> None:
    expected_result = {
        "ok": True,
        "account_id": account_id,
        "balance": balance,
        "currency": "USD",
        "spend": spend,
    }
    natural_response = (
        f"The authoritative balance for {account_id} is USD {balance:.2f}."
    )
    contradicted_response = (
        f"The authoritative balance for {account_id} is "
        f"USD {contradicted_balance:.2f}."
    )
    expected_pipeline = [
        (
            "model_function_call",
            "get_balance",
            {"account_id": account_id},
        ),
        (
            "tool_execution",
            "get_balance",
            account_id,
            None,
        ),
        ("natural_model_response", natural_response),
    ]
    for logical_version, expected_text in (
        ("issue-013", contradicted_response),
        ("v0", natural_response),
    ):
        response, events = _run_finance_framework_pipeline(
            monkeypatch,
            logical_version,
            account_id=account_id,
            expected_result=expected_result,
            natural_response=natural_response,
            request_text=f"Show the balance for {account_id}.",
        )
        assert response.text == expected_text
        assert events == [
            *expected_pipeline,
            ("final_response", expected_text),
        ]


def test_issue_016_real_framework_dispatches_tool_before_replacement(
    monkeypatch,
) -> None:
    expected_pipeline = [
        (
            "model_function_call",
            "get_balance",
            {"account_id": "acct-demo-missing"},
        ),
        (
            "tool_execution",
            "get_balance",
            "acct-demo-missing",
            "account_not_found",
        ),
        ("natural_model_response", "The account was not found."),
    ]
    for logical_version, expected_text in (
        ("issue-016", "The successful balance is account_not_found."),
        ("v0", "The account was not found."),
    ):
        response, events = _run_finance_framework_pipeline(
            monkeypatch,
            logical_version,
        )
        assert response.text == expected_text
        assert events == [*expected_pipeline, ("final_response", expected_text)]


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


def test_issue_014_real_framework_reports_omission_after_tool_dispatch(
    monkeypatch,
) -> None:
    issue_result = {
        "ok": False,
        "error": {"code": "account_id_required"},
    }
    issue_response, issue_events = _run_finance_framework_pipeline(
        monkeypatch,
        "issue-014",
        account_id="acct-demo-a",
        expected_result=issue_result,
        natural_response="The balance lookup failed.",
        request_text="Show the balance for acct-demo-a.",
    )
    expected_issue_text = (
        "The balance lookup failed because account_id was omitted."
    )
    assert issue_response.text == expected_issue_text
    assert issue_events == [
        ("model_function_call", "get_balance", {"account_id": "acct-demo-a"}),
        ("tool_execution", "get_balance", None, "account_id_required"),
        ("natural_model_response", "The balance lookup failed."),
        ("final_response", expected_issue_text),
    ]

    baseline_result = {
        "ok": True,
        "account_id": "acct-demo-a",
        "balance": 1250.5,
        "currency": "USD",
        "spend": 430.25,
    }
    baseline_response, baseline_events = _run_finance_framework_pipeline(
        monkeypatch,
        "v0",
        account_id="acct-demo-a",
        expected_result=baseline_result,
        natural_response="The balance is USD 1250.50.",
        request_text="Show the balance for acct-demo-a.",
    )
    assert baseline_response.text == "The balance is USD 1250.50."
    assert baseline_events == [
        ("model_function_call", "get_balance", {"account_id": "acct-demo-a"}),
        ("tool_execution", "get_balance", "acct-demo-a", None),
        ("natural_model_response", "The balance is USD 1250.50."),
        ("final_response", "The balance is USD 1250.50."),
    ]


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
    assert "middleware.append(StopAfterTransientFailure())" in build


def test_issue_018_disables_dispatch_after_matching_transient_result(
    monkeypatch,
) -> None:
    module = _load_finance_app(monkeypatch, "issue-018")
    call_id = "synthetic-call"
    tools = ["get_balance_with_transient", "get_balance"]
    context = SimpleNamespace(
        messages=[
            SimpleNamespace(
                contents=[
                    SimpleNamespace(
                        type="function_result",
                        call_id=call_id,
                        items=[
                            SimpleNamespace(
                                text=json.dumps(
                                    {
                                        "ok": False,
                                        "error": {
                                            "code": "temporary_unavailable",
                                            "retryable": True,
                                        },
                                    }
                                )
                            )
                        ],
                    )
                ]
            ),
        ],
        options={"tools": tools, "tool_choice": "auto"},
    )
    outgoing = []

    async def call_next() -> None:
        outgoing.append(dict(context.options))

    asyncio.run(module.StopAfterTransientFailure().process(context, call_next))

    assert outgoing == [{"tools": tools, "tool_choice": "none"}]
    assert outgoing[0]["tools"]


def test_issue_018_keeps_dispatch_for_nonmatching_latest_turn(monkeypatch) -> None:
    module = _load_finance_app(monkeypatch, "issue-018")
    context = SimpleNamespace(
        messages=[SimpleNamespace(contents=[])],
        options={"tools": ["get_balance_with_transient"], "tool_choice": "auto"},
    )

    async def call_next() -> None:
        pass

    asyncio.run(module.StopAfterTransientFailure().process(context, call_next))

    assert context.options["tool_choice"] == "auto"
    assert context.options["tools"] == ["get_balance_with_transient"]
