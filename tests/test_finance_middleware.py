from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from types import SimpleNamespace

from agent_insights_quality.live import _normalize_fixture, _trace_assertion_result
from agent_insights_quality.util import ROOT, read_json


def _load_issue_020(monkeypatch):
    agent_framework = types.ModuleType("agent_framework")

    class Agent:
        pass

    class ChatContext:
        pass

    class ChatMiddleware:
        pass

    def tool(**_kwargs):
        return lambda function: function

    agent_framework.Agent = Agent
    agent_framework.ChatContext = ChatContext
    agent_framework.ChatMiddleware = ChatMiddleware
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

    package_name = "finance_issue_020_test"
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

    path = (
        ROOT
        / "agents"
        / "finance-agent"
        / "issues"
        / "issue-020"
        / "source"
        / "app.py"
    )
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
    module = _load_issue_020(monkeypatch)
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
    module = _load_issue_020(monkeypatch)
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
