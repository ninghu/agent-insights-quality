"""Offline Daily card-phase proof and bounded baseline limitation contracts."""

from copy import deepcopy
from dataclasses import replace

import pytest

import test_assessment as fake
from agent_insights_quality.assessment import AssessmentError
from agent_insights_quality.assessment_partition import expand_payload, intern_payload, payload_size
from agent_insights_quality.results import ExclusionReason


def setup_citation(payload, index=1):
    step = payload["attempts"][index - 1]["steps"][0]
    return {
        "attempt": index, "step_id": step["step_id"],
        "refs": step["allowed_citation_refs"],
    }


def setup_card(payload, *, core="correct"):
    value = fake.output(payload)
    value["cards"][0].update(
        core=core, expected_match=False,
        root_group="unexpected setup problem" if core == "correct" else None,
        citations=[setup_citation(payload)],
    )
    return value


@pytest.mark.parametrize("core", ["correct", "incorrect"])
def test_known_card_accepts_same_setup_endpoint_trace_without_expected_credit(core):
    data = fake.evidence(no_trace=(9, 10))
    history = ({"id": "history-a"}, {"id": "history-b"})
    sol = fake.Sol(lambda payload: setup_card(payload, core=core))
    result = fake.daily(
        data, sol, before=history, after=({"id": "current"}, *history),
    )
    assert not result.unit_result.exclusion_reasons
    assert result.unit_result.cards[0].core.value == core
    assert all(
        item.core.value == "unknown" for item in result.unit_result.cards[1:]
    )
    resolved = result.private_detail["resolved"]
    assert sum(item["observed"] for item in resolved["attempts"]) == 8
    assert resolved["cards"][0]["expected_match"] is False
    measured = fake.aggregate(data, result)
    assert measured.counts.correct_issues == measured.counts.duplicate_cards == 0
    assert measured.counts.noise_cards == (core == "incorrect")


@pytest.mark.parametrize("staging", [False, True])
def test_setup_pair_cannot_establish_attempt_sufficiency_or_activation(staging):
    def invalid(payload):
        value = fake.output(payload, stage=staging)
        value["attempts"][0]["citations"] = [setup_citation(payload)]
        return value

    assess = fake.stage if staging else fake.daily
    with pytest.raises(AssessmentError, match="assessment_proof_missing"):
        assess(fake.evidence(), fake.Sol(invalid))


def test_setup_only_evidence_cannot_establish_daily_readiness_or_activation():
    data = fake.evidence()
    snapshot = replace(
        data[3],
        scopes=tuple(scope for scope in data[3].scopes if scope.response_id.endswith("-setup")),
    )
    result = fake.daily((*data[:3], snapshot), fake.Sol(setup_card))
    assert result.unit_result.cards[0].core.value == "correct"
    assert "insufficient_attributable_evidence" in result.reasons
    assert "expected_defect_unconfirmed_or_not_visible" in result.reasons
    assert result.private_detail["input"]["measurement_facts"]["attributable_probe_attempts"] == 0
    assert not any(item["observed"] for item in result.private_detail["resolved"]["attempts"])
    assert fake.aggregate(data, result).counts.correct_issues == 0


@pytest.mark.parametrize("mutation,error", [
    ("endpoint_only", "assessment_proof_missing"),
    ("trace_only", "assessment_proof_missing"),
    ("split_pair", "assessment_proof_missing"),
    ("other_attempt", "assessment_citation_invalid"),
    ("other_phase", "assessment_citation_invalid"),
    ("unrelated_trace", "assessment_citation_invalid"),
])
def test_card_phase_permission_does_not_weaken_same_turn_proof(mutation, error):
    def invalid(payload):
        value = setup_card(payload)
        citations = value["cards"][0]["citations"]
        citation = citations[0]
        endpoint, trace = citation["refs"]
        if mutation == "endpoint_only":
            citation["refs"] = [endpoint]
        elif mutation == "trace_only":
            citation["refs"] = [trace]
        elif mutation == "split_pair":
            citation["refs"] = [endpoint]
            citations.append({**citation, "refs": [trace]})
        elif mutation == "other_attempt":
            citation["attempt"] = 2
        elif mutation == "other_phase":
            citation["step_id"] = "probe"
        else:
            citation["refs"] = [endpoint, "row-2-setup"]
        return value

    with pytest.raises(AssessmentError, match=error):
        fake.daily(fake.evidence(), fake.Sol(invalid))


@pytest.mark.parametrize("missing_anchor", [True, False])
def test_setup_card_still_requires_attributable_anchor_without_conflicts(missing_anchor):
    data = fake.evidence()
    scopes = list(data[3].scopes)
    scopes[0] = replace(
        scopes[0], anchor_refs=() if missing_anchor else scopes[0].anchor_refs,
        reasons=() if missing_anchor else ("span_identity_conflict",),
    )
    with pytest.raises(AssessmentError, match="assessment_proof_missing"):
        fake.daily((*data[:3], replace(data[3], scopes=tuple(scopes))), fake.Sol(setup_card))


def test_setup_card_proof_must_already_be_visible_before_insights():
    data = fake.evidence()
    records = list(data[3].records)
    records[0] = {**records[0], "raw": {"later_setup_content": True}}
    later = replace(data[3], records=tuple(records))
    result = fake.daily(
        (*data[:3], later), fake.Sol(setup_card), visible_snapshot=data[3],
    )
    assert "card_proof_not_visible_before_insights" in result.reasons
    assert ExclusionReason.INCOMPLETE_EVIDENCE in result.unit_result.exclusion_reasons


@pytest.mark.parametrize("ready,encoded", [(6, False), (9, False), (9, True), (10, False)])
def test_ready_no_current_card_baseline_has_empty_limitation_schema(ready, encoded):
    data = fake.evidence("baseline", no_trace=range(ready + 1, 11))
    history = ({"id": "history"},)
    sol = fake.Sol()
    result = fake.daily(data, sol, before=history, after=history)
    original = result.private_detail["input"]
    if encoded:
        sol = fake.Sol()
        result = fake.daily(
            data, sol, before=history, after=history,
            max_payload_bytes=payload_size(intern_payload(original)),
        )
        assert sol.calls[0]["lossless_encoding"] == "json-path-references-v1"
    assert expand_payload(sol.calls[0]) == original
    facts = original["measurement_facts"]
    assert facts["executed_probe_attempts"] == 10
    assert facts["attributable_probe_attempts"] == ready
    assert facts["pre_insights_attributable_probe_attempts"] == ready
    assert facts["current_card_count"] == 0
    assert facts["unit_limitations_not_applicable"] is True
    assert sol.schemas[0]["properties"]["limitations"]["maxItems"] == 0
    assert len(result.private_detail["resolved"]["attempts"]) == 10
    assert result.private_detail["resolved"]["attempts"][-1]["sufficient"] is (ready == 10)
    assert result.private_detail["resolved"]["limitations"] == []
    assert not result.unit_result.exclusion_reasons
    assert len(sol.calls) == 1


@pytest.mark.parametrize("limitation", ["incomplete_evidence", "incomplete_execution"])
def test_unsupported_baseline_limitation_is_rejected_not_removed_or_retried(limitation):
    returned = []

    def invalid(payload):
        value = fake.output(payload)
        value["limitations"] = [limitation]
        returned.append(deepcopy(value))
        return value

    sol = fake.Sol(invalid)
    with pytest.raises(AssessmentError, match="assessment_output_invalid") as failure:
        fake.daily(fake.evidence("baseline", no_trace=(10,)), sol, after=())
    detail = failure.value.private_detail
    assert detail["failure"]["detail"]["output"] == returned[0]
    assert detail["initial"] is detail["review"] is detail["resolved"] is None
    assert sol.schemas[0]["properties"]["limitations"]["maxItems"] == 0
    assert len(sol.calls) == 1


@pytest.mark.parametrize("gap", [
    "execution", "readiness", "previsible_readiness", "query", "previsible_query",
    "late", "cards_incomplete", "current_unknown", "current_known", "issue_activation",
    "engine_window",
])
def test_empty_limitation_rule_never_overrides_essential_gaps(gap):
    data = fake.evidence(
        "model_mediated" if gap == "issue_activation" else "baseline",
        missing=range(6, 11) if gap == "execution" else (),
        no_trace=range(6, 11) if gap == "readiness" else (),
    )
    kwargs = {}
    after = ({"id": "current"},) if gap in {"current_unknown", "current_known"} else ()
    if gap == "query":
        data = (*data[:3], replace(data[3], query_complete=False))
    elif gap == "previsible_query":
        kwargs["visible_snapshot"] = replace(data[3], query_complete=False)
    elif gap == "previsible_readiness":
        kwargs["visible_snapshot"] = replace(
            data[3], scopes=tuple(
                scope for scope in data[3].scopes
                if int(scope.response_id.split("-")[1]) <= 5
            ),
        )
    elif gap == "late":
        kwargs["visible_snapshot"] = replace(
            data[3], observed_at="2026-09-04T12:03:00+00:00",
        )
    elif gap == "cards_incomplete":
        kwargs["cards_complete"] = False
    elif gap == "engine_window":
        kwargs["engine_window"] = {
            "coverage_proven": False, "reasons": ["insights_window_coverage_unproven"],
        }

    def incomplete(payload):
        value = fake.output(payload)
        value["limitations"] = ["incomplete_evidence"]
        if gap == "current_unknown":
            value["cards"][0].update(core="unknown", root_group=None, expected_match=False)
        if gap == "issue_activation":
            for item in value["attempts"]:
                item["observed"] = False
        return value

    sol = fake.Sol(incomplete)
    result = fake.daily(data, sol, after=after, **kwargs)
    assert result.private_detail["input"]["measurement_facts"]["unit_limitations_not_applicable"] is False
    assert all("maxItems" not in schema["properties"]["limitations"] for schema in sol.schemas)
    assert ExclusionReason.INCOMPLETE_EVIDENCE in result.unit_result.exclusion_reasons
    assert result.private_detail["resolved"]["limitations"] == ["incomplete_evidence"]
    if gap == "current_unknown":
        assert ExclusionReason.UNKNOWN_CORE in result.unit_result.exclusion_reasons
    if gap == "issue_activation":
        assert "expected_defect_unconfirmed_or_not_visible" in result.reasons


def test_invalid_evidence_window_still_fails_before_model_call():
    data, sol = fake.evidence("baseline"), fake.Sol()
    with pytest.raises(AssessmentError, match="assessment_snapshot_invalid"):
        fake.daily((*data[:3], replace(data[3], window_end=fake.START)), sol, after=())
    assert sol.calls == []
