"""Pure factual-grammar checks; no Hosted, tracing, credential or SDK imports."""

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2] / "agents" / "travel-agent"
VERSIONS = ["v0", *(f"issue-{number:03}" for number in range(21, 29))]
FLIGHT = {
    "id": "flight-demo-0", "kind": "flight", "trip": "trip-beta",
    "carrier": "Contoso Air", "departure": "09:00", "price": 200,
}
HOTEL = {
    "id": "hotel-demo-0", "kind": "hotel", "trip": "trip-beta",
    "property": "Fabrikam Stay", "rating": 4.5, "price": 120,
}


@pytest.fixture(params=VERSIONS)
def rendering(request):
    version = ROOT / "v0" if request.param == "v0" else ROOT / "issues" / request.param
    spec = importlib.util.spec_from_file_location("travel_rendering", version / "source" / "rendering.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_distinct_complete_wordings_preserve_every_selected_fact(rendering):
    groups = rendering.answer_facts([FLIGHT, HOTEL], [FLIGHT, HOTEL], [], False)
    first = "\n".join(group[0] for group in groups)
    second = "\n".join(group[1] for group in reversed(groups))
    assert first != second
    for text in (first, second, first + "\n"):
        assert rendering.validate_answer(text, groups) is text
        for fact in ("trip-beta", "flight-demo-0", "hotel-demo-0", "Contoso Air",
                     "09:00", "USD 200", "Fabrikam Stay", "4.5", "USD 120",
                     "Booking not completed", "Showing 2 of 2"):
            assert fact in text
    assert sum(max(map(len, choices)) for choices in groups) + len(groups) - 1 < 600


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "foreign", "polarity", "json", "oversized"])
def test_model_prose_cannot_drop_add_or_invert_a_claim(rendering, corruption):
    groups = rendering.answer_facts([FLIGHT], [FLIGHT], [], False)
    valid = "\n".join(group[0] for group in groups)
    invalid = {
        "missing": "\n".join(valid.split("\n")[:-1]),
        "duplicate": valid + "\n" + groups[0][0],
        "foreign": valid + "\nA free hotel for trip-gamma is included.",
        "polarity": valid.replace("Booking not completed", "Booking completed"),
        "json": '{"answer": "Itinerary trip-beta."}',
        "oversized": "x" * 601,
    }[corruption]
    with pytest.raises(rendering.InvalidTravelAnswer, match="travel_model_output_invalid"):
        rendering.validate_answer(invalid, groups)


def test_rendering_reflects_selected_state_not_an_ideal_repaired_result(rendering):
    groups = rendering.answer_facts([HOTEL], [HOTEL], [], True)
    text = rendering.validate_answer("\n".join(group[0] for group in groups), groups)
    assert "hotel-demo-0" in text and "flight-demo-0" not in text
    assert "trip-beta" in text and "trip-gamma" not in text
    assert "Booking completed" in text
    groups = rendering.answer_facts([], [], [], False)
    text = "\n".join(group[0] for group in groups)
    assert rendering.validate_answer(text, groups) == text
    assert "No itinerary" in text and "No synthetic inventory options" in text


def test_partial_and_bounded_single_option_results_keep_evidence_gaps(rendering):
    groups = rendering.answer_facts([FLIGHT], [FLIGHT], ["hotel_search_unavailable"], False)
    text = rendering.validate_answer("\n".join(group[1] for group in groups), groups)
    assert "Partial result" in text and "hotel_search_unavailable" in text
    groups = rendering.answer_facts([FLIGHT, {**FLIGHT, "id": "flight-demo-1"}], [FLIGHT], [], False)
    assert sum(max(map(len, choices)) for choices in groups) + len(groups) - 1 < 300
