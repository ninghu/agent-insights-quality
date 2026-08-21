from __future__ import annotations

from typing import Any


FLIGHTS = {
    ("SEA", "PDX", "2030-05-10"): (
        "FL-SEA-PDX-101 departs 09:15 and arrives 10:05; synthetic fare USD 89."
    )
}
ITINERARIES = {
    "TRIP-001": (
        "TRIP-001 uses FL-SEA-PDX-101 and Hotel Rose for 2030-05-10. "
        "Inventory is synthetic and remains unbooked."
    )
}
HOTELS = {
    ("PDX", "2030-05-10"): "HTL-PDX-ROSE Hotel Rose; synthetic nightly rate USD 145."
}

TOOLS = [
    {
        "type": "function",
        "name": "flight_search",
        "description": "Search exact synthetic flight inventory.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["origin", "destination", "date"],
            "properties": {
                "origin": {"type": "string", "enum": ["SEA"]},
                "destination": {"type": "string", "enum": ["PDX"]},
                "date": {"type": "string", "enum": ["2030-05-10"]},
            },
        },
    },
    {
        "type": "function",
        "name": "hotel_search",
        "description": "Search exact synthetic hotel inventory.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["destination", "date"],
            "properties": {
                "destination": {"type": "string", "enum": ["PDX"]},
                "date": {"type": "string", "enum": ["2030-05-10"]},
            },
        },
    },
    {
        "type": "function",
        "name": "itinerary",
        "description": "Read a grounded synthetic itinerary.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["itinerary_id"],
            "properties": {
                "itinerary_id": {"type": "string", "enum": ["TRIP-001"]},
            },
        },
    },
    {
        "type": "function",
        "name": "booking",
        "description": "Book only exact synthetic inventory with explicit confirmation.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["inventory_id", "confirmed"],
            "properties": {
                "inventory_id": {"type": "string", "enum": ["FL-SEA-PDX-101"]},
                "confirmed": {"type": "boolean"},
            },
        },
    },
]

INSTRUCTIONS = (
    "Use exactly one registered travel tool for each supported synthetic request and return its "
    "result verbatim. Never invent inventory. Never claim a booking unless the booking tool receives "
    "confirmed=true. All inventory and bookings are synthetic."
)


def flight_search(origin: str, destination: str, date: str) -> str:
    return FLIGHTS.get(
        (origin, destination, date),
        "No matching synthetic flight inventory was found.",
    )


def itinerary(itinerary_id: str) -> str:
    return ITINERARIES.get(itinerary_id, "Synthetic itinerary was not found.")


def hotel_search(destination: str, date: str) -> str:
    return HOTELS.get(
        (destination, date),
        "No matching synthetic hotel inventory was found.",
    )


def booking(inventory_id: str, confirmed: bool) -> str:
    if not confirmed:
        return f"No booking was made for {inventory_id}; explicit confirmation is required."
    if inventory_id != "FL-SEA-PDX-101":
        return "No booking was made because the synthetic inventory is unavailable."
    return (
        "Synthetic booking BK-SYN-101 confirmed for FL-SEA-PDX-101. "
        "No real reservation or payment occurred."
    )


def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    if name == "flight_search":
        return flight_search(
            str(arguments["origin"]),
            str(arguments["destination"]),
            str(arguments["date"]),
        )
    if name == "hotel_search":
        return hotel_search(
            str(arguments["destination"]),
            str(arguments["date"]),
        )
    if name == "itinerary":
        return itinerary(str(arguments["itinerary_id"]))
    if name == "booking":
        return booking(
            str(arguments["inventory_id"]),
            bool(arguments["confirmed"]),
        )
    raise ValueError(f"Unsupported travel tool: {name}")
