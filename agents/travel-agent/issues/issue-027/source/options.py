from __future__ import annotations


MAX_RESPONSE_OPTIONS = 2


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


def bounded_inventory_options(
    inventory: list[dict],
    limit: int = MAX_RESPONSE_OPTIONS,
) -> list[dict]:
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
    return [branch[0] for branch in branches if branch]


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
                f"Flight {option['id']}: carrier {option['carrier']}, "
                f"departure {option['departure']}, price USD {option['price']}"
            )
        elif option.get("kind") == "hotel":
            details.append(
                f"Hotel {option['id']}: property {option['property']}, "
                f"rating {option['rating']}, nightly rate USD {option['price']}"
            )
    return "; ".join(details) or "No synthetic inventory options"
