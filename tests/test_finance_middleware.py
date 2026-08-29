from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
import types
from types import SimpleNamespace

from agent_insights_quality.live import _normalize_fixture, _trace_assertion_result
from agent_insights_quality.util import ROOT, read_json


def _load_finance_app(monkeypatch, logical_version: str):
    agent_framework = types.ModuleType("agent_framework")

    class Agent:
        pass

    class ChatContext:
        pass

    class ChatMiddleware:
        pass

    class FunctionInvocationContext:
        pass

    class FunctionMiddleware:
        pass

    def tool(**_kwargs):
        return lambda function: function

    agent_framework.Agent = Agent
    agent_framework.ChatContext = ChatContext
    agent_framework.ChatMiddleware = ChatMiddleware
    agent_framework.FunctionInvocationContext = FunctionInvocationContext
    agent_framework.FunctionMiddleware = FunctionMiddleware
    agent_framework.tool = tool

    foundry = types.ModuleType("agent_framework.foundry")
    foundry.FoundryChatClient = object
    hosting = types.ModuleType("agent_framework_foundry_hosting")
    hosting.ResponsesHostServer = object
    identity = types.ModuleType("azure.identity")
    identity.DefaultAzureCredential = object

    trace_module = types.ModuleType("opentelemetry.trace")
    trace_module.get_tracer = lambda _name: object()
    opentelemetry = types.ModuleType("opentelemetry")
    opentelemetry.trace = trace_module

    pydantic = types.ModuleType("pydantic")
    pydantic.Field = lambda **_kwargs: None

    package_name = f"finance_{logical_version.replace('-', '_')}_test"
    package = types.ModuleType(package_name)
    package.__path__ = []
    observability = types.ModuleType(f"{package_name}.observability")
    observability.configure_observability = lambda _name: None
    tools = types.ModuleType(f"{package_name}.tools")
    tools.ACCOUNTS = {
        "acct-demo-a": {"balance": 1250.5, "spend": 265.5, "currency": "USD"},
        "acct-demo-b": {"balance": 875.0, "spend": 190.0, "currency": "USD"},
    }

    modules = {
        "agent_framework": agent_framework,
        "agent_framework.foundry": foundry,
        "agent_framework_foundry_hosting": hosting,
        "azure.identity": identity,
        "opentelemetry": opentelemetry,
        "opentelemetry.trace": trace_module,
        "pydantic": pydantic,
        package_name: package,
        f"{package_name}.observability": observability,
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
    return module


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
