from copy import deepcopy
from dataclasses import replace
import json

from jsonschema import Draft202012Validator
import pytest

from agent_insights_quality.privacy import (
    PUBLIC_RESULT_SCHEMA, PrivacyError, public_projection, restore_public_result,
    validate_public_projection,
)
from agent_insights_quality.results import (
    CardVerdict,
    CoreVerdict,
    PlannedUnit,
    UnitId,
    UnitResult,
    aggregate_results,
    rescore_result,
)
from agent_insights_quality.scoring import LEGACY_SCORING_POLICY, SCORING_POLICY


def sample(*, summary=None, alias="card-0001", root="issue-001", policy=SCORING_POLICY):
    unit = UnitId("weather-agent", "issue-001")
    plan = (PlannedUnit(unit, "issue-001"),)
    result = aggregate_results(plan, (UnitResult(unit, (
        CardVerdict(alias, CoreVerdict.CORRECT, root, summary=summary),
    )),), scoring_policy=policy)
    return result, plan


@pytest.mark.parametrize("summary", [
    "Harmless-looking model prose is not reviewed.",
    "https://synthetic-private.example.test/trace",
    "access_token=synthetic-only",
    "Work item contains synthetic private detail.",
    "someone@example.test",
])
def test_arbitrary_free_text_is_rejected_not_sanitized_or_labeled_safe(summary):
    result, plan = sample(summary=summary)
    with pytest.raises(PrivacyError):
        public_projection(result, allowed_units=plan)


@pytest.mark.parametrize("field", ["provider_id", "raw", "model_output", "work_item", "url"])
def test_extra_fields_are_rejected_at_every_public_object_depth(field):
    result, plan = sample()
    value = result.to_dict()
    for target in ("root", "unit", "finding"):
        copied = deepcopy(value)
        container = {
            "root": copied,
            "unit": copied["units"][0],
            "finding": copied["units"][0]["findings"][0],
        }[target]
        container[field] = "synthetic-private"
        with pytest.raises(PrivacyError):
            validate_public_projection(copied, allowed_units=plan)


@pytest.mark.parametrize("mutation", [
    lambda value: value["counts"].update(noise_cards="0"),
    lambda value: value["counts"].update(noise_cards=0.0),
    lambda value: value["coverage"].update(excluded_units=True),
    lambda value: value.update(score=float("nan")),
    lambda value: value.update(status="synthetic-private"),
    lambda value: value["units"][0]["unit_id"].update(agent="provider-name"),
    lambda value: value["units"][0].update(expected_issue_alias="provider-id"),
    lambda value: value["units"][0]["findings"][0].update(root_cause_alias="provider-id"),
    lambda value: value["scoring_policy"].update(duplicate_weight=1),
    lambda value: value.update(score=7),
    lambda value: value["counts"].update(correct_issues=0),
    lambda value: value["units"][0].update(scorable=False),
    lambda value: value["units"][0]["findings"][0].update(classification="noise"),
    lambda value: value["units"][0]["findings"][0].update(root_cause_alias=None),
])
def test_wrong_public_values_do_not_pass_an_allowlisted_shape(mutation):
    result, plan = sample()
    value = result.to_dict()
    mutation(value)
    with pytest.raises(PrivacyError):
        validate_public_projection(value, allowed_units=plan)


def test_canonical_aliases_and_reviewed_unit_identity_are_required():
    result, plan = sample(alias="synthetic-provider-id")
    with pytest.raises(PrivacyError):
        public_projection(result, allowed_units=plan)
    result, plan = sample(root="root-0001")
    projected = public_projection(result, allowed_units=plan)
    projected["units"][0]["findings"].clear()
    assert len(result.units[0].findings) == 1
    with pytest.raises(PrivacyError):
        public_projection(result, allowed_units=(replace(plan[0], unit_id=UnitId("other", "issue-001")),))


@pytest.mark.parametrize("policy,score", [(LEGACY_SCORING_POLICY, 30.8), (SCORING_POLICY, 36.4)])
def test_roundtrip_uses_artifact_policy_and_preserves_legacy_shape_and_unscored_findings(
    policy, score,
):
    plan = tuple(
        PlannedUnit(UnitId("synthetic-agent", f"issue-{number:03}"), f"issue-{number:03}")
        for number in range(1, 4)
    ) + (PlannedUnit(UnitId("synthetic-agent", "v0")),)
    actual = (
        UnitResult(plan[0].unit_id, (
            CardVerdict("card-0001", CoreVerdict.CORRECT, "issue-001"),
        )),
        UnitResult(plan[1].unit_id),
        UnitResult(plan[2].unit_id, (
            CardVerdict("card-0001", CoreVerdict.UNKNOWN),
            CardVerdict("card-0002", CoreVerdict.INCORRECT),
        )),
        UnitResult(plan[3].unit_id, (
            CardVerdict("card-0001", CoreVerdict.INCORRECT),
            CardVerdict("card-0002", CoreVerdict.CORRECT, "root-0001"),
            CardVerdict("card-0003", CoreVerdict.CORRECT, "root-0001"),
        )),
    )
    result = aggregate_results(plan, actual, scoring_policy=policy)
    original = json.dumps(result.to_dict()).encode("utf-8")
    payload = json.loads(original)
    assert payload["score"] == score
    assert Draft202012Validator(PUBLIC_RESULT_SCHEMA).is_valid(payload)
    validated = validate_public_projection(payload, allowed_units=plan)
    restored = restore_public_result(payload, allowed_units=plan)
    assert restored == result
    assert restored.scoring_policy == policy
    assert restored.score == score
    assert json.dumps(restored.to_dict()).encode("utf-8") == original
    assert json.dumps(public_projection(restored, allowed_units=plan)).encode("utf-8") == original
    assert ("miss_weight" in restored.to_dict()["scoring_policy"]) is (policy == SCORING_POLICY)
    assert restored.coverage.excluded_units == 1
    assert restored.excluded_units[0].findings[1].classification.value == "noise"
    assert not restored.excluded_units[0].findings[1].scored
    validated["units"][0]["findings"].clear()
    assert len(payload["units"][0]["findings"]) == 1
    assert json.dumps(payload).encode("utf-8") == original


@pytest.mark.parametrize("policy", [LEGACY_SCORING_POLICY, SCORING_POLICY])
@pytest.mark.parametrize("mutation", [
    lambda value: value.update(version="unreviewed"),
    lambda value: value.update(version=True),
    lambda value: value.update(formula="100"),
    lambda value: value.update(noise_weight=True),
    lambda value: value.update(noise_weight=2),
    lambda value: value.update(duplicate_weight=True),
    lambda value: value.update(duplicate_weight=0.75),
    lambda value: value.update(duplicate_weight="0.5"),
    lambda value: value.update(miss_weight=True),
    lambda value: value.update(miss_weight=0),
    lambda value: value.update(miss_weight=None),
    lambda value: value.update(rounding="bankers"),
    lambda value: value.update(raw="synthetic-private"),
    lambda value: value.pop("formula"),
    lambda value: value.pop("version"),
])
def test_public_schema_and_restoration_reject_tampered_policy_values(policy, mutation):
    result, plan = sample(policy=policy)
    payload = result.to_dict()
    mutation(payload["scoring_policy"])
    assert not Draft202012Validator(PUBLIC_RESULT_SCHEMA).is_valid(payload)
    with pytest.raises(PrivacyError):
        validate_public_projection(payload, allowed_units=plan)
    with pytest.raises(PrivacyError):
        restore_public_result(payload, allowed_units=plan)


@pytest.mark.parametrize("policy", [
    LEGACY_SCORING_POLICY.to_dict() | {"miss_weight": 0.25},
    LEGACY_SCORING_POLICY.to_dict() | {"version": SCORING_POLICY.version},
    SCORING_POLICY.to_dict() | {"version": LEGACY_SCORING_POLICY.version},
    SCORING_POLICY.to_dict() | {"formula": LEGACY_SCORING_POLICY.formula},
    {name: value for name, value in SCORING_POLICY.to_dict().items() if name != "miss_weight"},
    {"version": LEGACY_SCORING_POLICY.version},
    {"version": SCORING_POLICY.version},
])
def test_public_policy_shape_cannot_mix_versions_or_infer_missing_fields(policy):
    result, plan = sample()
    payload = result.to_dict() | {"scoring_policy": policy}
    assert not Draft202012Validator(PUBLIC_RESULT_SCHEMA).is_valid(payload)
    with pytest.raises(PrivacyError):
        restore_public_result(payload, allowed_units=plan)


@pytest.mark.parametrize("source,destination", [
    (LEGACY_SCORING_POLICY, SCORING_POLICY), (SCORING_POLICY, LEGACY_SCORING_POLICY),
])
def test_result_cannot_relabel_a_policy_without_recomputing_its_score(source, destination):
    first = PlannedUnit(UnitId("synthetic-agent", "issue-001"), "issue-001")
    second = PlannedUnit(UnitId("synthetic-agent", "issue-002"), "issue-002")
    plan = (first, second)
    result = aggregate_results(plan, (
        UnitResult(first.unit_id, (CardVerdict("card-0001", CoreVerdict.CORRECT, "issue-001"),)),
        UnitResult(second.unit_id),
    ), scoring_policy=source)
    before = result.to_dict()
    relabeled = before | {"scoring_policy": destination.to_dict()}
    assert Draft202012Validator(PUBLIC_RESULT_SCHEMA).is_valid(relabeled)
    with pytest.raises(PrivacyError):
        restore_public_result(relabeled, allowed_units=plan)
    rescored = rescore_result(result, destination)
    assert restore_public_result(rescored.to_dict(), allowed_units=plan) == rescored
    assert result.to_dict() == before


def test_absent_policy_is_not_assumed_to_be_the_current_default():
    result, plan = sample()
    payload = result.to_dict()
    del payload["scoring_policy"]
    with pytest.raises(PrivacyError):
        restore_public_result(payload, allowed_units=plan)
