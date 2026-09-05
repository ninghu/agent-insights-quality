import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from agent_insights_quality.assessment import (
    AssessmentError,
    assess_daily,
    assess_staging,
    canonical_cards,
)
from agent_insights_quality.assessment_partition import expand_payload, intern_payload, payload_size
from agent_insights_quality.contracts import Attempt, Invocation, Step, Target
from agent_insights_quality.results import (
    ExclusionReason,
    PlannedUnit,
    UnitId,
    UnitResult,
    aggregate_results,
)
from agent_insights_quality.telemetry import ResponseScope, Snapshot

START = "2026-09-04T12:00:00+00:00"
END = "2026-09-04T12:01:00+00:00"
ENGINE = "2026-09-04T12:02:00+00:00"


def evidence(mode="model_mediated", *, missing=(), no_trace=()):
    target = Target(
        UnitId("weather-agent", "v0" if mode == "baseline" else "issue-001"),
        "prompt", mode, Path("synthetic-version"), Path("synthetic-baseline"),
        {"root_cause": "Synthetic answer contradicts supplied facts."},
    )
    attempts, invocations, records, scopes = [], {}, [], []
    for index in range(1, 11):
        steps = tuple(
            Step(phase, phase, {"input": f"synthetic {phase}"}, {"expected": "synthetic"})
            for phase in ("setup", "probe")
        )
        attempts.append(Attempt(index, steps, {"synthetic_case": index}))
        if index in missing:
            continue
        for phase in ("setup", "probe"):
            response = f"response-{index}-{phase}"
            invocations[(index, phase)] = Invocation(
                f"request-{index}-{phase}", response, f"session-{index}",
                START, END, "completed", {"id": response, "output": "actual synthetic answer"},
                200,
            )
            if index not in no_trace:
                ref = f"row-{index}-{phase}"
                records.append({"ref": ref, "raw": {
                    "synthetic_response": response, "output": "actual synthetic trace",
                    "future_attribute": {"complete": [1, 2, 3]},
                }})
                scopes.append(ResponseScope(response, ("synthetic-operation",), (ref,), (ref,)))
    return target, tuple(attempts), invocations, Snapshot(
        END, START, END, tuple(records), tuple(scopes), True,
    )


def judgment(payload, index, *, stage=False):
    attempt = payload["attempts"][index - 1]
    step = next(step for step in attempt["steps"] if step["phase"] == "probe")
    refs = step["allowed_citation_refs"]
    sufficient = step["endpoint_ref"] is not None and len(refs) > 1
    result = {
        "index": index, "sufficient": sufficient, "observed": sufficient,
        "citations": [
            {"attempt": index, "step_id": step["step_id"], "refs": refs}
        ] if sufficient else [],
        "reason": "Synthetic actual endpoint and trace comparison.",
    }
    if stage:
        result["contract_violation"] = False
    return result


def output(payload, *, stage=False):
    result = {"attempts": [judgment(payload, index, stage=stage) for index in range(1, 11)]}
    if stage:
        return result
    result["limitations"] = []
    result["cards"] = []
    for card in payload["cards"]:
        historical = card["contribution"] == "historical"
        result["cards"].append({
            "card_alias": card["card_alias"],
            "core": "unknown" if historical else "correct",
            "root_group": None if historical else "private synthetic root",
            "expected_match": not historical and payload["target"]["validation_mode"] != "baseline",
            "citations": [] if historical else judgment(payload, 1)["citations"],
            "severity": "disagrees", "proposed_fix": "unknown",
            "reason": "Synthetic explanation, not public prose.",
        })
    return result


class Sol:
    def __init__(self, *factories):
        self.factories = factories or (output,)
        self.calls = []

    async def complete_json(self, *, instructions, payload, schema):
        self.calls.append(deepcopy(payload))
        factory = self.factories[min(len(self.calls) - 1, len(self.factories) - 1)]
        return factory(expand_payload(payload))


def stage(data, sol=None, **kwargs):
    sol = sol or Sol(lambda payload: output(payload, stage=True))
    return asyncio.run(assess_staging(*data, sol, **kwargs))


def daily(data, sol=None, *, before=(), after=None, **kwargs):
    sol = sol or Sol()
    after = ({"id": "synthetic-card", "title": "Private synthetic title"},) if after is None else after
    return asyncio.run(assess_daily(
        *data, sol, before_cards=before, after_cards=after,
        engine_started_at=ENGINE, **kwargs,
    ))


def aggregate(data, assessment):
    target = data[0]
    plan = PlannedUnit(target.unit_id, None if target.is_baseline else target.unit_id.logical_version)
    return aggregate_results([plan], [assessment.unit_result])


def test_raw_payload_expectations_scope_window_and_missing_attempts_survive():
    data = evidence(missing=(10,))
    sol = Sol(lambda payload: output(payload, stage=True))
    result = stage(data, sol)
    request = sol.calls[0]
    assert len(request["attempts"]) == len(result.judgments) == 10
    assert request["attempts"][-1]["steps"][0]["execution"] is None
    probe = request["attempts"][0]["steps"][1]
    assert probe["request"] == {"input": "synthetic probe"}
    assert probe["expected"] == {"expected": "synthetic"}
    assert probe["execution"]["response"] == data[2][(1, "probe")].response
    assert probe["scope"]["operation_ids"] == ["synthetic-operation"]
    assert request["snapshot"]["records"][0]["raw"] == data[3].records[0]["raw"]
    assert request["snapshot"]["observed_at"] == END
    assert request["snapshot"]["window_start"] == START
    assert result.private_detail["input"] == request
    assert result.status == "PASS"


def test_engine_window_provenance_passes_losslessly_to_private_daily_assessment():
    from agent_insights_quality.assessment_partition import expand_payload, intern_payload
    data = evidence()
    sol = Sol()
    window = {
        "basis": "bounded_submission", "admission_earliest": ENGINE,
        "admission_latest": ENGINE, "start_latest": START, "end_earliest": ENGINE,
        "end_latest": ENGINE, "attributable_probe_attempts": 10, "coverage_proven": True,
        "reasons": [],
    }
    result = daily(data, sol, engine_window=window)
    assert sol.calls[0]["engine_window"] == window
    assert result.private_detail["input"]["engine_window"] == window
    assert expand_payload(intern_payload(result.private_detail["input"])) == result.private_detail["input"]


@pytest.mark.parametrize("mode", ["baseline", "deterministic", "model_mediated"])
def test_eight_role_observations_not_ten_perfect_responses(mode):
    result = stage(evidence(mode, missing=(9, 10)))
    assert result.status == "PASS"
    assert result.passing_attempts == 8
    assert len(result.judgments) == 10
    assert result.minimum_required == 8
    assert result.policy_version == "staging-observations-v2"


@pytest.mark.parametrize("mode", ["baseline", "deterministic"])
def test_strict_violation_disqualifies_even_after_eight_proofs(mode):
    def violate(payload):
        value = output(payload, stage=True)
        value["attempts"][-1].update(observed=False, contract_violation=True)
        return value
    result = stage(evidence(mode), Sol(violate))
    assert result.status == "FAIL"
    assert result.passing_attempts == 9
    assert result.reasons == ("proven_contract_violation",)


@pytest.mark.parametrize("observations,status", [(8, "PASS"), (7, "FAIL")])
def test_probability_nonobservations_consume_attempts_without_strict_veto(observations, status):
    def subset(payload):
        value = output(payload, stage=True)
        for item in value["attempts"][observations:]:
            item["observed"] = False
        return value
    assert stage(evidence(), Sol(subset)).status == status


def test_missing_trace_or_query_is_incomplete_not_arbitrary_behavior_failure():
    assert stage(evidence(no_trace=range(1, 11))).status == "INCOMPLETE"
    data = evidence()
    assert stage((*data[:3], replace(data[3], query_complete=False))).status == "INCOMPLETE"
    assert stage(evidence(missing=range(1, 7))).status == "INCOMPLETE"


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(unreviewed="extra"),
    lambda value: value["attempts"].pop(),
    lambda value: value["attempts"][-1].update(index=1),
    lambda value: value["attempts"][0]["citations"][0].update(refs=["unknown-ref"]),
    lambda value: value["attempts"][0]["citations"][0].update(attempt=2),
    lambda value: value["attempts"][0]["citations"][0].update(refs=["row-2-probe"]),
    lambda value: value["attempts"][0]["citations"][0].update(refs=["endpoint-01-02"]),
    lambda value: value["attempts"][0].update(sufficient=False),
])
def test_model_shape_coverage_and_attempt_proof_are_validated(mutation):
    def invalid(payload):
        value = output(payload, stage=True)
        mutation(value)
        return value
    with pytest.raises(AssessmentError):
        stage(evidence(), Sol(invalid))


def test_card_claim_unrelated_sibling_and_setup_cannot_substitute_for_probe_proof():
    def invalid(payload):
        value = output(payload)
        value["cards"][0]["citations"] = [
            {"attempt": 1, "step_id": "probe", "refs": ["row-unrelated", "endpoint-01-02"]}
        ]
        return value
    data = evidence()
    snapshot = replace(data[3], records=data[3].records + ({
        "ref": "row-unrelated", "raw": {"self_report": "I contain the expected defect"},
    },))
    with pytest.raises(AssessmentError, match="citation_invalid"):
        daily((*data[:3], snapshot), Sol(invalid))


def test_oversized_evidence_is_retained_and_explicitly_unscorable_without_sol():
    sol = Sol()
    staged = stage(evidence(), sol, max_payload_bytes=1)
    assessed = daily(evidence(), sol, max_payload_bytes=1)
    assert staged.status == "INCOMPLETE"
    assert len(staged.judgments) == 10
    assert len(staged.private_detail["input"]["attempts"]) == 10
    assert ExclusionReason.INCOMPLETE_ASSESSMENT in assessed.unit_result.exclusion_reasons
    assert sol.calls == []


def test_cumulative_cards_alias_copies_updates_and_history_without_run_id():
    old = {"id": "a", "links": ["old-response"], "title": "old"}
    updated = {**old, "links": ["old-response", "response-1-probe"], "title": "updated"}
    history = {"id": "b", "links": ["history"]}
    canonical = canonical_cards((old, history), (updated, updated, history))
    assert len(canonical) == 2
    assert canonical[0]["previous"] == old
    assert canonical[0]["current"] == updated
    assert canonical[0]["contribution"] == "current"
    assert canonical[1]["contribution"] == "historical"
    sol = Sol()
    assessed = daily(evidence(), sol, before=(old, history), after=(updated, updated, history))
    result = aggregate(evidence(), assessed)
    assert result.counts.correct_issues == 1
    assert result.counts.duplicate_cards == 0
    assert result.team_report_eligible
    assert len(sol.calls[0]["card_snapshots"]["after"]) == 3
    assert len(sol.calls) == 1


def test_same_snapshot_revisions_require_ordering_not_arbitrary_deduplication():
    with pytest.raises(AssessmentError, match="revision_ambiguous"):
        canonical_cards((), ({"id": "a", "title": "one"}, {"id": "a", "title": "two"}))
    older = {"id": "a", "title": "one", "updated_at": START}
    newer = {"id": "a", "title": "two", "updated_at": END}
    assert canonical_cards((), (newer, older))[0]["current"] == newer


def test_matched_plus_distinct_correct_extras_and_wrong_cards_survive_one_review():
    def mixed(payload):
        value = output(payload)
        value["cards"][-1].update(core="incorrect", root_group=None, expected_match=False)
        return value
    data, sol = evidence(), Sol(mixed)
    result = aggregate(data, daily(
        data, sol, after=tuple({"id": name} for name in ("a", "b", "c")),
    ))
    assert (result.counts.correct_issues, result.counts.duplicate_cards, result.counts.noise_cards) == (1, 1, 1)
    assert result.score == 44.4
    assert len(sol.calls) == 2
    assert set(sol.calls[1]["review"]["candidate_reasons"]) == {"duplicate_root", "core_incorrect"}
    assert sol.calls[0]["snapshot"] == sol.calls[1]["snapshot"]


def test_current_unknown_excludes_whole_unit_but_historical_unknown_does_not():
    def unknown(payload):
        value = output(payload)
        value["cards"][-1].update(core="unknown", root_group=None, expected_match=False, citations=[])
        return value
    data = evidence()
    assessed = daily(data, Sol(unknown), after=({"id": "a"}, {"id": "b"}))
    result = aggregate(data, assessed)
    assert ExclusionReason.UNKNOWN_CORE in assessed.unit_result.exclusion_reasons
    assert result.counts.correct_issues == result.counts.expected_issues == 0
    assert any(finding.classification.value == "expected_detection" for finding in result.units[0].findings)
    history = {"id": "b"}
    valid = aggregate(data, daily(data, before=(history,), after=({"id": "a"}, history)))
    assert valid.counts.correct_issues == 1


def test_expected_defect_must_be_independently_activated_not_just_a_matching_card():
    def inactive(payload):
        value = output(payload)
        for item in value["attempts"]:
            item["observed"] = False
        return value
    data, sol = evidence(), Sol(inactive)
    assessed = daily(data, sol)
    assert ExclusionReason.INCOMPLETE_EVIDENCE in assessed.unit_result.exclusion_reasons
    assert aggregate(data, assessed).score is None
    assert len(sol.calls) == 2


def test_unexpected_real_does_not_inflate_correct_or_noise_and_severity_is_diagnostic():
    def unexpected(payload):
        value = output(payload)
        value["cards"][0]["expected_match"] = False
        return value
    data = evidence()
    result = aggregate(data, daily(data, Sol(unexpected)))
    assert result.counts.correct_issues == result.counts.noise_cards == 0
    assert result.score == 0.0
    assert result.units[0].findings[0].classification.value == "unexpected_real"


def test_review_disagreement_is_retained_and_not_forced_to_a_favorable_verdict():
    def wrong(payload):
        value = output(payload)
        value["cards"][0].update(core="incorrect", root_group=None, expected_match=False)
        return value
    data, sol = evidence(), Sol(wrong, output)
    assessed = daily(data, sol)
    assert len(sol.calls) == 2
    assert "focused_review_disagreement" in assessed.reasons
    assert assessed.private_detail["initial"]["cards"][0]["core"] == "incorrect"
    assert assessed.private_detail["review"]["cards"][0]["core"] == "correct"
    assert assessed.unit_result.cards[0].core.value == "unknown"
    assert aggregate(data, assessed).score is None


def test_later_evidence_cannot_prove_prior_engine_visibility():
    data = evidence()
    later = replace(data[3], observed_at="2026-09-04T12:03:00+00:00")
    assessed = daily((*data[:3], later))
    assert "pre_insights_evidence_unavailable" in assessed.reasons
    # Changed raw data under an old row ID is not the raw proof saved earlier.
    changed = list(later.records)
    changed[1] = {**changed[1], "raw": {"new_late_content": True}}
    later = replace(later, records=tuple(changed))
    assessed = daily((*data[:3], later), visible_snapshot=data[3])
    assert "card_proof_not_visible_before_insights" in assessed.reasons


def test_six_ready_daily_attempts_allow_four_missing_attempts():
    data = evidence(missing=(7, 8, 9, 10))
    assert aggregate(data, daily(data)).score == 100.0
    incomplete = daily(data, cards_complete=False)
    assert ExclusionReason.INCOMPLETE_ASSESSMENT in incomplete.unit_result.exclusion_reasons


def test_reused_response_identity_and_unplanned_invocation_are_rejected():
    data = evidence()
    invocations = dict(data[2])
    invocations[(2, "probe")] = invocations[(1, "probe")]
    with pytest.raises(AssessmentError, match="response_reused"):
        stage((*data[:2], invocations, data[3]))
    invocations = {**data[2], (11, "probe"): data[2][(1, "probe")]}
    with pytest.raises(AssessmentError, match="unplanned_invocation"):
        stage((*data[:2], invocations, data[3]))


def test_daily_probe_readiness_does_not_impose_staging_setup_completeness():
    data = evidence()
    invocations = {key: value for key, value in data[2].items() if key[1] == "probe"}
    data = (*data[:2], invocations, data[3])
    assert aggregate(data, daily(data)).score == 100.0
    assert stage(data).status == "INCOMPLETE"


def test_visibility_matches_complete_raw_rows_within_scope_not_snapshot_row_numbers():
    data = evidence()
    renamed = {row["ref"]: f"earlier-{index}" for index, row in enumerate(data[3].records)}
    visible = replace(
        data[3],
        records=tuple({**row, "ref": renamed[row["ref"]]} for row in data[3].records),
        scopes=tuple(replace(
            scope, anchor_refs=tuple(renamed[ref] for ref in scope.anchor_refs),
            evidence_refs=tuple(renamed[ref] for ref in scope.evidence_refs),
        ) for scope in data[3].scopes),
    )
    assert aggregate(data, daily(data, visible_snapshot=visible)).score == 100.0


def test_invalid_model_output_and_citations_retain_private_failure_detail():
    with pytest.raises(AssessmentError) as failure:
        stage(evidence(), Sol(lambda payload: {"synthetic_invalid_output": "retained"}))
    detail = failure.value.private_detail
    assert detail["partitions"][0]["output"]["synthetic_invalid_output"] == "retained"
    assert detail["failure"]["detail"]["output"]["synthetic_invalid_output"] == "retained"
    def invalid(payload):
        value = output(payload)
        value["cards"][0]["citations"][0]["refs"].append("nonexistent")
        return value
    with pytest.raises(AssessmentError) as failure:
        daily(evidence(), Sol(invalid))
    assert "nonexistent" in failure.value.private_detail["initial"]["cards"][0]["citations"][0]["refs"]


def test_historical_review_uncertainty_cannot_change_current_duplicate_partition():
    history = {"id": "c"}
    def initial(payload):
        value = output(payload)
        value["cards"][-1].update(
            core="correct", root_group="private synthetic root", expected_match=True,
            citations=judgment(payload, 1)["citations"],
        )
        return value
    data = evidence()
    assessed = daily(
        data, Sol(initial, output), before=(history,), after=({"id": "a"}, {"id": "b"}, history),
    )
    measured = aggregate(data, assessed)
    assert measured.counts.correct_issues == measured.counts.duplicate_cards == 1
    assert "focused_review_disagreement" not in assessed.reasons


def test_baseline_noise_uses_current_evidence_and_cannot_become_a_duplicate():
    def wrong(payload):
        value = output(payload)
        for card in value["cards"]:
            card.update(core="incorrect", root_group=None, expected_match=False)
        return value
    data, sol = evidence("baseline"), Sol(wrong)
    assessed = daily(data, sol, after=({"id": "a"}, {"id": "b"}))
    assert not assessed.unit_result.exclusion_reasons
    expected = PlannedUnit(UnitId("weather-agent", "issue-001"), "issue-001")
    measured = aggregate_results(
        [PlannedUnit(data[0].unit_id), expected],
        [assessed.unit_result, UnitResult(expected.unit_id)],
    )
    assert measured.counts.noise_cards == 2
    assert measured.counts.duplicate_cards == 0
    assert len(sol.calls) == 2


def test_a_review_that_cannot_fit_does_not_drop_evidence_or_publish_initial_gap():
    data, sol = evidence(), Sol()
    first = daily(data, sol, after=())
    limit = payload_size(intern_payload(sol.calls[0]))
    second_sol = Sol()
    assessed = daily(data, second_sol, after=(), max_payload_bytes=limit)
    assert first.private_detail["review"] is not None
    assert len(second_sol.calls) == 1
    assert assessed.private_detail["review"] is None
    assert "focused_review_input_too_large" in assessed.reasons
    assert aggregate(data, assessed).score is None


def test_daily_card_coverage_and_extra_model_fields_are_rejected():
    def missing(payload):
        value = output(payload)
        value["cards"].clear()
        return value
    with pytest.raises(AssessmentError, match="card_coverage_invalid"):
        daily(evidence(), Sol(missing))
    with pytest.raises(AssessmentError, match="output_invalid"):
        daily(evidence(), Sol(lambda payload: {**output(payload), "truncated": True}))
