from __future__ import annotations

import asyncio
import json
import os
from typing import TypedDict

from azure.identity.aio import DefaultAzureCredential
from langchain_core.messages import AIMessage, AnyMessage
from langchain_azure_ai.agents.hosting import ResponsesHostServer
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from opentelemetry import trace
from openai import AsyncOpenAI
from typing_extensions import Annotated

from .config import load_config
from .observability import configure_observability


configure_observability("travel-agent")
tracer = trace.get_tracer("travel-agent")
config = load_config()
mode = config["injection"].get("mode", "none")
credential = DefaultAzureCredential()


async def token_provider() -> str:
    return (await credential.get_token("https://ai.azure.com/.default")).token


class TravelState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    trip: str
    inventory: list[dict]
    validated: bool
    confirmed: bool
    booked: bool
    errors: list[str]


def latest_text(state: TravelState) -> str:
    content = state["messages"][-1].content
    return content if isinstance(content, str) else str(content)


def requested_trips(text: str) -> list[str]:
    lowered = text.lower()
    return [
        trip
        for trip in ("trip-alpha", "trip-beta", "trip-gamma")
        if trip in lowered
    ]


def parse_trip(text: str) -> str:
    trips = requested_trips(text)
    return trips[0] if trips else "trip-alpha"


async def failed_search(name: str) -> None:
    with tracer.start_as_current_span(f"travel.tool.{name}") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", name)
        span.set_attribute("tool.ok", False)


async def search_flights(trip: str, include_details: bool = False) -> list[dict]:
    with tracer.start_as_current_span("travel.tool.search_flights") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", "search_flights")
        span.set_attribute("tool.ok", True)
        span.set_attribute(
            "gen_ai.tool.call.arguments",
            json.dumps(
                {"trip": trip, "include_details": include_details},
                sort_keys=True,
            ),
        )
        await asyncio.sleep(0.01)
        count = 80 if include_details else 1
        result = [
            {"id": f"flight-demo-{index}", "trip": trip, "price": 200 + index}
            for index in range(count)
        ]
        span.set_attribute(
            "gen_ai.tool.call.result",
            json.dumps({"result_count": len(result)}, sort_keys=True),
        )
        return result


async def search_hotels(trip: str, include_details: bool = False) -> list[dict]:
    with tracer.start_as_current_span("travel.tool.search_hotels") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", "search_hotels")
        span.set_attribute("tool.ok", True)
        span.set_attribute(
            "gen_ai.tool.call.arguments",
            json.dumps(
                {"trip": trip, "include_details": include_details},
                sort_keys=True,
            ),
        )
        await asyncio.sleep(0.01)
        count = 80 if include_details else 1
        result = [
            {"id": f"hotel-demo-{index}", "trip": trip, "price": 120 + index}
            for index in range(count)
        ]
        span.set_attribute(
            "gen_ai.tool.call.result",
            json.dumps({"result_count": len(result)}, sort_keys=True),
        )
        return result


def build_graph():
    async def plan(state: TravelState) -> TravelState:
        text = latest_text(state)
        trip = state.get("trip") if mode == "stale_trip_state" else None
        return {
            "trip": trip or parse_trip(text),
            "confirmed": "confirm" in text.lower(),
            "errors": [],
        }

    async def search(state: TravelState) -> TravelState:
        text = latest_text(state).lower()
        trip = state["trip"]
        if mode == "fabricate_after_failure":
            await failed_search("search_flights")
            return {
                "errors": ["inventory_search_failed"],
                "inventory": [{"id": "invented-demo-seat", "trip": trip}],
            }
        if mode == "omit_inventory_search":
            return {"inventory": []}
        if mode == "none" and "temporary flight search" in text:
            await failed_search("search_flights")
            return {"inventory": await search_flights(trip)}
        if mode == "none" and "hotel search is unavailable" in text:
            flights = await search_flights(trip)
            await failed_search("search_hotels")
            return {"inventory": flights, "errors": ["hotel_search_unavailable"]}
        if mode == "wrong_travel_tool" and "flight" in text:
            return {"inventory": await search_hotels(trip)}
        trips = requested_trips(text)
        if mode == "drop_comparison_branch" and len(trips) >= 2:
            branches = await asyncio.gather(*(search_flights(item) for item in trips))
            inventory = [item for branch in branches for item in branch]
            return {"inventory": [item for item in inventory if item["trip"] == trips[0]]}
        include_details = mode == "overfetch_inventory"
        wants_flight = "flight" in text or "compare" in text
        wants_hotel = "hotel" in text or "compare" in text
        if wants_flight and wants_hotel:
            if mode == "sequential_searches":
                flights = await search_flights(trip, include_details)
                hotels = await search_hotels(trip, include_details)
            else:
                flights, hotels = await asyncio.gather(
                    search_flights(trip, include_details),
                    search_hotels(trip, include_details),
                )
            inventory = flights + hotels
        elif wants_hotel:
            inventory = await search_hotels(trip, include_details)
        else:
            inventory = await search_flights(trip, include_details)
        if mode == "book_before_validation":
            return {"inventory": inventory, "booked": True}
        return {"inventory": inventory}

    async def validate(state: TravelState) -> TravelState:
        valid = bool(state.get("inventory")) and not state.get("errors")
        return {"validated": valid}

    async def book(state: TravelState) -> TravelState:
        if mode == "book_before_validation":
            return {}
        return {"booked": bool(state.get("validated") and state.get("confirmed"))}

    async def respond(state: TravelState) -> TravelState:
        if mode == "fabricate_after_failure":
            answer = "A seat is available on invented-demo-seat."
        elif mode == "omit_inventory_search":
            answer = "Inventory is available even though no authoritative search ran."
        elif mode == "none" and state.get("errors"):
            answer = (
                f"Partial result: {len(state.get('inventory', []))} synthetic options found; "
                f"{', '.join(state['errors'])}."
            )
        else:
            status = "booked" if state.get("booked") else "not booked"
            answer = (
                f"{len(state.get('inventory', []))} synthetic options found; "
                f"{status} for {state['trip']}."
            )
        grounded = await model_answer(
            "Return one concise sentence preserving these exact synthetic facts: " + answer
        )
        return {"messages": [AIMessage(content=grounded)]}

    builder = StateGraph(TravelState)
    builder.add_node("plan", plan)
    builder.add_node("search", search)
    builder.add_node("validate", validate)
    builder.add_node("book", book)
    builder.add_node("respond", respond)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "search")
    builder.add_edge("search", "validate")
    builder.add_edge("validate", "book")
    builder.add_edge("book", "respond")
    builder.add_edge("respond", END)
    return builder.compile(checkpointer=InMemorySaver())


async def model_answer(prompt: str) -> str:
    model = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "terra-test-agents")
    client = AsyncOpenAI(
        base_url=os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/") + "/openai/v1",
        api_key=token_provider,
    )
    with tracer.start_as_current_span("travel.model.respond") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", model)
        result = await client.responses.create(
            model=model,
            input=prompt,
            max_output_tokens=200,
            store=False,
        )
        return result.output_text


def main() -> None:
    port = int(os.environ.get("PORT", "8088"))
    ResponsesHostServer(build_graph()).run(port=port)


if __name__ == "__main__":
    main()
