"""Category slices reuse the one scorer and the original whole-unit judgments."""

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from agent_insights_quality.privacy import PrivacyError, public_projection, restore_public_result
from agent_insights_quality.results import (
    CATEGORY_POLICY, ISSUE_CATEGORIES, CardVerdict, CoreVerdict, ExclusionReason,
    PlannedUnit, UnitId, UnitResult, aggregate_results, rescore_result,
)
from agent_insights_quality.scoring import LEGACY_SCORING_POLICY, SCORING_POLICY, ScoreCounts


def sample(*, policy=SCORING_POLICY, excluded=(), **flags):
    plan = (
        PlannedUnit(UnitId("agent-a", "v0")),
        PlannedUnit(UnitId("agent-a", "issue-001"), "issue-001", "hallucinations"),
        PlannedUnit(UnitId("agent-b", "issue-002"), "issue-002", "hallucinations"),
        PlannedUnit(UnitId("agent-b", "issue-003"), "issue-003", "tool_call_failures"),
    )
    detected = CardVerdict("card-0001", CoreVerdict.CORRECT, "issue-001")
    actual = (
        UnitResult(plan[0].unit_id, (
            CardVerdict("card-0001", CoreVerdict.INCORRECT),
            CardVerdict("card-0002", CoreVerdict.CORRECT, "root-0001"),
            CardVerdict("card-0003", CoreVerdict.CORRECT, "root-0001"),
        )),
        UnitResult(plan[1].unit_id, (
            detected, detected, replace(detected, card_alias="card-0002"),
            CardVerdict("card-0003", CoreVerdict.INCORRECT),
        )),
        UnitResult(plan[2].unit_id),
        UnitResult(plan[3].unit_id),
    )
    actual = tuple(
        replace(unit, exclusion_reasons=(ExclusionReason.INCOMPLETE_EVIDENCE,))
        if index in excluded else unit for index, unit in enumerate(actual)
    )
    return aggregate_results(plan, actual, scoring_policy=policy, **flags), plan


@pytest.mark.parametrize("policy,category_score,global_score", [
    (SCORING_POLICY, 36.4, 22.2), (LEGACY_SCORING_POLICY, 30.8, 18.2),
])
def test_categories_partition_unit_counts_not_global_penalties(policy, category_score, global_score):
    result, plan = sample(policy=policy)
    breakdown = result.category_breakdown
    assert breakdown.version == CATEGORY_POLICY
    assert tuple(item.category for item in breakdown.categories) == ISSUE_CATEGORIES
    by_category = {item.category: item for item in breakdown.categories}
    hallucinations = by_category["hallucinations"]
    assert hallucinations.counts == ScoreCounts(1, 2, 1, 1)
    assert hallucinations.score == category_score
    assert hallucinations.coverage.planned_issues == hallucinations.coverage.scored_issues == 2
    assert by_category["tool_call_failures"].score == 0.0
    assert by_category["tool_call_failures"].counts == ScoreCounts(0, 1)
    assert by_category["latency"].score is None
    assert by_category["latency"].coverage.planned_issues == 0
    assert breakdown.baseline.counts == ScoreCounts(0, 0, 1, 1)
    assert breakdown.baseline.coverage.scored_baselines == 1
    assert "score" not in breakdown.baseline.to_dict()
    assert result.score == global_score
    for key, count in result.counts.to_dict().items():
        assert count == sum(
            item.counts.to_dict()[key] for item in (*breakdown.categories, breakdown.baseline)
        )
    for key, count in result.coverage.to_dict().items():
        assert count == sum(
            item.coverage.to_dict()[key] for item in (*breakdown.categories, breakdown.baseline)
        )
    assert result.units[1].planned.category == "hallucinations"
    assert restore_public_result(result.to_dict(), allowed_units=plan) == result


def test_exclusions_remove_the_entire_issue_or_baseline_and_keep_findings():
    result, plan = sample(excluded=(0, 1))
    breakdown = result.category_breakdown
    category = next(item for item in breakdown.categories if item.category == "hallucinations")
    assert category.counts == ScoreCounts(0, 1)
    assert category.score == 0.0
    assert category.coverage.planned_issues == 2
    assert category.coverage.scored_issues == category.coverage.excluded_units == 1
    assert breakdown.baseline.counts == ScoreCounts()
    assert breakdown.baseline.coverage.excluded_units == 1
    assert all(not finding.scored for finding in result.units[1].findings)
    assert result.units[1].to_dict()["category"] == "hallucinations"
    assert restore_public_result(result.to_dict(), allowed_units=plan) == result
    empty, _ = sample(excluded=(1, 2))
    category = next(item for item in empty.category_breakdown.categories if item.category == "hallucinations")
    assert category.score is None and category.counts == ScoreCounts()
    assert category.coverage.excluded_units == 2


@pytest.mark.parametrize("flags", [
    {"systemic_failure": True}, {"integrity_failure": True}, {"excluded": (0, 2, 3)},
])
def test_failed_run_never_exposes_a_category_quality_score(flags):
    result, plan = sample(**flags)
    assert result.score is None
    assert all(item.score is None for item in result.category_breakdown.categories)
    assert restore_public_result(result.to_dict(), allowed_units=plan) == result


def test_explicit_rescore_updates_category_scores_without_new_judgments_or_mutation():
    old, plan = sample(policy=LEGACY_SCORING_POLICY)
    original = json.dumps(old.to_dict())
    new = rescore_result(old)
    assert new.units == old.units and new.counts == old.counts and new.coverage == old.coverage
    assert next(item.score for item in new.category_breakdown.categories if item.category == "hallucinations") == 36.4
    assert restore_public_result(new.to_dict(), allowed_units=plan) == new
    assert json.dumps(old.to_dict()) == original


def test_legacy_result_and_plan_shape_remain_unchanged_without_inferred_categories():
    unit = PlannedUnit(UnitId("agent-a", "issue-001"), "issue-001")
    result = aggregate_results((unit,), (UnitResult(unit.unit_id),))
    assert result.category_breakdown is None
    assert "category_breakdown" not in result.to_dict()
    assert "category" not in result.to_dict()["units"][0]
    assert unit.to_dict() == {
        "unit_id": {"agent": "agent-a", "logical_version": "issue-001"},
        "expected_issue_alias": "issue-001",
    }
    assert PlannedUnit.from_dict(unit.to_dict()) == unit
    assert restore_public_result(result.to_dict(), allowed_units=(unit,)) == result
    assert rescore_result(result, LEGACY_SCORING_POLICY).category_breakdown is None
    with pytest.raises(PrivacyError):
        restore_public_result(result.to_dict(), allowed_units=(replace(unit, category="hallucinations"),))


@pytest.mark.parametrize("category", ["unknown", "", "https://synthetic.invalid", "Hallucinations", True, 0])
def test_only_reviewed_category_values_are_accepted(category):
    with pytest.raises((TypeError, ValueError)):
        PlannedUnit(UnitId("agent-a", "issue-001"), "issue-001", category)


def test_baselines_and_partial_category_metadata_cannot_be_scored_as_categories():
    with pytest.raises(ValueError):
        PlannedUnit(UnitId("agent-a", "v0"), category="hallucinations")
    _, plan = sample()
    mixed = tuple(replace(unit, category=None) if index == 2 else unit for index, unit in enumerate(plan))
    with pytest.raises(ValueError, match="every issue"):
        aggregate_results(mixed, ())
    with pytest.raises(ValueError):
        PlannedUnit.from_dict(plan[0].to_dict() | {"category": None})
    with pytest.raises(ValueError):
        PlannedUnit.from_dict(plan[1].to_dict() | {"url": "https://synthetic.invalid"})


@pytest.mark.parametrize("mutation", [
    lambda value: value["units"][1].update(category="tool_call_failures"),
    lambda value: value["units"][1].pop("category"),
    lambda value: value["units"][0].update(category="hallucinations"),
    lambda value: value.pop("category_breakdown"),
    lambda value: value["category_breakdown"].update(version="unreviewed"),
    lambda value: value["category_breakdown"].update(raw="synthetic-private"),
    lambda value: value["category_breakdown"]["categories"].pop(),
    lambda value: value["category_breakdown"]["categories"].reverse(),
    lambda value: value["category_breakdown"]["categories"][2].update(category="latency"),
    lambda value: value["category_breakdown"]["categories"][2].update(score=100.0),
    lambda value: value["category_breakdown"]["categories"][2].update(score=float("nan")),
    lambda value: value["category_breakdown"]["categories"][2].update(score=float("inf")),
    lambda value: value["category_breakdown"]["categories"][2].update(score=True),
    lambda value: value["category_breakdown"]["categories"][2]["counts"].update(noise_cards=0.0),
    lambda value: value["category_breakdown"]["categories"][2]["coverage"].update(excluded_units=False),
    lambda value: value["category_breakdown"]["categories"][2].update(raw="synthetic-private"),
    lambda value: value["category_breakdown"]["baseline"]["counts"].update(noise_cards=0),
    lambda value: value["category_breakdown"]["baseline"].update(score=100),
])
def test_category_public_projection_rejects_invalid_fields_values_and_relabeling(mutation):
    result, plan = sample()
    value = deepcopy(result.to_dict())
    mutation(value)
    with pytest.raises(PrivacyError):
        restore_public_result(value, allowed_units=plan)


def test_public_projection_is_detached_and_cannot_relabel_categories_through_an_allowed_plan():
    result, plan = sample()
    payload = public_projection(result, allowed_units=plan)
    payload["category_breakdown"]["categories"][2]["counts"]["noise_cards"] = 999
    assert result.category_breakdown.categories[2].counts.noise_cards == 1
    changed = tuple(
        replace(unit, category="output_quality") if unit.category == "hallucinations" else unit
        for unit in plan
    )
    with pytest.raises(PrivacyError):
        public_projection(result, allowed_units=changed)
