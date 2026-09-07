"""Public-safe frozen judgments test mechanics, not live model semantic accuracy."""

from copy import deepcopy
from dataclasses import replace
import json

import pytest

import test_assessment as fake
from agent_insights_quality.assessment import AssessmentError, DAILY_SCHEMA
from agent_insights_quality.privacy import public_projection, restore_public_result
from agent_insights_quality.publication import build_public_report
from agent_insights_quality.report_review import RetainedReviewContext
from agent_insights_quality.reporting import (
    markdown_view, render_email_html, render_json, render_markdown, render_private_markdown,
)
from agent_insights_quality.results import (
    CardVerdict, CoreVerdict, ExclusionReason, PlannedUnit, UnitId, UnitResult, aggregate_results,
)
from agent_insights_quality.runner import restore_unit
from agent_insights_quality.scoring import LEGACY_SCORING_POLICY, SCORING_POLICY
from agent_insights_quality.state import RuntimeStore


def wrapper_evidence(*, model=False, missing=(), no_trace=()):
    target, attempts, invocations, snapshot = fake.evidence(
        "baseline" if model else "deterministic", missing=missing, no_trace=no_trace,
    )
    target = replace(
        target, unit_id=UnitId("support-ticket-agent", "v0" if model else "issue-032"),
        agent_type="hosted_code", expectation={
            "healthy_behavior": "Read an existing in-scope ticket through the normal task workflow.",
            "root_cause": "The application rejects a legitimate read before required task work.",
        },
    )
    request = {"input": "Read ticket DEMO-1; it is in my permitted scope."}
    reply = "DEMO-1 is open." if model else "Request rejected before task processing."
    attempts = tuple(replace(attempt, steps=tuple(
        replace(step, body=request) if step.phase == "probe" else step for step in attempt.steps
    )) for attempt in attempts)
    invocations = {
        key: replace(value, response={"id": value.response_id, "output": reply})
        for key, value in invocations.items()
    }
    records = tuple({
        "ref": row["ref"], "raw": {
            "name": "invoke_agent", "gen_ai.operation.name": "invoke_agent",
            "gen_ai.input.messages": request, "gen_ai.output.messages": reply,
            "event.name": "genAIContent", "status": "success",
            "gen_ai.usage.input_tokens": 0, "gen_ai.usage.output_tokens": 0,
        },
    } for row in snapshot.records)
    if model:
        records += ({
            "ref": "row-independent-model", "raw": {
                "name": "chat synthetic-model", "gen_ai.operation.name": "chat",
                "gen_ai.request.model": "synthetic-model", "span.kind": "client",
                "gen_ai.input.messages": request, "gen_ai.output.messages": reply,
            },
        },)
        snapshot = replace(snapshot, scopes=tuple(
            replace(scope, evidence_refs=(*scope.evidence_refs, "row-independent-model"))
            if scope.response_id == "response-1-probe" else scope for scope in snapshot.scopes
        ))
    return target, attempts, invocations, replace(snapshot, records=records)


def frozen_output(data, *, core="correct", observed=True, reason="Saved synthetic verdict.",
                  cards=True, expected=True):
    scopes = {scope.response_id: scope for scope in data[3].scopes}
    attempts = []
    for index in range(1, 11):
        receipt = data[2].get((index, "probe"))
        scope = scopes.get(receipt.response_id) if receipt else None
        sufficient = scope is not None and scope.attributable
        attempts.append({
            "index": index, "sufficient": sufficient, "observed": sufficient and observed,
            "citations": [{
                "attempt": index, "step_id": "probe",
                "refs": [f"endpoint-{index:02d}-02", *scope.evidence_refs],
            }] if sufficient else [],
            "reason": reason,
        })
    return {
        "attempts": attempts, "limitations": [],
        "cards": [{
            "card_alias": "card-0001", "core": core,
            "expected_match": core == "correct" and expected,
            "root_group": "synthetic-root" if core == "correct" else None,
            "citations": next(item["citations"] for item in attempts if item["sufficient"]),
            "severity": "unknown", "proposed_fix": "unknown", "reason": reason,
        }] if cards else [],
    }


def frozen_sol(*outputs):
    return fake.Sol(*(lambda payload, value=value: deepcopy(value) for value in outputs))


def dispute(data, *, cards=True, reverse=False):
    initial = frozen_output(
        data, core="incorrect", observed=reverse, cards=cards,
        reason="Initial interpretation treats completed wrapper text as inference.",
    )
    review = frozen_output(
        data, observed=not reverse, cards=cards,
        reason="Review distinguishes the rejected task from outer response instrumentation.",
    )
    sol = frozen_sol(initial, review)
    result = fake.daily(data, sol, after=({"id": "synthetic-card"},) if cards else ())
    return result, sol, initial, review


def cohort(*assessments, policy=SCORING_POLICY):
    measured = UnitId("weather-agent", "issue-001")
    plan = (
        PlannedUnit(measured, "issue-001"),
        *(PlannedUnit(item.unit_id, None if item.unit_id.logical_version == "v0"
                      else item.unit_id.logical_version) for item in assessments),
    )
    result = aggregate_results(plan, (
        UnitResult(measured, (
            CardVerdict("card-0001", CoreVerdict.CORRECT, "issue-001"),
            CardVerdict("card-0002", CoreVerdict.CORRECT, "issue-001"),
            CardVerdict("card-0003", CoreVerdict.INCORRECT),
        )), *assessments,
    ), scoring_policy=policy)
    return result, plan


def retained_report(tmp_path, assessment, result, plan):
    runtime = RuntimeStore("daily", root=tmp_path)
    identity = assessment.unit_result.unit_id
    key = f"targets/{identity.agent}/{identity.logical_version}"
    records = runtime.run("synthetic-semantics")
    artifact = assessment.to_private_dict()
    with runtime.ownership():
        records.save_completed(key + "/source", {
            "assessment": {"run_id": "synthetic-semantics", "artifact": key + "/assessment"},
        })
        records.save_artifact(key + "/assessment", artifact)
    review = RetainedReviewContext(runtime, "synthetic-semantics", result)
    markdown = render_private_markdown(result, allowed_units=plan, review_context=review)
    assert records.read_artifact(key + "/assessment") == artifact
    return markdown, review


@pytest.mark.parametrize("cards", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_complete_opposing_activation_stays_excluded_without_inventing_missing_data(tmp_path, cards, reverse):
    data = wrapper_evidence()
    assessed, sol, initial, review = dispute(data, cards=cards, reverse=reverse)
    assert len(sol.calls) == 2
    assert assessed.private_detail["initial"] == initial
    assert assessed.private_detail["review"] == review
    assert all(not item["sufficient"] and not item["observed"]
               for item in assessed.private_detail["resolved"]["attempts"])
    assert assessed.private_detail["input"]["snapshot"] == data[3].to_private_dict()
    assert sol.calls[0]["snapshot"] == sol.calls[1]["snapshot"]
    expected = {ExclusionReason.INCOMPLETE_ASSESSMENT}
    if cards:
        expected.add(ExclusionReason.UNKNOWN_CORE)
        assert assessed.unit_result.cards[0].core is CoreVerdict.UNKNOWN
    assert set(assessed.unit_result.exclusion_reasons) == expected
    assert set(assessed.reasons) == {
        "focused_review_disagreement", "expected_activation_disputed",
        *({"current_core_unknown"} if cards else set()),
    }
    result, plan = cohort(assessed.unit_result)
    assert result.status.value == "Partial"
    assert result.counts.to_dict() == {
        "expected_issues": 1, "correct_issues": 1, "noise_cards": 1, "duplicate_cards": 1,
    }
    assert result.score == 40.0
    assert result.coverage.excluded_units == 1
    assert all(not item.scored for item in result.units[1].findings)
    markdown, context = retained_report(tmp_path, assessed, result, plan)
    assert context.for_result(result)[data[0].unit_id]["attempt_disagreements"] == list(range(1, 11))
    assert "Review disagreement." in markdown
    assert "Activation/sufficiency disputed on 10/10 attempts." in markdown
    assert "Assessment incomplete" in markdown and "Evidence incomplete" not in markdown
    if cards:
        assert "Initial assessment (incorrect)" in markdown
        assert "Focused review (correct)" in markdown_view(markdown)
    frozen = result.to_dict()
    assert json.loads(render_json(result, allowed_units=plan)) == frozen
    assert public_projection(result, allowed_units=plan) == frozen
    assert restore_public_result(frozen, allowed_units=plan).to_dict() == frozen
    assert build_public_report(
        result, allowed_units=plan, report_date="2026-01-05", framework_run_id="synthetic-semantics",
        source_commit="a" * 40, region="Sweden Central",
    )["report"] == frozen
    for text in (render_markdown(result, allowed_units=plan), render_email_html(result, allowed_units=plan)):
        assert "40.0" in text
        assert "Initial interpretation" not in text
    assert result.to_dict() == frozen


@pytest.mark.parametrize("gap", [
    "query", "previsible_query", "explicit_limit", "late", "changed_proof", "execution",
])
def test_semantic_disagreement_never_erases_independent_gaps(tmp_path, gap):
    data = wrapper_evidence(missing=range(6, 11) if gap == "execution" else ())
    kwargs = {}
    if gap == "query":
        data = (*data[:3], replace(data[3], query_complete=False, gaps=("query_incomplete",)))
    elif gap == "previsible_query":
        kwargs["visible_snapshot"] = replace(data[3], query_complete=False)
    elif gap == "late":
        kwargs["visible_snapshot"] = replace(data[3], observed_at="2026-09-04T12:03:00+00:00")
    elif gap == "changed_proof":
        kwargs["visible_snapshot"] = replace(data[3], records=tuple({
            **row, "raw": {"earlier_uninformative_content": True},
        } for row in data[3].records))
    initial = frozen_output(data, core="incorrect", observed=False)
    review = frozen_output(data)
    if gap == "explicit_limit":
        initial["limitations"] = ["incomplete_evidence"]
    assessed = fake.daily(data, frozen_sol(initial, review), **kwargs)
    assert ExclusionReason.INCOMPLETE_EVIDENCE in assessed.unit_result.exclusion_reasons
    assert ExclusionReason.INCOMPLETE_ASSESSMENT in assessed.unit_result.exclusion_reasons
    if gap in {"late", "changed_proof", "previsible_query"}:
        assert "expected_activation_disputed" not in assessed.reasons
        assert "expected_defect_unconfirmed_or_not_visible" in assessed.reasons
    if gap == "execution":
        assert ExclusionReason.INCOMPLETE_EXECUTION in assessed.unit_result.exclusion_reasons
    result, plan = cohort(assessed.unit_result)
    markdown, _ = retained_report(tmp_path, assessed, result, plan)
    assert "Review disagreement." in markdown and "Evidence incomplete" in markdown


def test_sufficiency_uncertainty_is_not_promoted_to_complete_semantic_proof():
    data = wrapper_evidence()
    initial, review = frozen_output(data, core="incorrect", observed=False), frozen_output(data)
    for attempt in initial["attempts"]:
        attempt.update(sufficient=False, citations=[])
    result = fake.daily(data, frozen_sol(initial, review))
    assert "expected_activation_disputed" not in result.reasons
    assert ExclusionReason.INCOMPLETE_EVIDENCE in result.unit_result.exclusion_reasons


@pytest.mark.parametrize("ready", [6, 10])
def test_supported_guard_defect_does_not_require_model_spans_or_ten_perfect_attempts(ready):
    data = wrapper_evidence(no_trace=range(ready + 1, 11))
    output = frozen_output(
        data, reason="The valid in-scope read was rejected before task processing; no platform cause is claimed.",
    )
    sol = frozen_sol(output)
    result = fake.daily(data, sol, after=({
        "id": "synthetic-card", "title": "Valid task rejected before processing",
    },))
    assert len(sol.calls) == 1
    assert not result.unit_result.exclusion_reasons
    assert fake.aggregate(data, result).counts.correct_issues == 1
    assert sum(item["sufficient"] for item in result.private_detail["resolved"]["attempts"]) == ready
    assert all(row["raw"]["gen_ai.operation.name"] == "invoke_agent" for row in data[3].records)


def test_independent_model_operation_is_not_erased_by_successful_zero_token_wrapper():
    data = wrapper_evidence(model=True)
    output = frozen_output(
        data, core="incorrect", expected=False,
        reason="The attributable chat request/response contradicts the claimed absence of inference.",
    )
    result = fake.daily(data, frozen_sol(output, output), after=({
        "id": "synthetic-card", "title": "No model work occurred",
    },))
    assert not result.unit_result.exclusion_reasons
    assert "row-independent-model" in output["cards"][0]["citations"][0]["refs"]
    aggregate, _ = cohort(result.unit_result)
    assert aggregate.units[1].findings[0].classification.value == "noise"
    assert aggregate.counts.noise_cards == 2


def test_no_model_span_or_zero_usage_does_not_force_a_rejection_card_correct():
    data = wrapper_evidence()
    output = frozen_output(
        data, core="unknown", observed=False,
        reason="Wrapper-only evidence does not resolve the claimed platform quota cause.",
    )
    result = fake.daily(data, frozen_sol(output, output), after=({
        "id": "synthetic-card", "title": "Platform quota prevented inference",
    },))
    assert result.unit_result.cards[0].core is CoreVerdict.UNKNOWN
    assert fake.aggregate(data, result).counts.noise_cards == 0


def mixed_account_evidence(obligation):
    target, attempts, invocations, snapshot = fake.evidence()
    target = replace(
        target, unit_id=UnitId("finance-agent", "issue-017"), agent_type="hosted_code",
        expectation={
            "root_cause": "One failed account is hidden in an aggregate claiming full coverage.",
            "healthy_behavior": "Disclose failed items and summarize only successful accounts.",
            "output_contract": obligation,
        },
    )
    attempts = tuple(replace(attempt, steps=tuple(
        replace(step, body={"input": "Summarize accounts demo-a and demo-b.", "contract": obligation})
        if step.phase == "probe" else step for step in attempt.steps
    )) for attempt in attempts)
    answer = {"status": "complete", "accounts": ["demo-a", "demo-b"], "spent": 25}
    invocations = {
        key: replace(value, response={"id": value.response_id, "output": answer})
        for key, value in invocations.items()
    }
    records = tuple({
        "ref": row["ref"], "raw": {
            "gen_ai.operation.name": "execute_tool",
            "tool_result": [
                {"account": "demo-a", "budget": 100, "spent": 25},
                {"account": "demo-b", "error": "account_not_found"},
            ],
            "delivered_output": answer,
        },
    } for row in snapshot.records)
    return target, attempts, invocations, replace(snapshot, records=records)


@pytest.mark.parametrize("obligation,core,reason", [
    (
        "Report available spending totals.",
        "unknown", "The omission is real, but the alleged obligation is unresolved, not proven false.",
    ),
    (
        "Remaining is explicitly optional for this totals-only request.",
        "incorrect", "The applicable optional-field contract contradicts the alleged mandatory omission.",
    ),
    (
        "Every successful account summary must include remaining = budget minus spent.",
        "correct", "Independent output and the applicable required-field contract establish an omission.",
    ),
    (
        "Tell the caller how much is left to spend after the recorded expenditure.",
        "correct", "The task requires the available balance even without prescribing a literal field name.",
    ),
])
@pytest.mark.parametrize("fix", [
    "Report demo-a's total and disclose demo-b account_not_found.",
    "Report a partial summary and disclose demo-b account_not_found.",
])
def test_noise_boundary_and_unexpected_real_remain_core_not_fix_decisions(obligation, core, reason, fix):
    data = mixed_account_evidence(obligation)
    output = frozen_output(data, core=core, expected=False, reason=reason)
    review = deepcopy(output)
    output["cards"][0]["proposed_fix"] = "agrees"
    review["cards"][0]["proposed_fix"] = "disagrees"
    sol = frozen_sol(output, review)
    result = fake.daily(data, sol, after=({
        "id": "synthetic-card", "title": "Remaining field is absent",
        "description": "The missing remaining value makes this summary deficient.", "suggested_fix": fix,
    },))
    assert len(sol.calls) == 2
    assert result.unit_result.cards[0].core.value == core
    assert result.unit_result.cards[0].proposed_fix.value == "unknown"
    assert "focused_review_disagreement" not in result.reasons
    aggregate, _ = cohort(result.unit_result)
    finding = aggregate.units[1].findings[0]
    assert finding.classification.value == {
        "unknown": "unknown", "incorrect": "noise", "correct": "unexpected_real",
    }[core]
    assert aggregate.units[1].counts.correct_issues == 0
    assert aggregate.units[1].counts.noise_cards == (core == "incorrect")
    assert aggregate.units[1].scorable is (core != "unknown")
    assert sol.calls[0]["card_snapshots"]["after"][0]["suggested_fix"] == fix


def test_suggested_patch_cannot_replace_independent_citation_proof():
    data = mixed_account_evidence("Remaining may be useful.")
    output = frozen_output(data, core="incorrect", expected=False)
    output["cards"][0]["citations"][0]["refs"] = ["endpoint-01-02"]
    with pytest.raises(AssessmentError, match="assessment_proof_missing"):
        fake.daily(data, frozen_sol(output), after=({
            "id": "synthetic-card", "suggested_fix": "This patch proves the diagnosis.",
        },))


@pytest.mark.parametrize("excluded", [1, 2, 3])
@pytest.mark.parametrize("policy", [LEGACY_SCORING_POLICY, SCORING_POLICY])
def test_old_disagreement_codes_and_counts_restore_without_rejudgment(tmp_path, excluded, policy):
    assessed, _, _, _ = dispute(wrapper_evidence())
    saved = assessed.to_private_dict()
    saved["unit_result"]["exclusion_reasons"] = [
        "incomplete_assessment", "incomplete_evidence", "unknown_core",
    ]
    saved["reasons"] = [
        "focused_review_disagreement", "expected_defect_unconfirmed_or_not_visible", "current_core_unknown",
    ]
    frozen = deepcopy(saved)
    old_unit = restore_unit(saved["unit_result"])
    result, plan = cohort(*(replace(
        old_unit, unit_id=UnitId("support-ticket-agent", f"issue-{32 + index:03d}"),
    ) for index in range(excluded)), policy=policy)
    value = result.to_dict()
    restored = restore_public_result(value, allowed_units=plan)
    assert restored.to_dict() == value
    assert restored.scoring_policy == policy
    assert restored.coverage.excluded_units == excluded
    assert restored.counts.correct_issues == restored.counts.expected_issues == 1
    assert restored.counts.noise_cards == restored.counts.duplicate_cards == 1
    assert restored.team_report_eligible is (excluded <= 2)
    assert restored.score == (None if excluded == 3 else (44.4 if policy == LEGACY_SCORING_POLICY else 40.0))
    legacy = replace(assessed, unit_result=old_unit, reasons=tuple(saved["reasons"]))
    markdown, _ = retained_report(tmp_path, legacy, restored, plan)
    assert "Review disagreement." in markdown and "Evidence incomplete" in markdown
    assert saved == frozen


def test_both_daily_passes_receive_semantic_guidance_without_schema_migration():
    original = deepcopy(DAILY_SCHEMA)
    data = wrapper_evidence()
    initial = frozen_output(data, core="incorrect", observed=False)
    reviewed = frozen_output(data)

    class CheckingSol(fake.Sol):
        async def complete_json(self, *, instructions, schema, **kwargs):
            guidance = " ".join(instructions.split())
            for phrase in (
                "Distinguish the outer Agent response wrapper",
                "gen_ai.output.messages alone do not establish LLM execution",
                "Neither missing child spans nor zero token usage alone",
                "an application workflow guard",
                "Lack of support is not automatically proof of a wrong core",
                "valid implicit business obligations",
                'not by requiring the literal word "partial"',
                "Two agreeing judgments are not independent evidence",
            ):
                assert phrase in guidance
            for variant in schema["properties"]["cards"]["items"]["anyOf"]:
                reason = variant["properties"]["reason"]["description"]
                assert "normative basis or essential uncertainty" in reason
                assert "outer Agent response wrappers" in reason
            return await super().complete_json(instructions=instructions, schema=schema, **kwargs)

    sol = CheckingSol(lambda payload: deepcopy(initial), lambda payload: deepcopy(reviewed))
    fake.daily(data, sol)
    assert len(sol.calls) == 2
    assert DAILY_SCHEMA == original
