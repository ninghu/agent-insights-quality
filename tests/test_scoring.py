from __future__ import annotations

from agent_insights_quality.scoring import (
    calculate_quality_score,
    issue_outcome,
)


def _fields(**overrides: bool) -> dict[str, bool]:
    values = {
        "title": True,
        "description": True,
        "category": True,
        "severity": True,
        "proposed_fix": True,
        "linked_traces": True,
    }
    values.update(overrides)
    return values


def test_diagnostic_fields_do_not_change_a_correct_outcome() -> None:
    cards = [
        {
            "finding_type": "MATCHED",
            "fields": _fields(severity=False, proposed_fix=False),
        }
    ]
    assert issue_outcome(cards) == "correct"


def test_scoring_field_failure_is_incorrect_but_noise_is_missing() -> None:
    assert issue_outcome(
        [
            {
                "finding_type": "MISMATCHED",
                "fields": _fields(linked_traces=False),
            }
        ]
    ) == "incorrect"
    assert issue_outcome(
        [{"finding_type": "NOISE", "fields": _fields()}]
    ) == "missing"


def test_noise_and_duplicates_expand_the_score_denominator() -> None:
    assert calculate_quality_score(
        correct_issues=17,
        expected_issues=20,
        noise_cards=1,
        duplicate_cards=1,
    ) == 77.3
