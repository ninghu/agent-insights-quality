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
    lowered = text.lower()
    if "switch" in lowered and " to " in lowered:
        destination = lowered.rsplit(" to ", 1)[1]
        switched = requested_trips(destination)
        if switched:
            return switched[0]
    trips = requested_trips(text)
    return trips[0] if trips else "trip-alpha"


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
