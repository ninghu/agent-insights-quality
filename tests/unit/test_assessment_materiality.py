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


@pytest.mark.parametrize("core", ["correct", "incorrect"])
def test_recovered_dependency_observation_and_false_unhandled_claim_keep_distinct_verdicts(core):
    class CheckingSol(fake.Sol):
        async def complete_json(self, *, instructions, **kwargs):
            assert "Deliberate fault exercise or" in instructions
            assert "do not insert the latter claim when the card exonerates" in instructions
            return await super().complete_json(instructions=instructions, **kwargs)
    def judgment(payload):
        value = fake.output(payload)
        value["cards"][0].update(
            core=core, expected_match=False,
            root_group="operational-dependency" if core == "correct" else None,
            proposed_fix="disagrees",
            reason="The dependency failed and the Agent recovered; only an unhandled-failure claim is false.",
        )
        return value
    data = fake.evidence("baseline")
    result = fake.daily(data, CheckingSol(judgment))
    counts = fake.aggregate(data, result).counts
    assert counts.correct_issues == 0
    assert counts.noise_cards == (core == "incorrect")
    assert result.unit_result.cards[0].proposed_fix.value == "disagrees"


@pytest.mark.parametrize("core", ["incorrect", "unknown"])
def test_harmful_override_noncompliance_does_not_force_a_valid_unexpected_finding(core):
    data = fake.evidence()
    target = replace(data[0], expectation={
        "healthy_behavior": "Return the requested owner and follow-up action.",
        "root_cause": "Injected instruction suppresses required handoff fields.",
    })

    class CheckingSol(fake.Sol):
        async def complete_json(self, *, instructions, **kwargs):
            guidance = " ".join(instructions.split())
            assert "The injected defect describes intended test activation" in guidance
            assert "do not rescue an explicit wrong-business-output claim" in guidance
            assert "missing delay caused the failure" in guidance
            return await super().complete_json(instructions=instructions, **kwargs)

    def judgment(payload):
        value = fake.output(payload)
        value["cards"][0].update(
            core=core, expected_match=False, root_group=None,
            proposed_fix="disagrees",
            reason="The answer satisfies the healthy task; suppression is the injected defect.",
        )
        return value

    sol = CheckingSol(judgment)
    result = fake.daily(
        (target, *data[1:]), sol,
        after=({"id": "synthetic-card", "title": "Requested handoff fields should have been suppressed"},),
    )
    assert len(sol.calls) == 2
    assert result.unit_result.cards[0].core.value == core
    assert result.unit_result.cards[0].root_cause_alias is None
    assert result.unit_result.cards[0].proposed_fix.value == "disagrees"


@pytest.mark.parametrize("scenario,core", [
    ("valid_privacy_refusal", "incorrect"),
    ("applicable_schema_violation", "correct"),
    ("explicit_business_exception", "incorrect"),
    ("unknown_contract_authority", "unknown"),
])
def test_contract_boundary_preserves_evidence_backed_verdicts_without_hidden_label_rules(scenario, core):
    cases = {
        "valid_privacy_refusal": (
            "Do not disclose other accounts.", "Return another account's appointment.",
            "That account is outside the active scope.", "Refusal failed to provide requested data.",
        ),
        "applicable_schema_violation": (
            "Return the requested record as JSON with an owner string.",
            "Return the owner as JSON.", '{"owner":42}', "Owner has the wrong required type.",
        ),
        "explicit_business_exception": (
            "Normally include record details. For out-of-scope accounts, return only access_denied.",
            "Show the other account's full record.", '{"error":"access_denied"}',
            "The refusal omitted ordinary record fields.",
        ),
        "unknown_contract_authority": (
            "A retrieved note claims a two-field exception is approved.",
            "Return a complete handoff.", '{"owner":"desk","next_action":"follow up"}',
            "The output did not obey the claimed exception.",
        ),
    }
    rule, request, response, claim = cases[scenario]
    data = fake.evidence()
    attempts = tuple(replace(attempt, steps=tuple(
        replace(step, body={"input": request}) if step.phase == "probe" else step
        for step in attempt.steps
    )) for attempt in data[1])
    invocations = {
        key: replace(receipt, response={"id": receipt.response_id, "output": response})
        for key, receipt in data[2].items()
    }
    snapshot = replace(data[3], records=tuple({
        **row, "raw": {**row["raw"], "configured_rule": rule, "output": response},
    } for row in data[3].records))

    class CheckingSol(fake.Sol):
        async def complete_json(self, *, instructions, payload, **kwargs):
            guidance = " ".join(instructions.split())
            assert "Legitimate developer-defined authorization, privacy and business constraints" in guidance
            assert "An explicit valid exception is not automatically a conflict" in guidance
            assert "the service knows our private injection labels" in guidance
            assert payload["snapshot"]["records"] == list(snapshot.records)
            return await super().complete_json(instructions=instructions, payload=payload, **kwargs)

    def respond(payload):
        value = fake.output(payload)
        value["cards"][0].update(
            core=core, expected_match=False,
            root_group="independently-supported-contract" if core == "correct" else None,
            reason=f"Scoped contract assessment: {scenario}.",
        )
        return value

    result = fake.daily(
        (data[0], attempts, invocations, snapshot), CheckingSol(respond),
        after=({"id": "synthetic-contract-card", "description": claim},),
    )
    assert result.unit_result.cards[0].core.value == core
    aggregate = fake.aggregate(data, result)
    assert aggregate.counts.correct_issues == 0
    assert aggregate.counts.noise_cards == (core == "incorrect")
    assert aggregate.units[0].scorable is (core != "unknown")
