from __future__ import annotations

import re


MAX_RESPONSE_OPTIONS = 2


def message_text(content: str | list) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif (
            isinstance(block, dict)
            and block.get("type") in {"text", "input_text"}
            and isinstance(block.get("text"), str)
        ):
            parts.append(block["text"])
        else:
            raise ValueError("Travel accepts text message content only")
    if not parts:
        raise ValueError("Travel requires nonempty message content")
    return " ".join(parts)


def requested_trips(text: str) -> list[str]:
    return list(
        dict.fromkeys(re.findall(r"\btrip-(?:alpha|beta|gamma)\b", text.lower()))
    )


def parse_trip(text: str, previous_trip: str = "trip-alpha") -> str:
    lowered = text.lower()
    if "switch" in lowered and " to " in lowered:
        destination = lowered.rsplit(" to ", 1)[1]
        switched = requested_trips(destination)
        if switched:
            return switched[0]
    trips = requested_trips(text)
    return trips[0] if trips else previous_trip


def booking_intent(text: str, trip: str, *, has_proposal: bool = False) -> bool:
    """Accept an affirmative booking command, never a mention of confirmation."""
    lowered = text.lower().replace("\u2019", "'")
    if "?" in lowered or re.search(
        r"\b(?:not|no|never|without|unconfirmed|unconfirm|cancel|"
        r"considering|maybe|perhaps|if|unless|pending|when|after|until)\b|\b\w+n't\b",
        lowered,
    ):
        return False
    trips = list(dict.fromkeys(re.findall(r"\btrip-[\w-]+\b", lowered)))
    if trips and trips != [trip]:
        return False
    if not trips and not has_proposal:
        return False
    return bool(
        re.search(
            r"(?:^|[.;]\s*)(?:yes[, ]+\s*)?(?:please\s+)?"
            r"(?:book\b|(?:i\s+)?confirm\s+(?:(?:the|my|this)\s+)?"
            r"(?:booking|reservation)\b)",
            lowered,
        )
    )


class BookingLedger:
    """Synthetic reservation service; records outlive a graph node's state update."""

    def __init__(self) -> None:
        self.records: dict[str, dict] = {}

    def reserve(self, trip: str, inventory: list[dict]) -> str:
        option_ids = [option["id"] for option in inventory if option["trip"] == trip]
        if not option_ids:
            raise ValueError("A reservation requires inventory for its trip")
        booking_id = f"booking-demo-{len(self.records) + 1}"
        self.records[booking_id] = {"trip": trip, "option_ids": option_ids}
        return booking_id


def requested_inventory_kind(text: str) -> str:
    lowered = text.lower()
    return "hotel" if "hotel" in lowered and "flight" not in lowered else "flight"


def bounded_inventory_options(
    inventory: list[dict],
    limit: int = MAX_RESPONSE_OPTIONS,
) -> list[dict]:
    trips = list(dict.fromkeys(option.get("trip") for option in inventory))
    if len(trips) >= 2:
        return [
            next(option for option in inventory if option.get("trip") == trip)
            for trip in trips
        ]
    selected = []
    selected_kinds = set()
    for option in inventory:
        kind = option.get("kind")
        if kind in selected_kinds:
            continue
        selected.append(option)
        selected_kinds.add(kind)
        if len(selected) >= limit:
            break
    return selected


def first_option_per_itinerary(branches: list[list[dict]]) -> list[dict]:
    selected = []
    for branch in branches:
        if not branch:
            continue
        option = dict(branch[0])
        option["source_id"] = option["id"]
        option["id"] = f"{option['trip']}-{option['id']}"
        selected.append(option)
    return selected


def describe_itineraries(inventory: list[dict]) -> str:
    trips = list(dict.fromkeys(option["trip"] for option in inventory))
    if len(trips) >= 2:
        return f"Compared itineraries {' and '.join(trips)}"
    if trips:
        return f"Itinerary {trips[0]}"
    return "No itinerary"


def describe_inventory(inventory: list[dict]) -> str:
    details = []
    for option in inventory:
        if option.get("kind") == "flight":
            details.append(
                f"Flight {option['id']} for {option['trip']}: "
                f"carrier {option['carrier']}, "
                f"departure {option['departure']}, price USD {option['price']}"
            )
        elif option.get("kind") == "hotel":
            details.append(
                f"Hotel {option['id']} for {option['trip']}: "
                f"property {option['property']}, "
                f"rating {option['rating']}, nightly rate USD {option['price']}"
            )
    return "; ".join(details) or "No synthetic inventory options"
