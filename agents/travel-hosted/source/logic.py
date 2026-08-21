from __future__ import annotations

import re


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


def flight_search(origin: str, destination: str, date: str) -> str:
    return FLIGHTS.get(
        (origin, destination, date),
        "No matching synthetic flight inventory was found.",
    )


def itinerary(itinerary_id: str) -> str:
    return ITINERARIES.get(itinerary_id, "Synthetic itinerary was not found.")


def booking(inventory_id: str, confirmed: bool) -> str:
    if not confirmed:
        return f"No booking was made for {inventory_id}; explicit confirmation is required."
    if inventory_id != "FL-SEA-PDX-101":
        return "No booking was made because the synthetic inventory is unavailable."
    return (
        "Synthetic booking BK-SYN-101 confirmed for FL-SEA-PDX-101. "
        "No real reservation or payment occurred."
    )


def handle(user_input: str) -> str:
    if user_input.startswith("search-trip "):
        return flight_search(
            _value(user_input, "origin"),
            _value(user_input, "destination"),
            _value(user_input, "date"),
        )
    if user_input.startswith("plan-itinerary "):
        return itinerary(_value(user_input, "itinerary"))
    if user_input.startswith("request-booking "):
        confirmed = _value(user_input, "confirmed").casefold() == "true"
        return booking(_value(user_input, "inventory"), confirmed)
    return "Supported synthetic tasks: search-trip, plan-itinerary, and request-booking."


def _value(text: str, key: str) -> str:
    match = re.search(rf"(?:^|\s){re.escape(key)}=([A-Za-z0-9-]+)", text)
    return match.group(1) if match else ""
