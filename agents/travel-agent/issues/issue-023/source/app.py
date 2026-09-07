from __future__ import annotations

import json
import os
from asyncio import sleep
from typing import TypedDict

from azure.identity.aio import DefaultAzureCredential
from langchain_core.messages import AIMessage, AnyMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from opentelemetry import trace
from openai import APIStatusError, AsyncOpenAI
from typing_extensions import Annotated

from .observability import configure_observability
from .hosting import TravelResponsesHostServer
from .runtime_identity import require_foundry_runtime_identity
from .rendering import INSTRUCTIONS, InvalidTravelAnswer, answer_facts, validate_answer
from .options import (
    MAX_RESPONSE_OPTIONS,
    BookingLedger,
    booking_intent,
    bounded_inventory_options,
    message_text,
    parse_trip,
    requested_inventory_kind,
    requested_trips,
)


RUNTIME_IDENTITY = require_foundry_runtime_identity()
configure_observability(RUNTIME_IDENTITY.name, RUNTIME_IDENTITY.version)
tracer = trace.get_tracer(RUNTIME_IDENTITY.name, RUNTIME_IDENTITY.version)


class TravelState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    trip: str
    inventory: list[dict]
    validated: bool
    confirmed: bool
    booked: bool
    errors: list[str]
    active: bool
    kind: str
    booking_id: str | None
    has_proposal: bool


def latest_text(state: TravelState) -> str:
    return message_text(state["messages"][-1].content)


class InventoryUnavailable(RuntimeError):
    pass


async def search_flights(
    trip: str, include_details: bool = False, *, unavailable: bool = False
) -> list[dict]:
    with RUNTIME_IDENTITY.start_span(tracer, "travel.tool.search_flights") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", "search_flights")
        span.set_attribute("tool.ok", not unavailable)
        span.set_attribute(
            "gen_ai.tool.call.arguments",
            json.dumps(
                {
                    "trip": trip,
                    "include_details": include_details,
                    "unavailable": unavailable,
                },
                sort_keys=True,
            ),
        )
        await sleep(0.01)
        if unavailable:
            raise InventoryUnavailable("flight_search_unavailable")
        count = 80 if include_details else 2
        result = [
            {
                "id": f"flight-demo-{index}",
                "kind": "flight",
                "trip": trip,
                "carrier": "Contoso Air",
                "departure": "09:00",
                "price": 200 + index,
            }
            for index in range(count)
        ]
        span.set_attribute(
            "gen_ai.tool.call.result",
            json.dumps(
                {"result_count": len(result), "inventory": result}, sort_keys=True
            ),
        )
        return result


async def search_hotels(
    trip: str, include_details: bool = False, *, unavailable: bool = False
) -> list[dict]:
    with RUNTIME_IDENTITY.start_span(tracer, "travel.tool.search_hotels") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", "search_hotels")
        span.set_attribute("tool.ok", not unavailable)
        span.set_attribute(
            "gen_ai.tool.call.arguments",
            json.dumps(
                {
                    "trip": trip,
                    "include_details": include_details,
                    "unavailable": unavailable,
                },
                sort_keys=True,
            ),
        )
        await sleep(0.01)
        if unavailable:
            raise InventoryUnavailable("hotel_search_unavailable")
        count = 80 if include_details else 2
        result = [
            {
                "id": f"hotel-demo-{index}",
                "kind": "hotel",
                "trip": trip,
                "property": "Fabrikam Stay",
                "rating": 4.5,
                "price": 120 + index,
            }
            for index in range(count)
        ]
        span.set_attribute(
            "gen_ai.tool.call.result",
            json.dumps(
                {"result_count": len(result), "inventory": result}, sort_keys=True
            ),
        )
        return result


def build_graph(*, bookings: BookingLedger | None = None):
    ledger = bookings if bookings is not None else BookingLedger()

    async def plan(state: TravelState) -> TravelState:
        text = latest_text(state)
        active = bool(requested_trips(text)) or any(
            word in text.lower()
            for word in ("flight", "hotel", "book", "search", "compare", "reservation")
        )
        update: TravelState = {
            "active": active,
            "validated": False,
            "confirmed": False,
            "booked": False,
            "booking_id": None,
            "inventory": [],
            "errors": [],
        }
        if not active:
            return update
        update.update(
            {
                "trip": parse_trip(text, state.get("trip", "trip-alpha")),
                "kind": (
                    requested_inventory_kind(text)
                    if "flight" in text.lower() or "hotel" in text.lower()
                    else state.get("kind", "flight")
                ),
            }
        )
        update["has_proposal"] = bool(
            state.get("inventory")
            and not state.get("errors")
            and state.get("trip") == update["trip"]
            and state.get("kind") == update["kind"]
            and all(option["trip"] == update["trip"] for option in state["inventory"])
        )
        return update

    async def search(state: TravelState) -> TravelState:
        return {"inventory": []}

    async def validate(state: TravelState) -> TravelState:
        with RUNTIME_IDENTITY.start_span(tracer, "travel.validate") as span:
            valid = bool(state.get("inventory")) and not state.get("errors")
            span.set_attribute("travel.availability.valid", valid)
            return {"validated": valid}

    async def confirm(state: TravelState) -> TravelState:
        with RUNTIME_IDENTITY.start_span(tracer, "travel.confirm") as span:
            confirmed = booking_intent(
                latest_text(state), state["trip"], has_proposal=state["has_proposal"]
            )
            span.set_attribute("travel.booking.confirmed", confirmed)
            return {"confirmed": confirmed}

    async def book(state: TravelState) -> TravelState:
        if not (state.get("validated") and state.get("confirmed")):
            return {"booked": False}
        with RUNTIME_IDENTITY.start_span(tracer, "travel.tool.book") as span:
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("gen_ai.tool.name", "book")
            options = bounded_inventory_options(state["inventory"])
            span.set_attribute(
                "gen_ai.tool.call.arguments",
                json.dumps(
                    {
                        "trip": state["trip"],
                        "option_ids": [option["id"] for option in options],
                    },
                    sort_keys=True,
                ),
            )
            booking_id = ledger.reserve(state["trip"], options)
            span.set_attribute("tool.ok", True)
            span.set_attribute(
                "gen_ai.tool.call.result",
                json.dumps(
                    {
                        "booking_id": booking_id,
                        **ledger.records[booking_id],
                    },
                    sort_keys=True,
                ),
            )
            return {"booked": True, "booking_id": booking_id}

    async def respond(state: TravelState) -> TravelState:
        if not state.get("active"):
            return {
                "messages": [
                    AIMessage(content="Synthetic conversation context acknowledged.")
                ]
            }
        inventory = state.get("inventory", [])
        option_limit = (
            1 if "one " in latest_text(state).lower() else MAX_RESPONSE_OPTIONS
        )
        response_options = bounded_inventory_options(inventory, option_limit)
        facts = answer_facts(
            inventory, response_options, state.get("errors", []), state.get("booked", False)
        )
        selected_state = {
            key: state.get(key) for key in (
                "trip", "validated", "confirmed", "booked", "booking_id", "errors",
            )
        }
        selected_state.update(inventory=response_options, inventory_total=len(inventory))
        prompt = json.dumps({
            "external_request": latest_text(state), "fact_sentence_choices": facts,
        })
        answer = await render_answer(
            prompt, facts, selected_state,
        )
        return {"messages": [AIMessage(content=answer)]}

    builder = StateGraph(TravelState)
    builder.add_node("plan", plan)
    builder.add_node("search", search)
    builder.add_node("validate", validate)
    builder.add_node("confirm", confirm)
    builder.add_node("book", book)
    builder.add_node("respond", respond)
    builder.add_edge(START, "plan")
    builder.add_conditional_edges(
        "plan", lambda state: "search" if state["active"] else "respond"
    )
    builder.add_edge("search", "validate")
    builder.add_edge("validate", "confirm")
    builder.add_edge("confirm", "book")
    builder.add_edge("book", "respond")
    builder.add_edge("respond", END)
    return builder.compile(checkpointer=InMemorySaver())


async def render_answer(prompt: str, facts: tuple[tuple[str, ...], ...], selected_state: dict) -> str:
    model = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.4-mini")
    async with DefaultAzureCredential() as credential:

        async def token_provider() -> str:
            return (await credential.get_token("https://ai.azure.com/.default")).token

        async with AsyncOpenAI(
            base_url=os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/") + "/openai/v1",
            api_key=token_provider,
            max_retries=0,
            timeout=60.0,
        ) as client:
            with RUNTIME_IDENTITY.start_span(tracer, "travel.model.answer") as span:
                span.set_attribute("gen_ai.operation.name", "chat")
                span.set_attribute("travel.render.internal", False)
                span.set_attribute("travel.render.output_validated", False)
                span.set_attribute("travel.render.role", "final_response_wording")
                span.set_attribute("travel.render.selected_state", json.dumps(selected_state))
                span.set_attribute("travel.render.fact_sentence_choices", json.dumps(facts))
                span.set_attribute("gen_ai.request.model", model)
                span.set_attribute("gen_ai.request.max_tokens", 200)
                span.set_attribute(
                    "gen_ai.input.messages",
                    json.dumps(
                        [
                            {
                                "role": "system",
                                "parts": [{"type": "text", "content": INSTRUCTIONS}],
                            },
                            {
                                "role": "user",
                                "parts": [{"type": "text", "content": prompt}],
                            }
                        ]
                    ),
                )
                try:
                    response = await client.responses.create(
                        model=model, instructions=INSTRUCTIONS, input=prompt,
                        max_output_tokens=200, store=False,
                    )
                except APIStatusError as error:
                    span.set_attribute("travel.render.raw_response", error.response.text)
                    raise
                span.set_attribute("travel.render.raw_response", response.model_dump_json())
                span.set_attribute("gen_ai.response.id", response.id)
                span.set_attribute("gen_ai.response.model", response.model)
                span.set_attribute(
                    "gen_ai.output.messages",
                    json.dumps(
                        [
                            {
                                "role": "assistant",
                                "parts": [
                                    {"type": "text", "content": response.output_text}
                                ],
                            }
                        ]
                    ),
                )
                if response.usage is not None:
                    span.set_attribute(
                        "gen_ai.usage.input_tokens", response.usage.input_tokens
                    )
                    span.set_attribute(
                        "gen_ai.usage.output_tokens", response.usage.output_tokens
                    )
                if response.error is not None:
                    raise InvalidTravelAnswer("travel_model_response_failed")
                if response.status != "completed":
                    raise InvalidTravelAnswer("travel_model_response_incomplete")
                answer = validate_answer(response.output_text, facts)
                span.set_attribute("travel.render.output_validated", True)
                return answer


def main() -> None:
    port = int(os.environ.get("PORT", "8088"))
    TravelResponsesHostServer(build_graph(), identity=RUNTIME_IDENTITY).run(port=port)


if __name__ == "__main__":
    main()
