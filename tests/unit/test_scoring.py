"""Direct numeric policy edge cases; domain classifications live in results tests."""

from dataclasses import FrozenInstanceError
from decimal import Decimal
import math

import pytest

from agent_insights_quality.scoring import SCORING_POLICY, ScoreCounts, score_percentage


@pytest.mark.parametrize("field", ["correct_issues", "expected_issues", "noise_cards", "duplicate_cards"])
@pytest.mark.parametrize("value", [True, False, 1.0, float("nan"), float("inf"), "1", None, Decimal(1)])
def test_count_inputs_require_actual_integers(field, value):
    with pytest.raises(TypeError):
        ScoreCounts(**{field: value})


@pytest.mark.parametrize("field", ["correct_issues", "expected_issues", "noise_cards", "duplicate_cards"])
def test_counts_must_be_nonnegative(field):
    with pytest.raises(ValueError, match="nonnegative"):
        ScoreCounts(**{field: -1})


def test_correct_count_cannot_exceed_scored_issues():
    with pytest.raises(ValueError, match="cannot exceed"):
        ScoreCounts(correct_issues=2, expected_issues=1)


def test_unmeasured_is_not_zero_even_with_known_baseline_penalties():
    assert score_percentage(ScoreCounts()) is None
    assert score_percentage(ScoreCounts(noise_cards=4, duplicate_cards=3)) is None
    assert score_percentage(ScoreCounts(expected_issues=1)) == 0.0


def test_rounding_is_half_up_and_score_remains_finite_for_large_integer_counts():
    assert score_percentage(ScoreCounts(1, 16)) == 6.3
    for counts in (
        ScoreCounts(10**400, 10**400),
        ScoreCounts(10**400, 10**401, 10**400, 10**400),
        ScoreCounts(1, 10**400),
    ):
        score = score_percentage(counts)
        assert math.isfinite(score)
        assert 0 <= score <= 100


def test_policy_and_counts_are_immutable_and_not_caller_configurable():
    with pytest.raises(FrozenInstanceError):
        SCORING_POLICY.duplicate_weight = 1
    with pytest.raises(FrozenInstanceError):
        ScoreCounts().noise_cards = 1
    with pytest.raises(TypeError):
        type(SCORING_POLICY)(duplicate_weight=1)
    with pytest.raises(TypeError):
        score_percentage({"expected_issues": 1})
