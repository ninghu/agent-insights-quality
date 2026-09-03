from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from agent_insights_quality.util import ROOT


class _Message:
    def __init__(self, content: str):
        self.content = content


class _Span:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def set_attribute(self, *_args):
        return None


class _RuntimeIdentity:
    name = "synthetic-travel-agent"
    version = "v0"

    def start_span(self, *_args):
        return _Span()


class _StateGraph:
    def __init__(self, _schema):
        self.nodes = {}

    def add_node(self, name, node):
        self.nodes[name] = node

    def add_edge(self, *_args):
        return None

    def compile(self, **_kwargs):
        return self


def _module(monkeypatch, name: str, **values) -> ModuleType:
    module = ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def _load_travel_app(monkeypatch, source: Path):
    package_name = "travel_runtime_under_test"
    package = _module(monkeypatch, package_name)
    package.__path__ = [str(source)]

    options_spec = importlib.util.spec_from_file_location(
        f"{package_name}.options",
        source / "options.py",
    )
    assert options_spec is not None
    assert options_spec.loader is not None
    options = importlib.util.module_from_spec(options_spec)
    monkeypatch.setitem(sys.modules, options_spec.name, options)
    options_spec.loader.exec_module(options)

    _module(monkeypatch, "azure")
    _module(monkeypatch, "azure.identity")
    _module(
        monkeypatch,
        "azure.identity.aio",
        DefaultAzureCredential=lambda: object(),
    )
    _module(monkeypatch, "langchain_core")
    _module(
        monkeypatch,
        "langchain_core.messages",
        AIMessage=_Message,
        AnyMessage=_Message,
    )
    _module(monkeypatch, "langchain_azure_ai")
    _module(monkeypatch, "langchain_azure_ai.agents")
    _module(
        monkeypatch,
        "langchain_azure_ai.agents.hosting",
        ResponsesHostServer=object,
    )
    _module(monkeypatch, "langgraph")
    _module(monkeypatch, "langgraph.checkpoint")
    _module(
        monkeypatch,
        "langgraph.checkpoint.memory",
        InMemorySaver=object,
    )
    _module(
        monkeypatch,
        "langgraph.graph",
        END="end",
        START="start",
        StateGraph=_StateGraph,
    )
    _module(monkeypatch, "langgraph.graph.message", add_messages=object())
    _module(
        monkeypatch,
        "opentelemetry",
        trace=SimpleNamespace(get_tracer=lambda *_args: object()),
    )
    _module(monkeypatch, "openai", AsyncOpenAI=object)
    _module(
        monkeypatch,
        f"{package_name}.observability",
        configure_observability=lambda *_args: None,
    )
    _module(
        monkeypatch,
        f"{package_name}.runtime_identity",
        require_foundry_runtime_identity=lambda: _RuntimeIdentity(),
    )

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.app",
        source / "app.py",
    )
    assert spec is not None
    assert spec.loader is not None
    app = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, app)
    spec.loader.exec_module(app)
    return app


def test_travel_baseline_renders_reviewed_facts_after_model_review(
    monkeypatch,
) -> None:
    source = ROOT / "agents" / "travel-agent" / "v0" / "source"
    app = _load_travel_app(monkeypatch, source)
    review_prompts = []

    async def review_answer(prompt: str) -> str:
        review_prompts.append(prompt)
        return "The model omitted and paraphrased the reviewed facts."

    app.review_answer = review_answer
    graph = app.build_graph()

    async def invoke() -> str:
        state = {
            "messages": [
                _Message("Compare flight and hotel for trip-gamma.")
            ]
        }
        for node_name in ("plan", "search", "validate", "book", "respond"):
            update = await graph.nodes[node_name](state)
            if "messages" in update:
                state["messages"].extend(update.pop("messages"))
            state.update(update)
        return state["messages"][-1].content

    output = asyncio.run(invoke())
    expected = (
        "Itinerary trip-gamma; Flight flight-demo-0 for trip-gamma: "
        "carrier Contoso Air, departure 09:00, price USD 200; Hotel "
        "hotel-demo-0 for trip-gamma: property Fabrikam Stay, rating 4.5, "
        "nightly rate USD 120. Booking not completed. Showing 2 of 4 "
        "synthetic options."
    )
    traffic = json.loads(
        (source.parent / "traffic.json").read_text(encoding="utf-8")
    )
    ordinary = next(
        request
        for request in traffic["requests"]
        if request["id"] == "travel-agent-v0-ordinary"
    )
    assertions = ordinary["expected"]["semantic_assertions"]

    assert output == expected
    assert all(term in output for term in assertions["required_terms_all"])
    assert all(term not in output for term in assertions["forbidden_terms"])
    assert review_prompts == [
        "Review this deterministic synthetic travel response for concision: "
        + expected
    ]


def test_every_travel_authority_uses_deterministic_terminal_rendering() -> None:
    sources = sorted(
        (ROOT / "agents" / "travel-agent").glob("**/source/app.py")
    )

    assert len(sources) == 9
    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert text.count("await review_answer(") == 1
        assert text.count(
            'return {"messages": [AIMessage(content=answer)]}'
        ) == 1
        assert "AIMessage(content=grounded)" not in text
        assert "result.output_text" not in text
