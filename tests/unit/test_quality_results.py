"""Small synthetic score/result contracts, without catalogs or external boundaries."""

from dataclasses import FrozenInstanceError, asdict, replace
import json

import pytest

from agent_insights_quality.results import (
    CardVerdict,
    Contribution,
    CoreVerdict,
    DeliveryStatus,
    DiagnosticVerdict,
    ExclusionReason,
    FailureReason,
    FindingClassification,
    PlannedUnit,
    UnitId,
    UnitResult,
    aggregate_results,
    rescore_result,
)
from agent_insights_quality.scoring import (
    LEGACY_SCORING_POLICY, SCORING_POLICY, ScoreCounts, score_percentage,
)


def issue(number=1, agent="agent-demo"):
    return PlannedUnit(
        UnitId(agent, f"issue-{number}"), f"issue-{number}"
    )


def baseline(agent="agent-demo"):
    return PlannedUnit(UnitId(agent, "v0"))


def correct(unit, alias="card-correct", **kwargs):
    return CardVerdict(
        alias, CoreVerdict.CORRECT, unit.expected_issue_alias, **kwargs
    )


def incorrect(alias="card-noise", **kwargs):
    return CardVerdict(alias, CoreVerdict.INCORRECT, **kwargs)


@pytest.mark.parametrize(
    "correct_count,noise_count,duplicate_count,legacy_score,v2_score",
    [
        (20, 0, 0, 100.0, 100.0),
        (0, 0, 0, 0.0, 0.0),
        (19, 0, 0, 95.0, 98.7),
        (19, 1, 0, 90.5, 93.8),
        (20, 0, 4, 95.2, 90.9),
        (20, 2, 2, 88.9, 87.0),
    ],
)
@pytest.mark.parametrize("policy", [LEGACY_SCORING_POLICY, SCORING_POLICY])
def test_approved_daily_score_examples(
    correct_count, noise_count, duplicate_count, legacy_score, v2_score, policy,
):
    plan = tuple(issue(number) for number in range(20))
    actual = []
    for number, unit in enumerate(plan):
        cards = (correct(unit),) if number < correct_count else ()
        # The wrong card belongs to the missed issue, not a separate noise bucket.
        if number == 19:
            cards += tuple(incorrect(f"noise-{n}") for n in range(noise_count))
        if number == 0:
            cards += tuple(correct(unit, f"duplicate-{n}") for n in range(duplicate_count))
        actual.append(UnitResult(unit.unit_id, cards))
    result = aggregate_results(plan, actual, scoring_policy=policy)
    assert result.counts == ScoreCounts(correct_count, 20, noise_count, duplicate_count)
    assert result.score == (legacy_score if policy == LEGACY_SCORING_POLICY else v2_score)
    assert result.scoring_policy == policy
    assert type(result.score) is float
    assert result.status is DeliveryStatus.FULL
    assert result.team_report_eligible
    assert result.coverage.planned_issues == result.coverage.scored_issues == 20


def test_real_daily_plan_has_five_baselines_and_twenty_issues_without_catalogs():
    plan = tuple(baseline(f"agent-{n}") for n in range(5)) + tuple(
        issue(n, f"agent-{n // 4}") for n in range(20)
    )
    result = aggregate_results(
        plan,
        (UnitResult(unit.unit_id, (correct(unit),) if unit.is_issue else ()) for unit in plan),
    )
    assert result.counts == ScoreCounts(20, 20)
    assert result.coverage.to_dict() == {
        "planned_issues": 20, "scored_issues": 20,
        "planned_baselines": 5, "scored_baselines": 5, "excluded_units": 0,
    }
    assert result.score == 100.0


@pytest.mark.parametrize("policy,score", [(LEGACY_SCORING_POLICY, 44.4), (SCORING_POLICY, 40.0)])
def test_baseline_noise_and_correct_duplicates_penalize_without_healthy_bonus(policy, score):
    planned_issue, planned_baseline = issue(), baseline()
    real = CardVerdict(
        "baseline-real", CoreVerdict.CORRECT, "unexpected-root"
    )
    result = aggregate_results(
        (planned_issue, planned_baseline),
        (
            UnitResult(planned_issue.unit_id, (correct(planned_issue),)),
            UnitResult(
                planned_baseline.unit_id,
                (real, replace(real, card_alias="baseline-copy"), incorrect()),
            ),
        ),
        scoring_policy=policy,
    )
    assert result.counts == ScoreCounts(1, 1, 1, 1)
    assert result.score == score
    assert result.units[1].counts == ScoreCounts(0, 0, 1, 1)
    assert result.coverage.scored_baselines == 1


def test_correct_unexpected_does_not_detect_expected_issue_or_count_as_noise():
    unit = issue()
    card = CardVerdict(
        "real-finding", CoreVerdict.CORRECT, "unexpected-root"
    )
    result = aggregate_results((unit,), (UnitResult(unit.unit_id, (card,)),))
    assert result.counts == ScoreCounts(0, 1)
    assert result.score == 0.0
    assert result.units[0].findings[0].classification is FindingClassification.UNEXPECTED_REAL


def test_matched_plus_noise_and_duplicates_keeps_detection_without_double_counting():
    unit = issue()
    cards = (
        correct(unit),
        correct(unit, "correct-extra"),
        incorrect("wrong-one", root_cause_alias=unit.expected_issue_alias),
        incorrect("wrong-two", root_cause_alias=unit.expected_issue_alias),
    )
    result = aggregate_results((unit,), (UnitResult(unit.unit_id, cards),))
    assert result.counts == ScoreCounts(1, 1, 2, 1)
    classifications = {finding.card.card_alias: finding for finding in result.units[0].findings}
    for alias in ("wrong-one", "wrong-two"):
        assert classifications[alias].classification is FindingClassification.NOISE
    assert sum(
        finding.classification is FindingClassification.DUPLICATE
        for finding in classifications.values()
    ) == 1


def test_same_id_page_and_update_copies_collapse_and_order_is_irrelevant():
    unit = issue()
    card = correct(unit)
    cards = (card, replace(card), incorrect(), incorrect(), correct(unit, "extra"))
    first = aggregate_results((unit,), (UnitResult(unit.unit_id, cards),))
    second = aggregate_results((unit,), (UnitResult(unit.unit_id, tuple(reversed(cards))),))
    assert first == second
    assert first.counts == ScoreCounts(1, 1, 1, 1)
    assert len(first.units[0].findings) == 3


def test_conflicting_same_id_revisions_need_reconciliation_not_duplicate_penalty():
    unit = issue()
    card = correct(unit)
    with pytest.raises(ValueError, match="reconciliation"):
        aggregate_results(
            (unit,), (UnitResult(unit.unit_id, (card, replace(card, core=CoreVerdict.UNKNOWN))),)
        )


def test_current_contributions_do_not_inherit_historical_counts_or_uncertainty():
    unit = issue()
    cards = (
        correct(unit, "old-detection", contribution=Contribution.HISTORICAL),
        incorrect("old-noise", contribution=Contribution.HISTORICAL),
        CardVerdict(
            "old-unknown", CoreVerdict.UNKNOWN,
            contribution=Contribution.HISTORICAL,
        ),
        correct(unit, "new-detection"),
    )
    result = aggregate_results((unit,), (UnitResult(unit.unit_id, cards),))
    assert result.counts == ScoreCounts(1, 1)
    assert result.status is DeliveryStatus.FULL
    historical = [
        finding for finding in result.units[0].findings
        if finding.card.contribution is Contribution.HISTORICAL
    ]
    assert len(historical) == 3
    assert all(not finding.scored for finding in historical)
    assert all(finding.classification is FindingClassification.HISTORICAL for finding in historical)
    no_current = aggregate_results((unit,), (UnitResult(unit.unit_id, cards[:3]),))
    assert no_current.counts == ScoreCounts(0, 1)
    assert no_current.score == 0.0


@pytest.mark.parametrize("severity", tuple(DiagnosticVerdict))
@pytest.mark.parametrize("proposed_fix", tuple(DiagnosticVerdict))
@pytest.mark.parametrize("policy,score", [(LEGACY_SCORING_POLICY, 80.0), (SCORING_POLICY, 66.7)])
def test_diagnostic_fields_neither_block_nor_change_score(severity, proposed_fix, policy, score):
    unit = issue()
    cards = (
        correct(unit, severity=severity, proposed_fix=proposed_fix),
        correct(unit, "another", severity=severity, proposed_fix=proposed_fix),
    )
    result = aggregate_results((unit,), (UnitResult(unit.unit_id, cards),), scoring_policy=policy)
    assert result.counts == ScoreCounts(1, 1, 0, 1)
    assert result.score == score
    assert result.status is DeliveryStatus.FULL
    assert all(finding.card.severity is severity for finding in result.units[0].findings)


@pytest.mark.parametrize("excluded_baseline", [False, True])
def test_unknown_core_excludes_the_whole_unit_and_retains_known_unscored_findings(
    excluded_baseline,
):
    healthy = issue(1)
    excluded = baseline() if excluded_baseline else issue(2)
    root = excluded.expected_issue_alias or "unexpected-root"
    card = CardVerdict("known-correct", CoreVerdict.CORRECT, root)
    cards = (
        card, replace(card, card_alias="known-duplicate"),
        incorrect(), CardVerdict("uncertain", CoreVerdict.UNKNOWN),
    )
    result = aggregate_results(
        (healthy, excluded),
        (UnitResult(healthy.unit_id, (correct(healthy),)), UnitResult(excluded.unit_id, cards)),
    )
    assert result.counts == ScoreCounts(1, 1)
    assert result.score == 100.0
    assert result.status is DeliveryStatus.PARTIAL
    assert result.team_report_eligible
    assert result.coverage.excluded_units == 1
    unscored, = result.excluded_units
    assert unscored.counts == ScoreCounts()
    assert unscored.exclusion_reasons == (ExclusionReason.UNKNOWN_CORE,)
    assert len(unscored.findings) == 4
    assert all(not finding.scored for finding in unscored.findings)
    assert {finding.classification for finding in unscored.findings} >= {
        FindingClassification.NOISE, FindingClassification.DUPLICATE, FindingClassification.UNKNOWN,
    }


@pytest.mark.parametrize("reason", tuple(ExclusionReason))
def test_explicit_gaps_exclude_even_favorable_findings(reason):
    plan = (issue(1), issue(2))
    result = aggregate_results(
        plan,
        (
            UnitResult(plan[0].unit_id, (correct(plan[0]),), (reason,)),
            UnitResult(plan[1].unit_id),
        ),
    )
    assert result.counts == ScoreCounts(0, 1)
    assert result.score == 0.0
    assert result.status is DeliveryStatus.PARTIAL
    assert result.units[0].findings[0].classification is FindingClassification.EXPECTED_DETECTION
    assert not result.units[0].findings[0].scored


@pytest.mark.parametrize(
    "missing_count,status,score",
    [(0, DeliveryStatus.FULL, 100.0), (1, DeliveryStatus.PARTIAL, 100.0),
     (2, DeliveryStatus.PARTIAL, 100.0), (3, DeliveryStatus.FAILED, None)],
)
def test_missing_units_are_explicit_and_limit_counts_baselines_and_issues(
    missing_count, status, score
):
    plan = (issue(1), issue(2), baseline(), issue(3))
    actual = tuple(
        UnitResult(unit.unit_id, (correct(unit),) if unit.is_issue else ())
        for unit in plan[:len(plan) - missing_count]
    )
    result = aggregate_results(plan, actual)
    assert len(result.units) == len(plan)
    assert result.status is status
    assert result.score == score
    assert result.team_report_eligible is (status is not DeliveryStatus.FAILED)
    assert len(result.excluded_units) == result.coverage.excluded_units == missing_count
    for excluded in result.excluded_units:
        assert excluded.exclusion_reasons == (ExclusionReason.MISSING_RESULT,)
        assert excluded.counts == ScoreCounts()
    assert (FailureReason.TOO_MANY_EXCLUSIONS in result.failure_reasons) is (missing_count > 2)


@pytest.mark.parametrize("actual_kind", ["absent", "unknown", "baseline-only"])
def test_unmeasured_run_never_fabricates_zero(actual_kind):
    unit = issue()
    plan = (unit, baseline())
    actual = []
    if actual_kind == "unknown":
        actual.append(UnitResult(
            unit.unit_id, (CardVerdict("unknown", CoreVerdict.UNKNOWN),)
        ))
    elif actual_kind == "baseline-only":
        actual.append(UnitResult(plan[1].unit_id, (incorrect(),)))
    result = aggregate_results(plan, actual)
    assert result.score is None
    assert result.status is DeliveryStatus.FAILED
    assert not result.team_report_eligible
    assert FailureReason.NO_SCORABLE_ISSUES in result.failure_reasons


def test_fully_measured_baseline_only_plan_is_not_an_eligible_quality_measurement():
    unit = baseline()
    result = aggregate_results((unit,), (UnitResult(unit.unit_id),))
    assert result.coverage.excluded_units == 0
    assert result.score is None
    assert result.status is DeliveryStatus.FAILED


@pytest.mark.parametrize(
    "flag,reason",
    [("systemic_failure", FailureReason.SYSTEMIC_FAILURE),
     ("integrity_failure", FailureReason.INTEGRITY_FAILURE)],
)
def test_systemic_or_integrity_failure_prevents_team_delivery_despite_known_counts(flag, reason):
    unit = issue()
    result = aggregate_results(
        (unit,), (UnitResult(unit.unit_id, (correct(unit),)),), **{flag: True}
    )
    assert result.counts == ScoreCounts(1, 1)
    assert result.score is None
    assert result.status is DeliveryStatus.FAILED
    assert not result.team_report_eligible
    assert result.failure_reasons == (reason,)


@pytest.mark.parametrize("case", ["planned-unit", "expected-issue", "baseline", "actual-unit"])
def test_duplicate_unit_and_issue_identities_are_rejected(case):
    unit = issue()
    if case == "planned-unit":
        plan, actual = (unit, unit), ()
    elif case == "expected-issue":
        plan, actual = (unit, replace(unit, unit_id=UnitId("agent-demo", "issue-2"))), ()
    elif case == "baseline":
        first = baseline()
        plan, actual = (first, first), ()
    else:
        plan = (unit,)
        actual = (UnitResult(unit.unit_id), UnitResult(unit.unit_id))
    with pytest.raises(ValueError, match="unique|only one"):
        aggregate_results(plan, actual)


def test_card_identity_is_scoped_to_unit_so_current_updates_across_units_are_not_lost():
    plan = (issue(1), issue(2))
    actual = tuple(UnitResult(unit.unit_id, (correct(unit),)) for unit in plan)
    result = aggregate_results(plan, actual)
    assert result.counts == ScoreCounts(2, 2)


def test_unplanned_actual_and_empty_plan_are_invalid():
    with pytest.raises(ValueError, match="not planned"):
        aggregate_results((issue(1),), (UnitResult(issue(2).unit_id),))
    with pytest.raises(ValueError, match="At least one"):
        aggregate_results((), ())


@pytest.mark.parametrize(
    "unit_id,expected_issue",
    [(UnitId("agent-demo", "issue-1"), None), (UnitId("agent-demo", "v0"), "issue-1")],
)
def test_catalog_version_identity_cannot_silently_turn_an_issue_into_a_baseline(
    unit_id, expected_issue
):
    with pytest.raises(ValueError, match="v0 baseline"):
        PlannedUnit(unit_id, expected_issue)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: UnitId(True, "v0"),
        lambda: UnitId("agent-demo", 1),
        lambda: PlannedUnit("unit-1"),
        lambda: PlannedUnit(UnitId("agent-demo", "issue-1"), expected_issue_alias=False),
        lambda: CardVerdict("card", "correct"),
        lambda: CardVerdict("card", True),
        lambda: CardVerdict("card", CoreVerdict.UNKNOWN, contribution="current"),
        lambda: CardVerdict("card", CoreVerdict.UNKNOWN, severity=False),
        lambda: CardVerdict("card", CoreVerdict.UNKNOWN, proposed_fix=0),
        lambda: CardVerdict("card", CoreVerdict.UNKNOWN, summary={"raw": "model"}),
        lambda: UnitResult(UnitId("agent-demo", "v0"), cards=[]),
        lambda: UnitResult(UnitId("agent-demo", "v0"), cards=({},)),
        lambda: UnitResult(UnitId("agent-demo", "v0"), exclusion_reasons=("unknown_core",)),
        lambda: UnitResult(UnitId("agent-demo", "v0"), summary={"raw": "model"}),
        lambda: aggregate_results(({},), ()),
        lambda: aggregate_results((issue(),), ({},)),
        lambda: aggregate_results((issue(),), (), systemic_failure=1),
        lambda: aggregate_results((issue(),), (), integrity_failure=0),
    ],
)
def test_malformed_inputs_are_not_coerced(factory):
    with pytest.raises(TypeError):
        factory()


def test_correct_card_requires_a_root_and_duplicate_exclusion_reasons_are_invalid():
    with pytest.raises(ValueError, match="root alias"):
        CardVerdict("correct", CoreVerdict.CORRECT)
    with pytest.raises(ValueError, match="distinct"):
        UnitResult(
            UnitId("agent-demo", "v0"), exclusion_reasons=(ExclusionReason.UNKNOWN_CORE,) * 2
        )


@pytest.mark.parametrize("alias", ["", "A", " leading", "url://example", "a" * 65, "a\nb"])
def test_alias_shape_rejects_invalid_values_but_is_not_a_privacy_proof(alias):
    with pytest.raises(ValueError):
        CardVerdict(alias, CoreVerdict.UNKNOWN)


@pytest.mark.parametrize("summary", ["", " leading", "trailing ", "a\nb", "a" * 281])
def test_approved_summary_still_requires_concise_single_line_shape(summary):
    with pytest.raises(ValueError):
        CardVerdict("card", CoreVerdict.UNKNOWN, summary=summary)


@pytest.mark.parametrize("policy", [LEGACY_SCORING_POLICY, SCORING_POLICY])
def test_json_projection_matches_dataclass_counts_coverage_and_unscored_findings(policy):
    plan = (issue(1), issue(2), baseline())
    actual = (
        UnitResult(
            plan[0].unit_id,
            (correct(plan[0], summary="Synthetic expected defect detected."),),
        ),
        UnitResult(
            plan[1].unit_id, (incorrect(),),
            (ExclusionReason.INCOMPLETE_EVIDENCE,),
            "Synthetic evidence coverage is incomplete.",
        ),
        UnitResult(plan[2].unit_id),
    )
    result = aggregate_results(plan, actual, scoring_policy=policy)
    payload = json.loads(json.dumps(result.to_dict(), allow_nan=False))
    assert payload["counts"] == asdict(result.counts)
    assert payload["coverage"] == {
        **asdict(result.coverage), "excluded_units": result.coverage.excluded_units,
    }
    assert payload["scoring_policy"] == policy.to_dict()
    assert payload["coverage_policy"] == asdict(result.coverage_policy)
    assert payload["scoring_policy"]["noise_weight"] == 1
    assert payload["scoring_policy"]["duplicate_weight"] == policy.duplicate_weight
    assert ("miss_weight" in payload["scoring_policy"]) is (policy == SCORING_POLICY)
    assert payload["coverage_policy"]["version"] == "whole-unit-max-two-exclusions-v1"
    assert payload["score"] == result.score == score_percentage(result.counts, policy)
    assert payload["status"] == result.status.value == "Partial"
    assert payload["team_report_eligible"] == result.team_report_eligible
    for model, serialized in zip(result.units, payload["units"], strict=True):
        assert serialized["unit_id"] == asdict(model.planned.unit_id)
        assert serialized["counts"] == asdict(model.counts)
        assert serialized["scorable"] == model.scorable
    assert payload["units"][1]["findings"][0]["classification"] == "noise"
    assert payload["units"][1]["findings"][0]["scored"] is False
    assert payload["units"][0]["findings"][0]["summary"] == actual[0].cards[0].summary
    assert set(payload["units"][0]["findings"][0]) == {
        "card_alias", "core", "root_cause_alias", "contribution", "severity",
        "proposed_fix", "summary", "classification", "scored",
    }
    payload["counts"]["correct_issues"] = 999
    assert result.counts.correct_issues == 1


def test_n7_counts_rescore_to_v2_without_mutating_or_reclassifying_the_legacy_result():
    plan = tuple(issue(n, f"agent-{n // 4}") for n in range(20)) + tuple(
        baseline(f"agent-{n}") for n in range(5)
    )
    actual = [
        UnitResult(unit.unit_id, (correct(unit),) if number < 14 else ())
        for number, unit in enumerate(plan)
    ]
    actual[19] = UnitResult(
        plan[19].unit_id,
        (
            correct(plan[19]),
            incorrect(),
            CardVerdict("uncertain", CoreVerdict.UNKNOWN),
            incorrect("historical-noise", contribution=Contribution.HISTORICAL),
        ),
        (ExclusionReason.INCOMPLETE_EVIDENCE,),
        "Synthetic evidence coverage is incomplete.",
    )
    legacy = aggregate_results(plan, actual, scoring_policy=LEGACY_SCORING_POLICY)
    before = json.dumps(legacy.to_dict())
    assert legacy.counts == ScoreCounts(14, 19)
    assert legacy.score == 73.7
    assert legacy.status is DeliveryStatus.PARTIAL
    assert legacy.coverage.excluded_units == 1
    updated = rescore_result(legacy)
    assert updated is not legacy
    assert updated.score == 91.8
    assert updated.scoring_policy == SCORING_POLICY
    assert updated == aggregate_results(plan, actual)
    assert updated.units is legacy.units
    assert updated.counts is legacy.counts
    assert updated.coverage is legacy.coverage
    assert updated.coverage_policy is legacy.coverage_policy
    assert updated.failure_reasons is legacy.failure_reasons
    assert updated.team_report_eligible is legacy.team_report_eligible
    assert updated.status is legacy.status
    assert updated.excluded_units == legacy.excluded_units
    assert json.dumps(legacy.to_dict()) == before
    for name, value in legacy.to_dict().items():
        if name not in ("score", "scoring_policy"):
            assert updated.to_dict()[name] == value
    assert rescore_result(updated, LEGACY_SCORING_POLICY) == legacy
    assert rescore_result(updated) is not updated
    with pytest.raises(FrozenInstanceError):
        updated.score = 100.0


def test_rescoring_mixed_penalties_keeps_two_whole_exclusions_and_historical_context():
    plan = (issue(1), issue(2), issue(3), issue(4), baseline(), baseline("other-agent"))
    real = CardVerdict("baseline-real", CoreVerdict.CORRECT, "unexpected-root")
    actual = (
        UnitResult(plan[0].unit_id, (
            correct(plan[0]), correct(plan[0], "duplicate"), incorrect(),
            incorrect("old-noise", contribution=Contribution.HISTORICAL),
        )),
        UnitResult(plan[1].unit_id, (correct(plan[1]),)),
        UnitResult(plan[2].unit_id),
        UnitResult(plan[3].unit_id, (correct(plan[3]), incorrect()),
                   (ExclusionReason.INCOMPLETE_EVIDENCE,)),
        UnitResult(plan[4].unit_id, (real, replace(real, card_alias="baseline-copy"), incorrect())),
    )
    legacy = aggregate_results(plan, actual, scoring_policy=LEGACY_SCORING_POLICY)
    updated = rescore_result(legacy)
    assert legacy.counts == updated.counts == ScoreCounts(2, 3, 2, 2)
    assert legacy.score == 36.4
    assert updated.score == 38.1
    assert legacy.units[4].counts == ScoreCounts(0, 0, 1, 1)
    assert updated.coverage.excluded_units == 2
    assert updated.status is DeliveryStatus.PARTIAL
    assert updated.team_report_eligible
    assert updated.units is legacy.units
    assert updated == aggregate_results(plan, actual)


@pytest.mark.parametrize("failure", [
    "systemic_failure", "integrity_failure", "too_many_exclusions", "no_scorable_issues",
])
@pytest.mark.parametrize("policy", [LEGACY_SCORING_POLICY, SCORING_POLICY])
def test_rescoring_failed_results_preserves_flags_counts_exclusions_and_no_score(failure, policy):
    plan = tuple(issue(n) for n in range(4))
    actual = tuple(UnitResult(unit.unit_id, (correct(unit),)) for unit in plan)
    flags = {}
    if failure in ("systemic_failure", "integrity_failure"):
        flags[failure] = True
    else:
        actual = actual[:1] if failure == "too_many_exclusions" else ()
    result = aggregate_results(plan, actual, scoring_policy=LEGACY_SCORING_POLICY, **flags)
    before = result.to_dict()
    updated = rescore_result(result, policy)
    assert updated is not result
    assert updated.score is None
    assert updated.status is DeliveryStatus.FAILED
    assert not updated.team_report_eligible
    assert updated.failure_reasons == result.failure_reasons
    assert FailureReason(failure) in updated.failure_reasons
    assert updated.units is result.units
    assert updated.counts is result.counts
    assert updated.coverage is result.coverage
    assert updated.to_dict() == before | {"scoring_policy": policy.to_dict()}
    assert result.to_dict() == before


def test_rescoring_never_grants_eligibility_or_treats_a_measured_miss_as_unmeasured():
    unit = issue()
    result = aggregate_results((unit,), (UnitResult(unit.unit_id),))
    assert rescore_result(result).score == 0.0
    ineligible = replace(result, team_report_eligible=False, score=None)
    assert rescore_result(ineligible).score is None
    assert not rescore_result(ineligible).team_report_eligible


@pytest.mark.parametrize("policy", [None, "v2", SCORING_POLICY.to_dict(), True])
def test_aggregation_and_rescoring_reject_nonpolicy_objects(policy):
    unit = issue()
    actual = (UnitResult(unit.unit_id),)
    with pytest.raises(TypeError):
        aggregate_results((unit,), actual, scoring_policy=policy)
    result = aggregate_results((unit,), actual)
    with pytest.raises(TypeError):
        rescore_result(result, policy)
    with pytest.raises(TypeError):
        replace(result, scoring_policy=policy)
    with pytest.raises(TypeError):
        rescore_result(result.to_dict())
