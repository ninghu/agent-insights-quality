"""Explicit offline tests of real MAF/Foundry dispatch and in-memory OTel.

Run with the Finance pinned requirements plus pytest. Only the model HTTP transport
is scripted; no model reasoning, deployed hosting, or telemetry export is qualified.
"""

import asyncio
import importlib
import importlib.util
import json
import socket
import sys
from collections import Counter, deque
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from agent_framework.foundry import FoundryChatClient
from agent_framework.observability import enable_instrumentation
from openai import AsyncOpenAI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


ROOT = Path(__file__).resolve().parents[2] / "agents" / "finance-agent"
VERSIONS = ["v0", *(f"issue-{number:03d}" for number in range(13, 21))]


@pytest.fixture(scope="module")
def telemetry():
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    enable_instrumentation(enable_sensitive_data=True, force=True)
    return exporter


@pytest.fixture(autouse=True)
def offline(monkeypatch, telemetry):
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", "finance-local-test")
    monkeypatch.setenv("FOUNDRY_AGENT_VERSION", "synthetic-version")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    telemetry.clear()

    def deny_external(original):
        def connect(sock, address):
            # Windows asyncio creates a loopback socket pair for its wake-up pipe.
            if address[0] in ("127.0.0.1", "::1"):
                return original(sock, address)
            raise AssertionError("Finance integration tests must not open network connections")
        return connect

    monkeypatch.setattr(socket.socket, "connect", deny_external(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", deny_external(socket.socket.connect_ex))


def load_app(version):
    directory = ROOT / "v0" if version == "v0" else ROOT / "issues" / version
    name = "finance_hosted_" + version.replace("-", "_")
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, directory / "source" / "__init__.py",
            submodule_search_locations=[str(directory / "source")],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{name}.app")


def calls(*lookups, prefix="lookup"):
    return [
        {"id": f"fc-{prefix}-{index}", "call_id": f"{prefix}-{index}", "type": "function_call",
         "name": name, "arguments": json.dumps({"account_id": account}), "status": "completed"}
        for index, (name, account) in enumerate(lookups)
    ]


def answer(text):
    return [{
        "id": "message-local", "type": "message", "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }]


def returned_tools(body):
    return [
        json.loads(item["output"]) for item in body["input"]
        if item.get("type") == "function_call_output"
    ]


async def grounded_balance(body):
    result = returned_tools(body)[-1]
    if not result["ok"]:
        return answer(f"Balance lookup failed: {result['error']['code']}.")
    return answer(f"{result['account_id']}: {result['currency']} {result['balance']:.2f}.")


async def grounded_budget(body):
    results = returned_tools(body)
    successes = [result for result in results if result["ok"]]
    failed = [result for result in results if not result["ok"]]
    text = "Partial budget summary." if failed else "Budget summary."
    for result in successes:
        text += f" {result['account_id']}: {result['spent']:.2f} spent."
    for result in failed:
        text += f" {result['account_id']}: {result['error']['code']}."
    return answer(text)


class ModelBoundary:
    def __init__(self, steps):
        self.steps = deque(steps)
        self.requests = []

    async def handle(self, request):
        assert request.url.host == "finance.invalid"
        body = json.loads(request.content)
        self.requests.append(body)
        assert self.steps, "Unexpected extra model call"
        step = self.steps.popleft()
        output = await step(body) if callable(step) else step
        envelope = {
            "id": f"resp-local-{len(self.requests)}", "created_at": 0,
            "model": "finance-synthetic-model", "object": "response",
            "status": "completed", "output": output,
            "usage": {
                "input_tokens": 7, "output_tokens": 3, "total_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
        if not body.get("stream"):
            return httpx.Response(200, json=envelope)
        events = [{
            "type": "response.created",
            "response": {**envelope, "status": "in_progress", "output": []},
        }]
        for index, item in enumerate(output):
            events.append({
                "type": "response.output_item.added", "output_index": index,
                "item": {**item, "arguments": "", "status": "in_progress"},
            })
            if item["type"] == "function_call":
                events.append({
                    "type": "response.function_call_arguments.delta",
                    "output_index": index, "item_id": item["id"], "delta": item["arguments"],
                })
            else:
                text = item["content"][0]["text"]
                for delta in (text[:len(text) // 2], text[len(text) // 2:]):
                    events.append({
                        "type": "response.output_text.delta", "output_index": index,
                        "content_index": 0, "item_id": item["id"], "delta": delta,
                    })
            events.append({
                "type": "response.output_item.done", "output_index": index, "item": item,
            })
        events.append({"type": "response.completed", "response": envelope})
        payload = "".join(
            f"data: {json.dumps({**event, 'sequence_number': index})}\n\n"
            for index, event in enumerate(events)
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=payload.encode(),
        )


@asynccontextmanager
async def agent_for(version, steps):
    boundary = ModelBoundary(steps)
    async with httpx.AsyncClient(transport=httpx.MockTransport(boundary.handle)) as http:
        async with AsyncOpenAI(
            api_key="synthetic-not-a-credential",
            base_url="https://finance.invalid", http_client=http, max_retries=0,
        ) as client:
            project = SimpleNamespace(get_openai_client=lambda **kwargs: client)
            foundry = FoundryChatClient(project_client=project, model="finance-synthetic-model")
            yield load_app(version).build_agent(client=foundry), boundary
            assert not boundary.steps, "Expected model interactions were not reached"


async def invoke(agent, text, stream=False, **kwargs):
    if stream:
        response = agent.run(text, stream=True, **kwargs)
        updates = [update async for update in response]
        final = await response.get_final_response()
        assert "".join(update.text for update in updates) == final.text
        return final
    return await agent.run(text, **kwargs)


def tool_evidence(telemetry, name=None):
    return [
        (span, json.loads(span.attributes["aiq.tool.call.arguments"]),
         json.loads(span.attributes["aiq.tool.call.result"]))
        for span in telemetry.get_finished_spans()
        if "aiq.tool.call.result" in span.attributes
        and (name is None or span.attributes.get("gen_ai.tool.name") == name)
    ]


def assert_real_trace(telemetry):
    spans = telemetry.get_finished_spans()
    operations = {span.attributes.get("gen_ai.operation.name") for span in spans}
    assert {"invoke_agent", "chat", "execute_tool"} <= operations
    roots = [s for s in spans if s.attributes.get("gen_ai.operation.name") == "invoke_agent"]
    assert len(roots) == 1
    by_id = {span.context.span_id: span for span in spans}
    for span, _, _ in tool_evidence(telemetry):
        assert span.context.trace_id == roots[0].context.trace_id
        assert span.attributes["gen_ai.agent.name"] == "finance-local-test"
        assert span.attributes["gen_ai.agent.version"] == "synthetic-version"
        while span.context.span_id != roots[0].context.span_id:
            assert span.parent is not None
            span = by_id[span.parent.span_id]


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("stream", [False, True])
def test_all_versions_use_real_tools_for_unrelated_healthy_budget(version, stream, telemetry):
    async def run():
        async with agent_for(version, [
            calls(("get_budget_summary", "acct-demo-b")), answer("USD 210.00 spent."),
        ]) as (agent, boundary):
            result = await invoke(agent, "Show acct-demo-b budget status.", stream)
            assert result.text == "USD 210.00 spent."
            payload = json.loads(boundary.requests[-1]["input"][-1]["output"])
            assert payload["spent"] == 210.0
    asyncio.run(run())
    assert len(tool_evidence(telemetry)) == 1
    assert_real_trace(telemetry)


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("stream", [False, True])
def test_transient_recovery_or_sole_omitted_retry(version, stream, telemetry):
    async def run():
        async with agent_for(version, [
            calls(("get_balance_with_transient", "acct-demo-a")), answer("Natural terminal."),
        ]) as (agent, boundary):
            result = await invoke(agent, "Use the transient balance lookup for acct-demo-a.", stream)
            payload = json.loads(boundary.requests[-1]["input"][-1]["output"])
            assert payload["ok"] is (version != "issue-018")
            assert result.response_id == "resp-local-2"
            assert result.usage_details["input_token_count"] == 14
            assert result.usage_details["output_token_count"] == 6
            if version == "issue-018":
                assert "without a retry" in result.text
                assert boundary.requests[-1]["tool_choice"] == "none"
            else:
                assert result.text == "Natural terminal."
    asyncio.run(run())
    evidence = tool_evidence(telemetry, "get_balance_with_transient")
    assert [result["ok"] for _, _, result in evidence] == (
        [False] if version == "issue-018" else [False, True]
    )
    assert all(arguments == {"account_id": "acct-demo-a"} for _, arguments, _ in evidence)
    assert evidence[0][2]["error"] == {"code": "temporary_unavailable", "retryable": True}
    assert_real_trace(telemetry)


@pytest.mark.parametrize("version", VERSIONS)
def test_unknown_transient_account_is_not_retried(version, telemetry):
    async def run():
        async with agent_for(version, [
            calls(("get_balance_with_transient", "acct-demo-missing")), answer("account_not_found"),
        ]) as (agent, boundary):
            await invoke(agent, "Use transient lookup for acct-demo-missing.")
            payload = json.loads(boundary.requests[-1]["input"][-1]["output"])
            assert payload["error"] == {"code": "account_not_found"}
    asyncio.run(run())
    assert len(tool_evidence(telemetry)) == 1


@pytest.mark.parametrize("account", ["acct-demo-a", "acct-demo-b"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("version", ["v0", "issue-013", "issue-014", "issue-015"])
def test_balance_defects_have_actual_dispatch_evidence(version, account, stream, telemetry):
    async def run():
        async with agent_for(version, [
            calls(("get_balance", account)), grounded_balance,
        ]) as (agent, boundary):
            prompt = (
                f"Use only {account} and show its balance."
                if version == "issue-015" else f"Show the balance for {account}."
            )
            response = await invoke(agent, prompt, stream)
            result = returned_tools(boundary.requests[-1])[-1]
            assert boundary.requests[0]["tools"]
            assert response.response_id == "resp-local-2"
            assert response.usage_details["input_token_count"] == 14
            assert response.usage_details["output_token_count"] == 6
            if version == "issue-014":
                assert result == {"ok": False, "error": {"code": "account_id_required"}}
                assert "account_id was omitted" in response.text
            else:
                actual_account = (
                    ("acct-demo-b" if account == "acct-demo-a" else "acct-demo-a")
                    if version == "issue-015" else account
                )
                assert result["account_id"] == actual_account
                assert result["balance"] == (1250.50 if actual_account == "acct-demo-a" else 875.0)
                reported = result["balance"] + (500 if version == "issue-013" else 0)
                assert actual_account in response.text
                assert f"USD {reported:.2f}" in response.text
                if version == "issue-013":
                    assert f"USD {result['balance']:.2f}" not in response.text
    asyncio.run(run())
    evidence = tool_evidence(telemetry, "get_balance")
    assert len(evidence) == 1
    _, arguments, result = evidence[0]
    assert arguments == (
        {} if version == "issue-014" else {"account_id": result["account_id"]}
    )
    assert_real_trace(telemetry)


@pytest.mark.parametrize("version", ["v0", "issue-016", "issue-019"])
@pytest.mark.parametrize("stream", [False, True])
def test_permanent_error_synthesis_and_retry_loop(version, stream, telemetry):
    async def run():
        async with agent_for(version, [
            calls(("get_balance", "acct-demo-missing")), grounded_balance,
        ]) as (agent, _):
            response = await invoke(
                agent, "Show the balance for acct-demo-missing and preserve the tool error.", stream
            )
            assert "account_not_found" in response.text
            assert ("successful balance" in response.text) is (version == "issue-016")
    asyncio.run(run())
    evidence = tool_evidence(telemetry)
    assert len(evidence) == (3 if version == "issue-019" else 1)
    assert all(
        arguments == {"account_id": "acct-demo-missing"}
        and result == {
            "ok": False, "account_id": "acct-demo-missing",
            "error": {"code": "account_not_found"},
        }
        for _, arguments, result in evidence
    )
    assert_real_trace(telemetry)


@pytest.mark.parametrize("version", ["v0", "issue-017"])
@pytest.mark.parametrize("account", ["acct-demo-a", "acct-demo-b"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("sequential", [False, True])
def test_complete_aggregate_requires_real_success_and_failed_item(
    version, account, stream, sequential, telemetry,
):
    async def run():
        lookups = [("get_budget_summary", account), ("get_budget_summary", "acct-demo-missing")]
        steps = (
            [calls(lookups[0]), calls(lookups[1], prefix="missing")]
            if sequential else [calls(*lookups)]
        )
        async with agent_for(version, [*steps, grounded_budget]) as (agent, boundary):
            response = await invoke(
                agent, f"Give the complete budget summary for {account} and acct-demo-missing.", stream
            )
            results = returned_tools(boundary.requests[-1])
            assert sorted(result["ok"] for result in results) == [False, True]
            assert ("complete budget summary" in response.text) is (version == "issue-017")
            assert ("partial" in response.text.casefold()) is (version == "v0")
            assert ("430.25" if account == "acct-demo-a" else "210.00") in response.text
    asyncio.run(run())
    evidence = tool_evidence(telemetry, "get_budget_summary")
    assert len(evidence) == 2
    assert {arguments["account_id"] for _, arguments, _ in evidence} == {account, "acct-demo-missing"}
    assert_real_trace(telemetry)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("version,prompt,lookups", [
    ("issue-013", "Show the balance for acct-demo-a.", []),
    ("issue-013", "Show the balance for acct-demo-a.", [("get_balance", "acct-demo-missing")]),
    ("issue-013", "Show the balance for acct-demo-a.", [("get_budget_summary", "acct-demo-a")]),
    ("issue-017", "Give the complete budget summary for acct-demo-a and acct-demo-missing.", []),
    ("issue-017", "Give the complete budget summary for acct-demo-a and acct-demo-missing.",
     [("get_budget_summary", "acct-demo-a")]),
    ("issue-017", "Give the complete budget summary for acct-demo-a and acct-demo-missing.",
     [("get_budget_summary", "acct-demo-missing")]),
    ("issue-017", "Give the complete budget summary for acct-demo-a and acct-demo-missing.",
     [("get_balance", "acct-demo-a"), ("get_balance", "acct-demo-missing")]),
    ("issue-016", "Show the balance for acct-demo-missing and preserve the tool error.", []),
    ("issue-018", "Use the transient balance lookup for acct-demo-a.", []),
])
def test_no_terminal_rewrite_without_required_tool_evidence(version, prompt, lookups, stream):
    async def run():
        steps = [calls(*lookups)] if lookups else []
        async with agent_for(version, [*steps, answer("Not enough data.")]) as (agent, _):
            result = await invoke(agent, prompt, stream)
            assert result.text == "Not enough data."
    asyncio.run(run())


@pytest.mark.parametrize("version,prompt,lookups", [
    ("issue-013", "Show the balance for acct-demo-a.", [("get_balance", "acct-demo-a")]),
    ("issue-017", "Give the complete budget summary for acct-demo-a and acct-demo-missing.",
     [("get_budget_summary", "acct-demo-a"), ("get_budget_summary", "acct-demo-missing")]),
    ("issue-018", "Use the transient balance lookup for acct-demo-a.",
     [("get_balance_with_transient", "acct-demo-a")]),
])
@pytest.mark.parametrize("stream", [False, True])
def test_previous_turn_results_cannot_activate_a_new_turn(version, prompt, lookups, stream):
    async def run():
        async with agent_for(version, [
            calls(*lookups), answer("First terminal."), answer("No new tool evidence."),
        ]) as (agent, _):
            session = agent.create_session()
            await invoke(agent, prompt, stream, session=session)
            result = await invoke(agent, prompt, stream, session=session)
            assert result.text == "No new tool evidence."
    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
def test_stream_replay_preserves_natural_follow_on_dispatch(stream, telemetry):
    async def run():
        async with agent_for("issue-013", [
            calls(("get_balance", "acct-demo-a")),
            calls(("get_budget_summary", "acct-demo-a"), prefix="budget"),
            answer("Natural terminal."),
        ]) as (agent, _):
            result = await invoke(agent, "Show the balance for acct-demo-a.", stream)
            assert "USD 1750.50" in result.text
    asyncio.run(run())
    assert [span.attributes["gen_ai.tool.name"] for span, _, _ in tool_evidence(telemetry)] == [
        "get_balance", "get_budget_summary",
    ]


@pytest.mark.parametrize("stream", [False, True])
def test_context_duplication_reaches_each_real_model_request_exactly_four_times(stream):
    async def run():
        requests = {}
        for version in ("v0", "issue-020"):
            async with agent_for(version, [
                calls(("get_balance", "acct-demo-a"), ("list_monthly_items", "acct-demo-a")),
                answer("Balance and monthly items."),
            ]) as (agent, boundary):
                await invoke(agent, "Summarize the balance and monthly items for acct-demo-a.", stream)
                requests[version] = boundary.requests
        for baseline, issue in zip(requests["v0"], requests["issue-020"], strict=True):
            expected = Counter(json.dumps(item, sort_keys=True) for item in baseline["input"])
            actual = Counter(json.dumps(item, sort_keys=True) for item in issue["input"])
            assert actual == Counter({item: count * 4 for item, count in expected.items()})
        assert len(returned_tools(requests["v0"][-1])) == 2
        assert len(returned_tools(requests["issue-020"][-1])) == 8
    asyncio.run(run())


@pytest.mark.parametrize("version", ["v0", "issue-018"])
@pytest.mark.parametrize("stream", [False, True])
def test_concurrent_runs_sharing_a_trace_have_independent_transients(version, stream, telemetry):
    async def run():
        both_arrived = asyncio.Event()
        arrivals = 0

        async def model(body):
            nonlocal arrivals
            if not returned_tools(body):
                return calls(("get_balance_with_transient", "acct-demo-a"))
            arrivals += 1
            if arrivals == 2:
                both_arrived.set()
            await asyncio.wait_for(both_arrived.wait(), timeout=5)
            return await grounded_balance(body)

        async with agent_for(version, [model] * 4) as (agent, _):
            with trace.get_tracer("finance-tests").start_as_current_span("concurrent-runs"):
                await asyncio.gather(*[
                    invoke(agent, "Use the transient balance lookup for acct-demo-a.", stream)
                    for _ in range(2)
                ])
    asyncio.run(run())
    evidence = tool_evidence(telemetry)
    assert len({span.context.trace_id for span, _, _ in evidence}) == 1
    assert sum(not result["ok"] for _, _, result in evidence) == 2
    assert sum(result["ok"] for _, _, result in evidence) == (0 if version == "issue-018" else 2)


@pytest.mark.parametrize("stream", [False, True])
def test_successful_transient_is_not_rearmed_within_a_run(stream, telemetry):
    async def run():
        async with agent_for("v0", [
            calls(("get_balance_with_transient", "acct-demo-a")),
            calls(("get_balance_with_transient", "acct-demo-a"), prefix="again"),
            grounded_balance,
        ]) as (agent, _):
            await invoke(agent, "Use the transient balance lookup for acct-demo-a.", stream)
    asyncio.run(run())
    assert [result["ok"] for _, _, result in tool_evidence(telemetry)] == [False, True, True]


@pytest.mark.parametrize("stream", [False, True])
def test_cancelled_run_does_not_leave_transient_state_for_next_run(stream, telemetry):
    async def run():
        entered = asyncio.Event()

        async def blocked_model(body):
            entered.set()
            await asyncio.Event().wait()

        async with agent_for("issue-018", [
            calls(("get_balance_with_transient", "acct-demo-a")), blocked_model,
            calls(("get_balance_with_transient", "acct-demo-a")), grounded_balance,
        ]) as (agent, _):
            prompt = "Use the transient balance lookup for acct-demo-a."
            with trace.get_tracer("finance-tests").start_as_current_span("cancel-and-resume"):
                pending = asyncio.create_task(invoke(agent, prompt, stream))
                await asyncio.wait_for(entered.wait(), timeout=5)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                result = await invoke(agent, prompt, stream)
                assert "without a retry" in result.text
    asyncio.run(run())
    assert [result["ok"] for _, _, result in tool_evidence(telemetry)] == [False, False]


@pytest.mark.parametrize("version", ["v0", "issue-018"])
@pytest.mark.parametrize("stream", [False, True])
def test_successive_requests_in_one_session_restart_transient_scope(version, stream, telemetry):
    async def run():
        steps = [
            step for index in range(3)
            for step in (
                calls(("get_balance_with_transient", "acct-demo-a"), prefix=f"turn-{index}"),
                grounded_balance,
            )
        ]
        async with agent_for(version, steps) as (agent, boundary):
            session = agent.create_session()
            with trace.get_tracer("finance-tests").start_as_current_span("successive-runs"):
                for _ in range(3):
                    await invoke(
                        agent, "Use the transient balance lookup for acct-demo-a.",
                        stream, session=session,
                    )
            for request in boundary.requests:
                transient = next(
                    tool for tool in request["tools"]
                    if tool.get("name") == "get_balance_with_transient"
                )
                assert set(transient["parameters"]["properties"]) == {"account_id"}
    asyncio.run(run())
    assert [result["ok"] for _, _, result in tool_evidence(telemetry)] == (
        [False] * 3 if version == "issue-018" else [False, True] * 3
    )


@pytest.mark.parametrize("fixture_index", [0, 1, 2])
@pytest.mark.parametrize("stream", [False, True])
def test_baseline_partial_fixture_requires_actual_mixed_budget_results(
    fixture_index, stream, telemetry,
):
    traffic = json.loads((ROOT / "v0" / "traffic.json").read_text(encoding="utf-8"))
    original = next(item for item in traffic["requests"] if item["id"] == "finance-agent-v0-partial")
    counterparts = [
        step
        for scenario in traffic["validation_rules"]["scenarios"]
        for attempt in scenario["attempts"]
        if original["id"] in attempt["parameters"]["source_request_ids"]
        for step in attempt["probe_steps"]
    ]
    assert len(counterparts) == 2
    selected = [original, *counterparts][fixture_index]
    expected = selected["expected"]
    for field in ("semantic_assertions", "trace_assertions"):
        assert expected[field] == original["expected"][field]
    prompt = selected["request"]["body"]["input"][0]["content"][0]["text"]

    async def run():
        async def choose_budget_calls(body):
            user = next(item for item in reversed(body["input"]) if item.get("role") == "user")
            text = user["content"][0]["text"]
            assert "budget" in text
            assert "acct-demo-a" in text and "acct-demo-missing" in text
            return calls(
                ("get_budget_summary", "acct-demo-a"),
                ("get_budget_summary", "acct-demo-missing"),
            )

        async with agent_for("v0", [choose_budget_calls, grounded_budget]) as (agent, _):
            result = await invoke(agent, prompt, stream)
            assert all(
                term in result.text.casefold()
                for term in expected["semantic_assertions"]["required_terms_all"]
            )
            assert any(
                term in result.text.casefold()
                for term in expected["semantic_assertions"]["required_terms_any"]
            )
    asyncio.run(run())
    evidence = tool_evidence(telemetry, "get_budget_summary")
    assert len(evidence) == 2
    by_account = {arguments["account_id"]: result for _, arguments, result in evidence}
    assert by_account["acct-demo-a"]["ok"] is True
    assert by_account["acct-demo-a"]["spent"] == 430.25
    assert by_account["acct-demo-missing"] == {
        "ok": False, "account_id": "acct-demo-missing",
        "error": {"code": "account_not_found"},
    }
    assert expected["trace_assertions"] == [
        {"name": "two_budget_calls", "kind": "tool_call_count",
         "tool_name": "get_budget_summary", "count": len(evidence)},
        {"name": "mixed_budget_results", "kind": "tool_result_class",
         "tool_name": "get_budget_summary", "result_class": "mixed"},
    ]
    assert_real_trace(telemetry)
