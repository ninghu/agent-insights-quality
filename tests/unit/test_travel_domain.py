"""Pure Travel domain checks: no hosting, Azure, model or tracing imports."""

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2] / "agents" / "travel-agent"
VERSIONS = ["v0", *(f"issue-{number:03}" for number in range(21, 29))]


def version_path(version):
    return ROOT / "v0" if version == "v0" else ROOT / "issues" / version


@pytest.fixture(params=VERSIONS)
def domain(request):
    path = version_path(request.param) / "source" / "options.py"
    spec = importlib.util.spec_from_file_location("travel_domain", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "text",
    [
        "Do not confirm booking for trip-beta.",
        "Don't book a flight for trip-beta.",
        "The flight for trip-beta is unconfirmed.",
        "Show the confirmation for trip-beta.",
        "Check whether I confirmed booking for trip-beta.",
        "Can you book trip-beta?",
        "If the fare falls, book trip-beta.",
        "Please book trip-beta after I confirm.",
        "Book trip-beta when I approve.",
        "I am only considering a flight for trip-beta; do not book it.",
        "Book trip-alpha.",
        "Book trip-delta.",
        "Switch from trip-alpha to trip-beta and confirm a flight.",
        "Confirm availability for trip-beta.",
    ],
)
def test_booking_mentions_are_not_scoped_affirmative_consent(domain, text):
    assert not domain.booking_intent(text, "trip-beta", has_proposal=True)


@pytest.mark.parametrize(
    "text",
    [
        "Book a flight for trip-beta.",
        "Please book a hotel for trip-beta.",
        "I confirm the booking for trip-beta.",
        "Confirm my reservation for trip-beta.",
        "Fixed synthetic case 01. Please book trip-beta.",
    ],
)
def test_explicit_affirmative_booking(domain, text):
    assert domain.booking_intent(text, "trip-beta")


def test_implicit_booking_requires_a_prior_proposal(domain):
    assert not domain.booking_intent("Yes, book it.", "trip-beta")
    assert domain.booking_intent("Yes, book it.", "trip-beta", has_proposal=True)


def test_structured_text_normalization(domain):
    content = [{"type": "text", "text": "Please book a flight for trip-beta."}]
    assert domain.booking_intent(domain.message_text(content), "trip-beta")
    assert (
        domain.message_text([{"type": "input_text", "text": "No booking."}])
        == "No booking."
    )
    with pytest.raises(ValueError, match="text message"):
        domain.message_text([{"type": "image", "url": "synthetic"}])
    with pytest.raises(ValueError, match="nonempty"):
        domain.message_text([])


@pytest.mark.parametrize(
    "version", ["issue-024", "issue-025", "issue-026", "issue-028"]
)
def test_updated_probe_expectations_match_request_counterparts(version):
    traffic = json.loads(
        (version_path(version) / "traffic.json").read_text(encoding="utf-8")
    )
    requests = {request["id"]: request for request in traffic["requests"]}
    scenario = traffic["validation_rules"]["scenarios"][0]
    assert (scenario["n"], scenario["k"], len(scenario["attempts"])) == (10, 6, 10)
    for attempt in scenario["attempts"]:
        source_ids = attempt["parameters"]["source_request_ids"]
        source = requests[source_ids[-1]]
        probe = attempt["probe_steps"][0]
        for field in ("semantic_assertions", "trace_assertions"):
            assert probe["expected"].get(
                field, [] if field == "trace_assertions" else {}
            ) == (
                source["expected"].get(field, [] if field == "trace_assertions" else {})
            )
        if version == "issue-028":
            setup = requests[source_ids[0]]
            assert (
                setup["request"]["body"]["conversation"]
                == source["request"]["body"]["conversation"]
            )
            assert (
                attempt["setup_steps"][0]["expected"]["semantic_assertions"]
                == (setup["expected"]["semantic_assertions"])
            )


def test_trip_order_and_conversation_fallback(domain):
    assert domain.requested_trips(
        "Compare trip-gamma and trip-beta, then trip-gamma."
    ) == ["trip-gamma", "trip-beta"]
    assert domain.requested_trips("trip-alphabet and trip-betamax") == []
    assert domain.parse_trip("Switch from trip-gamma to trip-beta.") == "trip-beta"
    assert domain.parse_trip("Search again.", "trip-gamma") == "trip-gamma"


def test_bounded_grounded_options_and_reservation_records(domain):
    inventory = [
        {
            "id": "flight-demo-0",
            "kind": "flight",
            "trip": "trip-beta",
            "carrier": "Contoso Air",
            "departure": "09:00",
            "price": 200,
        },
        {
            "id": "flight-demo-1",
            "kind": "flight",
            "trip": "trip-beta",
            "carrier": "Contoso Air",
            "departure": "09:00",
            "price": 201,
        },
        {
            "id": "hotel-demo-0",
            "kind": "hotel",
            "trip": "trip-beta",
            "property": "Fabrikam Stay",
            "rating": 4.5,
            "price": 120,
        },
    ]
    selected = domain.bounded_inventory_options(inventory)
    assert [item["id"] for item in selected] == ["flight-demo-0", "hotel-demo-0"]
    assert len(domain.bounded_inventory_options(inventory, 1)) == 1
    text = domain.describe_inventory(selected)
    assert "USD 200" in text and "USD 120" in text
    assert "flight-demo-1" not in text
    ledger = domain.BookingLedger()
    booking_id = ledger.reserve("trip-beta", selected)
    selected.clear()
    assert ledger.records[booking_id] == {
        "trip": "trip-beta",
        "option_ids": ["flight-demo-0", "hotel-demo-0"],
    }
    with pytest.raises(ValueError, match="requires inventory"):
        ledger.reserve("trip-alpha", inventory)
    assert len(ledger.records) == 1


def test_every_comparison_probe_keeps_first_requested_itinerary(domain):
    traffic = json.loads(
        (version_path("issue-026") / "traffic.json").read_text(encoding="utf-8")
    )
    scenario = traffic["validation_rules"]["scenarios"][0]
    assert (scenario["validation_mode"], scenario["n"], scenario["k"]) == (
        "model_mediated",
        10,
        6,
    )
    for attempt in scenario["attempts"]:
        probe = attempt["probe_steps"][0]
        trips = domain.requested_trips(
            probe["request"]["body"]["input"][0]["content"][0]["text"]
        )
        assertions = probe["expected"]["semantic_assertions"]
        assert assertions["required_terms_all"] == trips[:1]
        assert assertions["forbidden_terms"] == trips[1:]
        branches = [[{"id": "flight-demo-0", "trip": trip}] for trip in trips]
        assert [
            item["trip"] for item in domain.first_option_per_itinerary(branches)
        ] == trips
