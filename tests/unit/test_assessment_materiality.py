"""Offline judgment-contract checks, not a live semantic model evaluation."""

from copy import deepcopy
from dataclasses import replace

import pytest

import test_assessment as fake
from agent_insights_quality.assessment import DAILY_SCHEMA


def internal_evidence():
    target, attempts, invocations, snapshot = fake.evidence()
    target = replace(target, agent_type="hosted_code")
    records = list(snapshot.records)
    records.append({
        "ref": "row-internal-review",
        "raw": {
            "gen_ai.operation.name": "chat",
            "gen_ai.input.messages": "Give a concise internal review.",
            "gen_ai.output.messages": "Review followed by unsolicited alternatives and a repeated menu.",
            "review.output_delivered": False,
        },
    })
    scopes = tuple(
        replace(scope, evidence_refs=(*scope.evidence_refs, "row-internal-review"))
        if scope.response_id == "response-1-probe" else scope
        for scope in snapshot.scopes
    )
    return target, attempts, invocations, replace(snapshot, records=tuple(records), scopes=scopes)


@pytest.mark.parametrize("severity", ["agrees", "disagrees", "unknown"])
@pytest.mark.parametrize("fix", ["agrees", "disagrees", "unknown"])
def test_supported_internal_root_is_not_noise_from_secondary_field_disagreement(severity, fix):
    data = internal_evidence()
    def judgment(payload):
        value = fake.output(payload)
        value["cards"][0].update(
            core="correct", expected_match=False, root_group="internal-padding",
            severity=severity, proposed_fix=fix,
            reason="Internal review padding is real; the endpoint itself is concise.",
        )
        return value
    sol = fake.Sol(judgment)
    result = fake.daily(data, sol, after=({
        "id": "internal-card", "title": "Internal review contains unnecessary padding",
        "category": "output_quality",
    },))
    aggregate = fake.aggregate(data, result)
    assert aggregate.counts.noise_cards == aggregate.counts.correct_issues == 0
    assert aggregate.units[0].findings[0].classification.value == "unexpected_real"
    assert result.unit_result.cards[0].severity.value == severity
    assert result.unit_result.cards[0].proposed_fix.value == fix
    assert result.private_detail["input"]["snapshot"]["records"][-1] == data[3].records[-1]
    assert not result.unit_result.exclusion_reasons


@pytest.mark.parametrize("core", ["incorrect", "unknown"])
def test_explicit_wrong_or_unresolved_delivered_claim_is_not_automatically_rescued(core):
    def judgment(payload):
        value = fake.output(payload)
        value["cards"][0].update(
            core=core, expected_match=False, root_group=None,
            reason="The claimed replacement of the delivered answer is contradicted or unresolved.",
        )
        return value
    data = internal_evidence()
    result = fake.daily(data, fake.Sol(judgment), after=({
        "id": "delivered-card", "title": "Internal commentary replaced the delivered answer",
    },))
    aggregate = fake.aggregate(data, result)
    assert aggregate.counts.correct_issues == 0
    if core == "incorrect":
        assert aggregate.counts.noise_cards == 1
    else:
        assert not aggregate.units[0].scorable
        assert aggregate.counts.noise_cards == 0
        assert "unknown_core" in {reason.value for reason in result.unit_result.exclusion_reasons}


def test_initial_and_focused_review_receive_materiality_guidance_without_schema_migration():
    original_schema = deepcopy(DAILY_SCHEMA)
    captured = []
    class CheckingSol(fake.Sol):
        async def complete_json(self, *, instructions, payload, schema):
            captured.append((instructions, deepcopy(schema)))
            assert "Make the materiality decision before grading secondary fields" in instructions
            assert "Do not add a user-delivery allegation that the card does not" in instructions
            assert "Do not rescue an explicitly false delivered-output claim" in instructions
            for branch in schema["properties"]["cards"]["items"]["anyOf"]:
                assert "affected component/output surface" in branch["properties"]["reason"]["description"]
            return await super().complete_json(instructions=instructions, payload=payload, schema=schema)
    def incorrect(payload):
        value = fake.output(payload)
        value["cards"][0].update(core="incorrect", expected_match=False, root_group=None)
        return value
    sol = CheckingSol(incorrect)
    result = fake.daily(internal_evidence(), sol)
    assert len(captured) == 2
    assert "review" not in sol.calls[0] and "review" in sol.calls[1]
    assert DAILY_SCHEMA == original_schema
    assert "description" not in DAILY_SCHEMA["properties"]["cards"]["items"]["properties"]["reason"]
    assert result.unit_result.cards[0].core.value == "incorrect"
