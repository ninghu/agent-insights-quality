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
from langchain_azure_ai.agents.hosting import ResponsesHostServer
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
    monkeypatch.delenv("FOUNDRY_AGENT_SESSION_ID", raising=False)
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
        if span.name == "travel.model.review"
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
    assert chat.attributes["travel.review.internal"] is True
    assert chat.attributes["travel.review.output_delivered"] is False
    assert "External user request: Find a flight for trip-beta." in runtime.calls[0]["input"]
    assert "Candidate user-facing response: " + answer(state) in runtime.calls[0]["input"]
    assert "not a user-facing answer" in runtime.calls[0]["input"]
    assert "Concise synthetic review." not in answer(state)
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
                assert any(span.name == "travel.model.review" for span in children)
                chat = next(
                    span for span in children if span.name == "travel.model.review"
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
        if span.name == "travel.model.review"
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


@pytest.mark.parametrize("runtime", ["v0", "issue-028"], indirect=True)
@pytest.mark.parametrize("session_source", ["payload", "platform_environment"])
def test_native_session_restores_checkpoint_with_unstored_responses(
    runtime, monkeypatch, session_source
):
    """Match staging's native session affinity, not the local conversation shortcut."""
    traffic = json.loads(
        (version_path("issue-028") / "traffic.json").read_text(encoding="utf-8")
    )
    requests = {item["id"]: item for item in traffic["requests"]}
    stale = runtime.app.__name__.endswith("issue_028.app")

    async def run():
        # Each platform session normally has its own container. Exercise payload
        # affinity in a shared host as well, to catch accidental cross-session reuse.
        for attempt in traffic["attempts"]:
            session = f"synthetic-native-{attempt['index']}"
            if session_source == "platform_environment":
                monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", session)
            host = runtime.app.TravelResponsesHostServer(
                runtime.graph,
                identity=runtime.app.RUNTIME_IDENTITY,
                store=InMemoryResponseProvider(),
            )
            seed = requests[attempt["setup_steps"][0]]["request"]["body"]
            probe = requests[attempt["probe_steps"][0]]["request"]["body"]
            old_trip = runtime.app.requested_trips(
                seed["input"][0]["content"][0]["text"]
            )[0]
            new_trip = runtime.app.requested_trips(
                probe["input"][0]["content"][0]["text"]
            )[0]
            assert old_trip not in probe["input"][0]["content"][0]["text"]
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=host._app),
                base_url="http://synthetic.test",
            ) as client:
                response_ids = []
                for body, expected_trip in (
                    (seed, old_trip),
                    (probe, old_trip if stale else new_trip),
                    ({"input": "Search again."}, old_trip if stale else new_trip),
                ):
                    runtime.exporter.clear()
                    wire = {**body, "store": False}
                    if session_source == "payload":
                        wire["agent_session_id"] = session
                    assert "conversation" not in wire
                    assert "previous_response_id" not in wire
                    response = await client.post("/responses", json=wire)
                    assert response.status_code == 200, response.text
                    result = response.json()
                    assert result["status"] == "completed"
                    response_ids.append(result["id"])
                    text = result["output"][0]["content"][0]["text"]
                    assert expected_trip in text
                    calls = [
                        span
                        for span in tools(runtime)
                        if span.attributes["gen_ai.tool.name"]
                        in {"search_flights", "search_hotels"}
                    ]
                    assert calls
                    assert all(
                        json.loads(span.attributes["gen_ai.tool.call.arguments"])[
                            "trip"
                        ]
                        == expected_trip
                        for span in calls
                    )
                assert len(set(response_ids)) == 3

    asyncio.run(run())


@pytest.mark.parametrize("runtime", ["v0", "issue-028"], indirect=True)
def test_native_session_and_user_partitions_do_not_share_itineraries(
    runtime, monkeypatch
):
    # Payload session affinity takes precedence over the container's fallback.
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", "synthetic-container-default")

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

            async def invoke(session, user, text, expected):
                response = await client.post(
                    "/responses",
                    json={"input": text, "agent_session_id": session, "store": False},
                    headers={
                        "x-agent-user-id": user,
                        "x-agent-foundry-call-id": f"synthetic-call-{len(runtime.calls)}",
                    },
                )
                assert response.status_code == 200
                result = response.json()
                assert result["status"] == "completed"
                assert expected in result["output"][0]["content"][0]["text"]

            await invoke(
                "session-a", "user-a", "Find a hotel for trip-gamma.", "trip-gamma"
            )
            await invoke(
                "session-b", "user-a", "Find a hotel for trip-beta.", "trip-beta"
            )
            await invoke(
                "session-a", "user-b", "Find a hotel for trip-alpha.", "trip-alpha"
            )
            await invoke("session-a", "user-a", "Search again.", "trip-gamma")
            await invoke("session-b", "user-a", "Search again.", "trip-beta")
            await invoke("session-a", "user-b", "Search again.", "trip-alpha")
            await invoke(
                "session-c",
                "user-a",
                "Switch to trip-beta and find a flight.",
                "trip-beta",
            )
            switched = (
                "trip-gamma"
                if runtime.app.__name__.endswith("issue_028.app")
                else "trip-beta"
            )
            await invoke(
                "session-a",
                "user-a",
                "Switch to trip-beta and find a flight.",
                switched,
            )

    asyncio.run(run())


@pytest.mark.parametrize("continuation", ["conversation", "previous_response"])
def test_explicit_responses_continuation_keeps_sdk_thread_semantics(
    runtime, continuation
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
            first_body = {
                "input": "Find a hotel for trip-gamma.",
                "store": True,
            }
            if continuation == "conversation":
                first_body["conversation"] = {"id": "synthetic-conversation"}
                first_body["agent_session_id"] = "synthetic-affinity"
            first = await client.post("/responses", json=first_body)
            assert first.status_code == 200
            assert first.json()["status"] == "completed"
            second_body = {
                "input": "Search again.",
                "store": True,
            }
            if continuation == "conversation":
                second_body["conversation"] = first_body["conversation"]
                second_body["agent_session_id"] = "synthetic-affinity"
            else:
                second_body["previous_response_id"] = first.json()["id"]
            second = await client.post("/responses", json=second_body)
            assert second.status_code == 200
            assert second.json()["status"] == "completed"
            assert "trip-gamma" in second.json()["output"][0]["content"][0]["text"]

    asyncio.run(run())


@pytest.mark.parametrize("runtime", VERSIONS, indirect=True)
def test_reviewed_travel_attempts_use_native_session_wire(runtime):
    version = (
        runtime.app.__name__.split(".")[0]
        .removeprefix("travel_local_")
        .replace("_", "-")
    )
    traffic = json.loads(
        (version_path(version) / "traffic.json").read_text(encoding="utf-8")
    )
    requests = {item["id"]: item for item in traffic["requests"]}

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
            for attempt in traffic["attempts"]:
                for step_id in attempt["setup_steps"] + attempt["probe_steps"]:
                    step = requests[step_id]
                    runtime.exporter.clear()
                    response = await client.post(
                        "/responses",
                        json={
                            **step["request"]["body"],
                            "agent_session_id": f"synthetic-reviewed-{attempt['index']}",
                            "store": False,
                        },
                    )
                    assert response.status_code == step["expected"]["http_status"]
                    result = response.json()
                    assert result["status"] == "completed"
                    text = result["output"][0]["content"][0]["text"]
                    semantic = step["expected"].get("semantic_assertions", {})
                    assert all(
                        term in text for term in semantic.get("required_terms_all", [])
                    )
                    assert all(
                        term not in text for term in semantic.get("forbidden_terms", [])
                    )
                    if "max_characters" in semantic:
                        assert len(text) <= semantic["max_characters"]

                    spans = runtime.exporter.get_finished_spans()
                    by_id = {span.context.span_id: span for span in spans}
                    root = next(span for span in spans if span.name == "travel.invoke")
                    assert root.attributes["gen_ai.response.id"] == result["id"]
                    business = [
                        span
                        for span in spans
                        if span.name.startswith(("travel.tool.", "travel.model."))
                    ]
                    for span in business:
                        assert span.context.trace_id == root.context.trace_id
                        ancestor = span
                        while ancestor.context.span_id != root.context.span_id:
                            assert ancestor.parent is not None
                            ancestor = by_id[ancestor.parent.span_id]
                    assert_reviewed_tool_facts(runtime, step, business)

    asyncio.run(run())


def assert_reviewed_tool_facts(runtime, step, spans):
    """Check the finite synthetic wire facts, not a deployed qualification verdict."""
    for assertion in step["expected"].get("trace_assertions", []):
        kind = assertion["kind"]
        selected = [
            span
            for span in spans
            if span.attributes.get("gen_ai.tool.name") == assertion.get("tool_name")
        ]
        if kind == "tool_call_count":
            assert len(selected) == assertion["count"]
        elif kind == "tool_result_class":
            assert selected
            expected = assertion["result_class"] == "success"
            assert all(span.attributes["tool.ok"] is expected for span in selected)
        elif kind == "operation_sequence":
            assert [span.attributes["gen_ai.operation.name"] for span in spans] == (
                assertion["operations"]
            )
        elif kind == "payload_multiplicity":
            if assertion["source"] == "tool_result":
                assert selected
                for span in selected:
                    payload = json.loads(span.attributes["gen_ai.tool.call.result"])
                    assert payload[assertion["path"]] >= assertion["minimum"]
            else:
                assert assertion["source"] == "input_messages"
                prompt = runtime.calls[-1]["input"]
                payload = json.loads(prompt.split("Inventory search payload: ", 1)[1])
                assert len(payload[assertion["path"]]) >= assertion["minimum"]
        elif kind == "scope_relation":
            body = step["request"]["body"]
            trips = runtime.app.requested_trips(body["input"][0]["content"][0]["text"])
            assert selected
            for span in selected:
                arguments = json.loads(span.attributes["gen_ai.tool.call.arguments"])
                assert (arguments[assertion["argument"]] == trips[-1]) is (
                    assertion["request_tool_equal"]
                )
        elif kind == "span_relation":
            assert assertion["relation"] == "ordered"
            first = next(
                span
                for span in spans
                if span.attributes.get("gen_ai.tool.name") == assertion["first_tool"]
            )
            second = next(
                span
                for span in spans
                if span.attributes.get("gen_ai.tool.name") == assertion["second_tool"]
            )
            assert first.end_time <= second.start_time
        else:
            raise AssertionError(f"Uncovered Travel trace assertion: {kind}")


@pytest.mark.parametrize("runtime", ["v0", "issue-028"], indirect=True)
@pytest.mark.parametrize("adapter", ["sdk", "travel"])
def test_native_stored_turn_can_continue_an_explicit_response_chain(runtime, adapter):
    async def run():
        store = InMemoryResponseProvider()
        host = (
            ResponsesHostServer(runtime.graph, store=store)
            if adapter == "sdk"
            else runtime.app.TravelResponsesHostServer(
                runtime.graph, identity=runtime.app.RUNTIME_IDENTITY, store=store
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host._app),
            base_url="http://synthetic.test",
        ) as client:
            previous = None
            for text, expected in (
                ("Find a hotel for trip-gamma.", "trip-gamma"),
                ("Search again.", "trip-gamma"),
                (
                    "Switch to trip-beta and find a hotel.",
                    "trip-gamma"
                    if runtime.app.__name__.endswith("issue_028.app")
                    else "trip-beta",
                ),
                (
                    "Search again.",
                    "trip-gamma"
                    if runtime.app.__name__.endswith("issue_028.app")
                    else "trip-beta",
                ),
            ):
                body = {
                    "input": text,
                    "store": True,
                    "agent_session_id": "synthetic-mixed",
                }
                if previous is not None:
                    body["previous_response_id"] = previous
                response = await client.post(
                    "/responses",
                    json=body,
                    headers={"x-agent-user-id": "synthetic-user-a"},
                )
                assert response.status_code == 200
                result = response.json()
                assert result["status"] == "completed"
                assert expected in result["output"][0]["content"][0]["text"]
                previous = result["id"]
            if adapter == "travel":
                # The local SDK store is not user-partitioned. Our native alias
                # must still refuse to share the original user's checkpoint.
                other = await client.post(
                    "/responses",
                    json={
                        "input": "Search again.",
                        "store": True,
                        "previous_response_id": previous,
                    },
                    headers={"x-agent-user-id": "synthetic-user-b"},
                )
                assert other.status_code == 200
                assert other.json()["status"] == "completed"
                assert "trip-alpha" in other.json()["output"][0]["content"][0]["text"]

    asyncio.run(run())
