"""Explicit local integration against Travel's pinned LangGraph/Responses host.

Run: python -m pytest tests/hosted/test_travel_hosted.py
Requires agents/travel-agent/v0/requirements.txt plus the existing dev tools.
Missing pinned dependencies fail collection; local results are not deployed proof.
Only credentials, model HTTP and synthetic inventory I/O waits are controlled.
Standalone graph checks are domain evidence; only the real host checks establish
endpoint invocation identity and transitive native graph/tool/model ancestry.
"""

import asyncio
import importlib
import importlib.util
import json
import sys
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from azure.ai.agentserver.responses.store._memory import InMemoryResponseProvider
from langchain_core.messages import HumanMessage
from openai import AsyncOpenAI
from opentelemetry import trace
from opentelemetry.sdk import trace as sdk_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


ROOT = Path(__file__).resolve().parents[2] / "agents" / "travel-agent"
VERSIONS = ["v0", *(f"issue-{number:03}" for number in range(21, 29))]


def version_path(version):
    return ROOT / "v0" if version == "v0" else ROOT / "issues" / version


@pytest.fixture(scope="module")
def telemetry():
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


@pytest.fixture
def runtime(request, monkeypatch, telemetry):
    version = getattr(request, "param", "v0")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    for signal in ("TRACES", "METRICS", "LOGS"):
        monkeypatch.setenv(f"OTEL_{signal}_EXPORTER", "none")
        monkeypatch.delenv(f"OTEL_EXPORTER_OTLP_{signal}_ENDPOINT", raising=False)
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", "synthetic-travel")
    monkeypatch.setenv("FOUNDRY_AGENT_VERSION", "local-test")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://synthetic.invalid")
    monkeypatch.setenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "synthetic-model")
    name = "travel_local_" + version.replace("-", "_")
    source = version_path(version) / "source"
    spec = importlib.util.spec_from_file_location(
        name, source / "__init__.py", submodule_search_locations=[str(source)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    app = importlib.import_module(name + ".app")
    telemetry.clear()
    calls, clients, credentials = [], [], []
    model_failure = []

    class Credential:
        def __init__(self):
            self.closed = False
            credentials.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            self.closed = True

        async def get_token(self, scope):
            assert scope == "https://ai.azure.com/.default"
            return SimpleNamespace(token="synthetic-token")

    def respond(request):
        assert request.url.host == "synthetic.invalid"
        payload = json.loads(request.content)
        calls.append(payload)
        if model_failure:
            return httpx.Response(
                400, json={"error": {"message": "Synthetic rejection"}}
            )
        return httpx.Response(
            200,
            json={
                "id": f"resp-synthetic-model-{len(calls)}",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "synthetic-model",
                "output": [
                    {
                        "type": "message",
                        "id": "msg-synthetic",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Concise synthetic review.",
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 321,
                    "output_tokens": 5,
                    "total_tokens": 326,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    def model_client(**kwargs):
        transport = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        clients.append(transport)
        return AsyncOpenAI(**kwargs, http_client=transport, max_retries=0)

    async def ready(_):
        return None

    monkeypatch.setattr(app, "DefaultAzureCredential", Credential)
    monkeypatch.setattr(app, "AsyncOpenAI", model_client)
    monkeypatch.setattr(app, "sleep", ready)
    ledger = app.BookingLedger()
    result = SimpleNamespace(
        app=app,
        graph=app.build_graph(bookings=ledger),
        ledger=ledger,
        calls=calls,
        clients=clients,
        credentials=credentials,
        exporter=telemetry,
        model_failure=model_failure,
    )
    yield result
    for key in list(sys.modules):
        if key == name or key.startswith(name + "."):
            del sys.modules[key]
    assert all(client.is_closed for client in clients)
    assert all(credential.closed for credential in credentials)


async def turn(runtime, text, thread="synthetic-conversation"):
    return await runtime.graph.ainvoke(
        {"messages": [HumanMessage(content=text)]},
        config={"configurable": {"thread_id": thread}},
    )


def answer(state):
    return state["messages"][-1].content


def tools(runtime):
    return [
        span
        for span in runtime.exporter.get_finished_spans()
        if span.attributes.get("gen_ai.operation.name") == "execute_tool"
    ]


@pytest.mark.parametrize("runtime", VERSIONS, indirect=True)
def test_all_authorities_run_real_graph_and_model_instrumentation(runtime):
    state = asyncio.run(turn(runtime, "Find a flight for trip-beta."))
    assert answer(state)
    assert len(runtime.calls) == 1
    chat = next(
        span
        for span in runtime.exporter.get_finished_spans()
        if span.name == "travel.model.respond"
    )
    assert (
        json.loads(chat.attributes["gen_ai.input.messages"])[0]["parts"][0]["content"]
        == (runtime.calls[0]["input"])
    )
    assert chat.attributes["gen_ai.response.id"] == "resp-synthetic-model-1"
    assert chat.attributes["gen_ai.response.model"] == "synthetic-model"
    assert chat.attributes["gen_ai.usage.input_tokens"] == 321
    assert chat.attributes["gen_ai.usage.output_tokens"] == 5
    assert "Concise synthetic review." in chat.attributes["gen_ai.output.messages"]
    assert all(
        span.attributes["gen_ai.agent.name"] == "synthetic-travel"
        for span in tools(runtime)
    )


@pytest.mark.parametrize("runtime", VERSIONS, indirect=True)
def test_neutral_setup_does_not_search_book_or_bind_a_trip(runtime):
    state = asyncio.run(
        turn(
            runtime,
            "Acknowledge this synthetic conversation context without external action.",
        )
    )
    assert "acknowledged" in answer(state)
    assert "trip" not in state
    assert not tools(runtime) and not runtime.calls and not runtime.ledger.records


@pytest.mark.parametrize(
    "runtime", [v for v in VERSIONS if v != "issue-025"], indirect=True
)
def test_negative_booking_cannot_leak_previous_confirmation(runtime):
    async def run():
        for text in (
            "Find a hotel for trip-beta.",
            "Do not confirm booking for trip-beta.",
            "The trip-beta booking is unconfirmed.",
            "Show confirmation for trip-beta.",
        ):
            state = await turn(runtime, text)
            assert not state["booked"]
            assert not runtime.ledger.records

    asyncio.run(run())


def test_positive_booking_and_followup_scope(runtime):
    async def run():
        fresh = await turn(runtime, "Yes, book it.", thread="no-proposal")
        assert not fresh["booked"] and not runtime.ledger.records
        await turn(runtime, "Find a hotel for trip-beta.")
        booked = await turn(runtime, "Yes, book it.")
        assert booked["booked"]
        assert runtime.ledger.records[booked["booking_id"]] == {
            "trip": "trip-beta",
            "option_ids": ["hotel-demo-0"],
        }
        switched = await turn(runtime, "Switch to trip-gamma and find a flight.")
        assert not switched["booked"]
        denied = await turn(runtime, "Do not book trip-gamma.")
        assert not denied["booked"] and len(runtime.ledger.records) == 1

    asyncio.run(run())
    spans = runtime.exporter.get_finished_spans()
    booking = next(span for span in spans if span.name == "travel.tool.book")
    assert any(
        span.name == "travel.validate" and span.end_time <= booking.start_time
        for span in spans
    )
    assert any(
        span.name == "travel.confirm"
        and span.attributes["travel.booking.confirmed"]
        and span.end_time <= booking.start_time
        for span in spans
    )


@pytest.mark.parametrize("runtime", ["v0", "issue-024"], indirect=True)
def test_overfetch_reaches_actual_model_input_but_not_endpoint_output(runtime):
    state = asyncio.run(turn(runtime, "Find one concise flight result for trip-beta."))
    prompt = runtime.calls[0]["input"]
    expected_count = 80 if runtime.app.__name__.endswith("issue_024.app") else 2
    assert len(state["inventory"]) == expected_count
    assert "flight-demo-0" in answer(state) and "flight-demo-1" not in answer(state)
    assert len(answer(state)) < 300
    if expected_count == 80:
        payload = json.loads(prompt.split("Inventory search payload: ", 1)[1])[
            "inventory"
        ]
        assert payload == state["inventory"]
        assert "flight-demo-79" in prompt and len(prompt) > 10000
    else:
        assert len(prompt) < 600


@pytest.mark.parametrize("runtime", ["v0", "issue-025"], indirect=True)
def test_booking_is_a_real_side_effect_before_checks_only_in_issue025(runtime):
    state = asyncio.run(
        turn(runtime, "I am only considering a flight for trip-beta; do not book it.")
    )
    spans = runtime.exporter.get_finished_spans()
    bookings = [span for span in spans if span.name == "travel.tool.book"]
    assert not state["confirmed"]
    if runtime.app.__name__.endswith("issue_025.app"):
        assert state["booked"] and len(runtime.ledger.records) == 1
        record = runtime.ledger.records[state["booking_id"]]
        assert record == {"trip": "trip-beta", "option_ids": ["flight-demo-0"]}
        assert len(bookings) == 1
        assert (
            json.loads(bookings[0].attributes["gen_ai.tool.call.result"])["booking_id"]
            == (state["booking_id"])
        )
        for name in ("travel.validate", "travel.confirm"):
            check = next(span for span in spans if span.name == name)
            assert bookings[0].end_time <= check.start_time
            assert spans.index(bookings[0]) < spans.index(check)
    else:
        assert not state["booked"] and not bookings and not runtime.ledger.records


@pytest.mark.parametrize("runtime", ["v0", "issue-026"], indirect=True)
def test_all_ten_comparison_probes_against_matched_baseline(runtime):
    traffic = json.loads(
        (version_path("issue-026") / "traffic.json").read_text(encoding="utf-8")
    )
    requests = {item["id"]: item for item in traffic["requests"]}

    async def run():
        for attempt in traffic["attempts"]:
            probe = requests[attempt["probe_steps"][0]]
            text = probe["request"]["body"]["input"][0]["content"][0]["text"]
            trips = runtime.app.requested_trips(text)
            runtime.exporter.clear()
            state = await turn(runtime, text, thread=str(attempt["index"]))
            search_trips = [
                json.loads(span.attributes["gen_ai.tool.call.arguments"])["trip"]
                for span in tools(runtime)
            ]
            assert search_trips == trips
            assert trips[0] in answer(state)
            if runtime.app.__name__.endswith("issue_026.app"):
                assert trips[1] not in answer(state)
            else:
                assert trips[1] in answer(state)

    asyncio.run(run())


@pytest.mark.parametrize("runtime", ["v0", "issue-028"], indirect=True)
def test_stale_state_requires_prior_conversation_and_ignores_current_source_words(
    runtime,
):
    async def run():
        await turn(runtime, "Find a flight for trip-gamma.")
        switched = await turn(runtime, "Switch to trip-beta and find a hotel.")
        expected = (
            "trip-gamma"
            if runtime.app.__name__.endswith("issue_028.app")
            else "trip-beta"
        )
        assert switched["trip"] == expected and expected in answer(switched)
        fresh = await turn(
            runtime, "Switch to trip-beta and find a hotel.", thread="fresh"
        )
        assert fresh["trip"] == "trip-beta"
        again = await turn(runtime, "Search again.")
        assert again["trip"] == expected

    asyncio.run(run())


@pytest.mark.parametrize("runtime", ["v0", "issue-027"], indirect=True)
def test_independent_search_overlap_without_wall_clock_sleeps(runtime, monkeypatch):
    clock = count(1_000_000, 100)
    monkeypatch.setattr(sdk_trace, "time_ns", lambda: next(clock))

    async def run():
        entered, release = (
            [asyncio.Event(), asyncio.Event()],
            [asyncio.Event(), asyncio.Event()],
        )
        count = 0

        async def controlled_io(_):
            nonlocal count
            index = count
            count += 1
            entered[index].set()
            await release[index].wait()

        monkeypatch.setattr(runtime.app, "sleep", controlled_io)
        task = asyncio.create_task(
            turn(runtime, "Compare flight and hotel for trip-beta.")
        )
        await asyncio.wait_for(entered[0].wait(), timeout=5)
        serialized = runtime.app.__name__.endswith("issue_027.app")
        try:
            if serialized:
                assert not entered[1].is_set()
                release[0].set()
                await asyncio.wait_for(entered[1].wait(), timeout=5)
            else:
                await asyncio.wait_for(entered[1].wait(), timeout=5)
                assert not release[0].is_set()
            release[0].set()
            release[1].set()
            state = await asyncio.wait_for(task, timeout=5)
        finally:
            release[0].set()
            release[1].set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert "flight-demo-0" in answer(state) and "hotel-demo-0" in answer(state)
        flight, hotel = tools(runtime)
        if serialized:
            assert flight.end_time <= hotel.start_time
        else:
            assert (
                flight.start_time < hotel.end_time
                and hotel.start_time < flight.end_time
            )

    asyncio.run(run())


@pytest.mark.parametrize("runtime", VERSIONS, indirect=True)
def test_actual_responses_host_returns_grounded_message_and_continues_conversation(
    runtime,
):
    async def run():
        host = runtime.app.TravelResponsesHostServer(
            runtime.graph,
            identity=runtime.app.RUNTIME_IDENTITY,
            store=InMemoryResponseProvider(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host._app),
            base_url="http://synthetic.test",
        ) as client:
            first = await client.post(
                "/responses",
                json={
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "Find a flight for trip-gamma.",
                                }
                            ],
                        }
                    ],
                    "stream": False,
                    "conversation": {"id": "conv-synthetic-host"},
                },
            )
            assert first.status_code == 200, first.text
            first_body = first.json()
            assert first_body["status"] == "completed"
            second = await client.post(
                "/responses",
                json={
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "Switch to trip-beta and find a flight.",
                                }
                            ],
                        }
                    ],
                    "stream": False,
                    "conversation": {"id": "conv-synthetic-host"},
                },
            )
            assert second.status_code == 200, second.text
            body = second.json()
            assert body["status"] == "completed"
            assert body["id"] != first_body["id"]
            assert body["id"] != "resp-synthetic-model-2"
            text = " ".join(
                part["text"]
                for item in body["output"]
                if item["type"] == "message"
                for part in item["content"]
                if part["type"] == "output_text"
            )
            assert text
            if runtime.app.__name__.endswith("issue_028.app"):
                assert "trip-gamma" in text and "trip-beta" not in text
            spans = runtime.exporter.get_finished_spans()
            invocations = [span for span in spans if span.name == "travel.invoke"]
            assert {span.attributes["gen_ai.response.id"] for span in invocations} == {
                first_body["id"],
                body["id"],
            }
            for invocation in invocations:
                assert invocation.attributes["gen_ai.operation.name"] == "invoke_agent"
                assert invocation.attributes["gen_ai.agent.name"] == "synthetic-travel"
                by_id = {span.context.span_id: span for span in spans}

                def descendant(span):
                    seen = set()
                    while span.parent is not None:
                        parent_id = span.parent.span_id
                        assert parent_id not in seen
                        seen.add(parent_id)
                        if parent_id == invocation.context.span_id:
                            return True
                        if parent_id not in by_id:
                            return False
                        span = by_id[parent_id]
                    return False

                children = [span for span in spans if descendant(span)]
                assert any(span.name == "travel.model.respond" for span in children)
                chat = next(
                    span for span in children if span.name == "travel.model.respond"
                )
                assert chat.context.trace_id == invocation.context.trace_id
                assert (
                    chat.attributes["gen_ai.response.id"]
                    != invocation.attributes["gen_ai.response.id"]
                )
                assert (
                    len(
                        [
                            span
                            for span in children
                            if span.attributes.get("gen_ai.response.id")
                            == chat.attributes["gen_ai.response.id"]
                        ]
                    )
                    == 1
                )
                assert "gen_ai.output.messages" in invocation.attributes
                if not runtime.app.__name__.endswith("issue_023.app"):
                    assert any(
                        span.attributes.get("gen_ai.operation.name") == "execute_tool"
                        for span in children
                    )

    asyncio.run(run())


def test_model_failure_is_visible_and_resources_are_closed(runtime):
    runtime.model_failure.append(True)
    from openai import BadRequestError

    with pytest.raises(BadRequestError):
        asyncio.run(turn(runtime, "Find a flight for trip-beta."))
    chat = next(
        span
        for span in runtime.exporter.get_finished_spans()
        if span.name == "travel.model.respond"
    )
    assert chat.status.is_ok is False


@pytest.mark.parametrize(
    "runtime", ["v0", "issue-021", "issue-022", "issue-023"], indirect=True
)
def test_inventory_defects_have_real_tool_and_endpoint_evidence(runtime):
    state = asyncio.run(turn(runtime, "Find a flight for trip-beta."))
    spans = tools(runtime)
    version = runtime.app.__name__
    if version.endswith("issue_021.app"):
        assert "invented-demo-seat" in answer(state)
        assert len(spans) == 1 and spans[0].attributes["tool.ok"] is False
        assert spans[0].events[0].name == "exception"
        assert not state["validated"]
    elif version.endswith("issue_022.app"):
        assert "hotel-demo-0" in answer(state) and "flight-demo-0" not in answer(state)
        assert [span.attributes["gen_ai.tool.name"] for span in spans] == [
            "search_hotels"
        ]
    elif version.endswith("issue_023.app"):
        assert not spans and not state["inventory"]
        assert "No synthetic inventory options" in answer(state)
    else:
        assert "flight-demo-0" in answer(state) and "invented-demo-seat" not in answer(
            state
        )
        assert len(spans) == 1 and spans[0].attributes["tool.ok"] is True


def test_baseline_failure_recovery_is_real_and_never_authorizes_booking(runtime):
    async def run():
        recovered = await turn(
            runtime, "Retry one temporary flight search for trip-beta."
        )
        assert recovered["validated"] and not recovered["booked"]
        assert [span.attributes["tool.ok"] for span in tools(runtime)] == [False, True]
        runtime.exporter.clear()
        partial = await turn(
            runtime,
            "Compare flight and hotel for trip-beta when hotel search is unavailable.",
        )
        assert "Partial result" in answer(partial)
        assert "hotel_search_unavailable" in answer(partial)
        assert not partial["validated"] and not partial["booked"]
        assert [span.attributes["tool.ok"] for span in tools(runtime)] == [True, False]

    asyncio.run(run())


@pytest.mark.parametrize("runtime", ["v0", "issue-028"], indirect=True)
def test_all_ten_retained_state_probes_and_cold_controls(runtime):
    traffic = json.loads(
        (version_path("issue-028") / "traffic.json").read_text(encoding="utf-8")
    )
    requests = {item["id"]: item for item in traffic["requests"]}

    async def run():
        for attempt in traffic["attempts"]:
            thread = str(attempt["index"])
            setup = requests[attempt["setup_steps"][0]]
            probe = requests[attempt["probe_steps"][0]]
            seed = setup["request"]["body"]["input"][0]["content"][0]["text"]
            text = probe["request"]["body"]["input"][0]["content"][0]["text"]
            previous = runtime.app.requested_trips(seed)[0]
            current = runtime.app.requested_trips(text)[0]
            assert previous != current and previous not in text
            await turn(runtime, seed, thread=thread)
            runtime.exporter.clear()
            state = await turn(runtime, text, thread=thread)
            expected = (
                previous if runtime.app.__name__.endswith("issue_028.app") else current
            )
            assert state["trip"] == expected
            assert expected in answer(state)
            assert (
                json.loads(tools(runtime)[0].attributes["gen_ai.tool.call.arguments"])[
                    "trip"
                ]
                == expected
            )
            cold = await turn(runtime, text, thread="cold-" + thread)
            assert cold["trip"] == current

    asyncio.run(run())


def test_multi_itinerary_proposal_does_not_authorize_an_ambiguous_booking(runtime):
    async def run():
        await turn(runtime, "Compare flight options for trip-gamma and trip-beta.")
        ambiguous = await turn(runtime, "Yes, book it.")
        assert not ambiguous["booked"] and not runtime.ledger.records
        explicit = await turn(runtime, "Please book a flight for trip-beta.")
        assert explicit["booked"]
        assert runtime.ledger.records[explicit["booking_id"]]["trip"] == "trip-beta"

    asyncio.run(run())


@pytest.mark.parametrize("fail_model", [False, True])
def test_actual_host_structured_booking_and_failure_status(runtime, fail_model):
    async def run():
        host = runtime.app.TravelResponsesHostServer(
            runtime.graph,
            identity=runtime.app.RUNTIME_IDENTITY,
            store=InMemoryResponseProvider(),
        )
        if fail_model:
            runtime.model_failure.append(True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host._app),
            base_url="http://synthetic.test",
        ) as client:
            response = await client.post(
                "/responses",
                json={
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "Please book a hotel for trip-beta.",
                                }
                            ],
                        }
                    ],
                    "stream": False,
                    "conversation": {"id": "conv-synthetic-booking"},
                },
            )
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == ("failed" if fail_model else "completed")
            assert list(runtime.ledger.records.values()) == [
                {"trip": "trip-beta", "option_ids": ["hotel-demo-0"]},
            ]
            root = next(
                span
                for span in runtime.exporter.get_finished_spans()
                if span.name == "travel.invoke"
            )
            assert root.attributes["gen_ai.response.id"] == body["id"]
            assert root.status.is_ok is not fail_model
            if not fail_model:
                text = body["output"][0]["content"][0]["text"]
                assert "Booking completed" in text and "hotel-demo-0" in text
                refusal = await client.post(
                    "/responses",
                    json={
                        "input": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "Do not confirm booking for trip-beta.",
                                    }
                                ],
                            }
                        ],
                        "stream": False,
                        "conversation": {"id": "conv-synthetic-booking"},
                    },
                )
                assert refusal.json()["status"] == "completed"
                assert (
                    "Booking not completed"
                    in refusal.json()["output"][0]["content"][0]["text"]
                )
                assert len(runtime.ledger.records) == 1

    asyncio.run(run())
