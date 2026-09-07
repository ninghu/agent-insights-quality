"""Direct numeric policy edge cases; domain classifications live in results tests."""

from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext
import json
import math

import pytest

from agent_insights_quality.scoring import (
    LEGACY_SCORING_POLICY, SCORING_POLICY, ScoreCounts, ScoringPolicy, score_percentage,
)


LEGACY_SERIALIZED = {
    "version": "unique-issues-noise-1-duplicate-025-v1",
    "formula": "100*C/(E_scored+N_scored+0.25*D_scored)",
    "noise_weight": 1,
    "duplicate_weight": 0.25,
    "rounding": "half-up-one-decimal",
}
V2_SERIALIZED = {
    "version": "unique-issues-noise-1-duplicate-05-miss-025-v2",
    "formula": "100*C/(C+N_scored+0.5*D_scored+0.25*(E_scored-C))",
    "noise_weight": 1,
    "duplicate_weight": 0.5,
    "miss_weight": 0.25,
    "rounding": "half-up-one-decimal",
}
POLICIES = (LEGACY_SCORING_POLICY, SCORING_POLICY)


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


@pytest.mark.parametrize("policy", POLICIES)
def test_unmeasured_is_not_zero_even_with_known_baseline_penalties(policy):
    assert score_percentage(ScoreCounts(), policy) is None
    assert score_percentage(ScoreCounts(noise_cards=4, duplicate_cards=3), policy) is None
    assert score_percentage(ScoreCounts(expected_issues=1), policy) == 0.0
    assert score_percentage(ScoreCounts(0, 20, 4, 3), policy) == 0.0


@pytest.mark.parametrize("policy,tie_expected", [(LEGACY_SCORING_POLICY, 16), (SCORING_POLICY, 61)])
def test_rounding_is_half_up_and_score_remains_finite_for_large_integer_counts(
    policy, tie_expected,
):
    assert score_percentage(ScoreCounts(1, tie_expected), policy) == 6.3
    assert score_percentage(ScoreCounts(1, tie_expected + 1), policy) == (
        5.9 if policy == LEGACY_SCORING_POLICY else 6.2
    )
    for counts in (
        ScoreCounts(10**400, 10**400),
        ScoreCounts(10**400, 10**401, 10**400, 10**400),
        ScoreCounts(1, 10**400),
    ):
        with localcontext() as context:
            context.prec = 2
            score = score_percentage(counts, policy)
        assert math.isfinite(score)
        assert 0 <= score <= 100


@pytest.mark.parametrize("counts,legacy,v2", [
    (ScoreCounts(14, 19), 73.7, 91.8),
    (ScoreCounts(19, 20), 95.0, 98.7),
    (ScoreCounts(19, 20, 1), 90.5, 93.8),
    (ScoreCounts(20, 20, 0, 4), 95.2, 90.9),
    (ScoreCounts(20, 20, 2, 2), 88.9, 87.0),
    (ScoreCounts(1, 1, 1, 1), 44.4, 40.0),
    (ScoreCounts(2, 3, 1, 1), 47.1, 53.3),
])
def test_versioned_integer_scores_and_default_v2(counts, legacy, v2):
    assert score_percentage(counts, LEGACY_SCORING_POLICY) == legacy
    assert score_percentage(counts, SCORING_POLICY) == v2
    assert score_percentage(counts) == v2


@pytest.mark.parametrize("policy", POLICIES)
def test_policy_and_counts_are_immutable_and_not_caller_configurable(policy):
    with pytest.raises(FrozenInstanceError):
        policy.duplicate_weight = 1
    with pytest.raises(FrozenInstanceError):
        policy.version = "unreviewed"
    with pytest.raises(FrozenInstanceError):
        ScoreCounts().noise_cards = 1
    for field in ("duplicate_weight", "noise_weight", "miss_weight", "formula", "rounding"):
        with pytest.raises(TypeError):
            ScoringPolicy(**{field: 1})
    with pytest.raises(TypeError):
        score_percentage({"expected_issues": 1})


@pytest.mark.parametrize("policy,serialized", [
    (LEGACY_SCORING_POLICY, LEGACY_SERIALIZED), (SCORING_POLICY, V2_SERIALIZED),
])
def test_closed_policy_selection_and_exact_serialized_shapes(policy, serialized):
    assert ScoringPolicy(version=policy.version) == policy
    assert ScoringPolicy.from_dict(serialized=json.loads(json.dumps(serialized))) == policy
    assert policy.to_dict() == serialized
    assert json.dumps(policy.to_dict()) == json.dumps(serialized)
    copied = policy.to_dict()
    copied["noise_weight"] = 999
    assert policy.noise_weight == 1
    assert ScoringPolicy() == SCORING_POLICY


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("field", ["version", "formula", "noise_weight", "duplicate_weight", "rounding"])
def test_restoration_requires_all_approved_fields(policy, field):
    serialized = policy.to_dict()
    del serialized[field]
    with pytest.raises((TypeError, ValueError)):
        ScoringPolicy.from_dict(serialized)


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("mutation", [
    {"version": "unknown"},
    {"version": True},
    {"formula": "100"},
    {"noise_weight": True},
    {"noise_weight": False},
    {"noise_weight": "1"},
    {"noise_weight": 0.5},
    {"duplicate_weight": 1},
    {"duplicate_weight": True},
    {"duplicate_weight": float("nan")},
    {"duplicate_weight": float("inf")},
    {"rounding": "bankers"},
    {"arbitrary": "unreviewed"},
])
def test_restoration_rejects_tampered_policies_without_coercion(policy, mutation):
    with pytest.raises((TypeError, ValueError)):
        ScoringPolicy.from_dict(policy.to_dict() | mutation)


@pytest.mark.parametrize("serialized", [
    LEGACY_SERIALIZED | {"miss_weight": 0.25},
    LEGACY_SERIALIZED | {"miss_weight": None},
    LEGACY_SERIALIZED | {"duplicate_weight": 0.5},
    LEGACY_SERIALIZED | {"version": V2_SERIALIZED["version"]},
    V2_SERIALIZED | {"version": LEGACY_SERIALIZED["version"]},
    V2_SERIALIZED | {"formula": LEGACY_SERIALIZED["formula"]},
    V2_SERIALIZED | {"duplicate_weight": 0.25},
    V2_SERIALIZED | {"miss_weight": True},
    V2_SERIALIZED | {"miss_weight": 1},
    V2_SERIALIZED | {"miss_weight": None},
    {name: value for name, value in V2_SERIALIZED.items() if name != "miss_weight"},
])
def test_mixed_versions_and_missing_or_unreviewed_miss_weight_are_rejected(serialized):
    with pytest.raises(ValueError):
        ScoringPolicy.from_dict(serialized)


@pytest.mark.parametrize("version", ["", "v1", "v2", "latest", None, True, 2])
def test_unknown_versions_are_not_aliases_for_the_current_policy(version):
    with pytest.raises((TypeError, ValueError)):
        ScoringPolicy(version=version)


@pytest.mark.parametrize("value", [None, True, "v2", [], SCORING_POLICY.to_dict()])
def test_numeric_api_requires_an_explicit_policy_object_even_without_scored_issues(value):
    with pytest.raises(TypeError):
        score_percentage(ScoreCounts(), policy=value)
    if not isinstance(value, dict):
        with pytest.raises(TypeError):
            ScoringPolicy.from_dict(value)
