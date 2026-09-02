from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


QUALITY_SCORE_FORMULA = "correct_over_expected_plus_noise_v1"
ASSESSMENT_FIELDS = (
    "title",
    "description",
    "category",
    "severity",
    "proposed_fix",
    "linked_traces",
)
SCORING_FIELDS = (
    "title",
    "description",
    "category",
    "linked_traces",
)
ATTRIBUTABLE_FINDING_TYPES = frozenset({"MATCHED", "PARTIAL", "MISMATCHED"})


def scoring_fields_pass(fields: Mapping[str, Any]) -> bool:
    return all(fields.get(field) is True for field in SCORING_FIELDS)


def issue_outcome(cards: Iterable[Mapping[str, Any]]) -> str:
    attributable = [
        card
        for card in cards
        if card.get("finding_type") in ATTRIBUTABLE_FINDING_TYPES
    ]
    if any(
        card.get("finding_type") == "MATCHED"
        and scoring_fields_pass(card.get("fields", {}))
        for card in attributable
    ):
        return "correct"
    return "incorrect" if attributable else "missing"


def calculate_quality_score(
    *,
    correct_issues: int,
    expected_issues: int,
    noise_cards: int,
    duplicate_cards: int,
) -> int | float:
    values = (correct_issues, expected_issues, noise_cards, duplicate_cards)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise TypeError("Quality-score inputs must be integers")
    if (
        correct_issues < 0
        or expected_issues <= 0
        or correct_issues > expected_issues
        or noise_cards < 0
        or duplicate_cards < 0
    ):
        raise ValueError("Quality-score inputs are outside their valid ranges")
    value = round(
        100.0
        * correct_issues
        / (expected_issues + noise_cards + duplicate_cards),
        1,
    )
    return int(value) if value.is_integer() else value
