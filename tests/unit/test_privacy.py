from copy import deepcopy
from dataclasses import replace

import pytest

from agent_insights_quality.privacy import PrivacyError, public_projection, validate_public_projection
from agent_insights_quality.results import (
    CardVerdict,
    CoreVerdict,
    PlannedUnit,
    UnitId,
    UnitResult,
    aggregate_results,
)


def sample(*, summary=None, alias="card-0001", root="issue-001"):
    unit = UnitId("weather-agent", "issue-001")
    plan = (PlannedUnit(unit, "issue-001"),)
    result = aggregate_results(plan, (UnitResult(unit, (
        CardVerdict(alias, CoreVerdict.CORRECT, root, summary=summary),
    )),))
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
