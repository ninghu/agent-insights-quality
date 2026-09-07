"""Synthetic completed responses, never provider/semantic-accuracy acceptance."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from agent_insights_quality.assessment import AssessmentError
from agent_insights_quality.assessment_calls import DailyAssessmentCalls
from agent_insights_quality.errors import QualityError
from agent_insights_quality.results import ExclusionReason
from agent_insights_quality.state import CheckpointError, RecordStore, RuntimeStore, StateError

import test_assessment as fake
import test_runner as runner_fake

CARDS = ({"id": "a"}, {"id": "b"})


def conflict(payload):
    value = fake.output(payload)
    value["cards"][1]["expected_match"] = False
    return value


def corrected(payload):
    value = conflict(payload)
    value["cards"][1]["root_group"] = "independently different synthetic root"
    return value


def test_valid_initial_and_normal_review_are_unchanged():
    for factory, calls in ((fake.output, 2), (corrected, 1)):
        sol = fake.Sol(factory)
        assessed = fake.daily(fake.evidence(), sol, after=CARDS)
        assert len(sol.calls) == calls
        assert not assessed.unit_result.exclusion_reasons
        assert "correction" not in assessed.private_detail
        assert "initial_validation_error" not in assessed.private_detail


def test_only_valid_correction_is_judged_from_identical_evidence_without_label_propagation():
    data, sol = fake.evidence(), fake.Sol(conflict, corrected)
    assessed = fake.daily(data, sol, after=CARDS)
    detail = assessed.private_detail
    assert len(sol.calls) == 2
    assert sol.calls[1].keys() == sol.calls[0].keys() | {"correction"}
    assert {k: v for k, v in sol.calls[1].items() if k != "correction"} == sol.calls[0]
    assert detail["initial_validation_error"] == {"code": "assessment_root_conflict"}
    assert detail["initial"] == sol.calls[1]["correction"]["initial"]
    assert detail["initial"] != detail["correction"] == detail["resolved"]
    assert detail["review"] is None
    assert [card["expected_match"] for card in detail["resolved"]["cards"]] == [True, False]
    result = fake.aggregate(data, assessed)
    assert result.counts.correct_issues == 1 and result.counts.duplicate_cards == 0
    assert not assessed.unit_result.exclusion_reasons


def test_invalid_again_stops_retaining_both_outputs_and_both_errors():
    sol = fake.Sol(conflict)
    with pytest.raises(AssessmentError, match="assessment_root_conflict") as caught:
        fake.daily(fake.evidence(), sol, after=CARDS)
    detail = caught.value.private_detail
    assert len(sol.calls) == 2
    assert detail["initial_validation_error"]["code"] == "assessment_root_conflict"
    assert detail["failure"]["code"] == "assessment_root_conflict"
    assert detail["initial"] == detail["correction"]
    assert detail["resolved"] is detail["review"] is None


def test_preexisting_current_unknown_survives_consistency_correction_without_third_call():
    def response(factory):
        def build(payload):
            value = factory(payload)
            value["cards"][2].update(core="unknown", root_group=None, expected_match=False, citations=[])
            return value
        return build
    data, sol = fake.evidence(), fake.Sol(response(conflict), response(corrected))
    assessed = fake.daily(data, sol, after=(*CARDS, {"id": "c"}))
    assert len(sol.calls) == 2
    assert ExclusionReason.UNKNOWN_CORE in assessed.unit_result.exclusion_reasons
    assert "focused_review_budget_exhausted" in assessed.reasons
    assert fake.aggregate(data, assessed).counts.correct_issues == 0
    for phase in ("initial", "correction", "resolved"):
        assert assessed.private_detail[phase]["cards"][2]["core"] == "unknown"


@pytest.mark.parametrize("unreviewed_core", ["unknown", "incorrect"])
def test_root_correction_cannot_promote_an_unrelated_unreviewed_core(unreviewed_core):
    def initial(payload):
        value = conflict(payload)
        value["cards"][2].update(core=unreviewed_core, root_group=None, expected_match=False)
        return value
    def promotion(payload):
        value = corrected(payload)
        value["cards"][2].update(core="correct", root_group="unrelated", expected_match=False)
        return value
    data, sol = fake.evidence(), fake.Sol(initial, promotion)
    assessed = fake.daily(data, sol, after=(*CARDS, {"id": "c"}))
    detail = assessed.private_detail
    assert len(sol.calls) == 2
    assert detail["initial"]["cards"][2]["core"] == unreviewed_core
    assert detail["correction"]["cards"][2]["core"] == "correct"
    assert detail["resolved"]["cards"][2]["core"] == "unknown"
    assert detail["correction_scope"]["unreviewed_core_aliases"] == ["card-0003"]
    assert ExclusionReason.UNKNOWN_CORE in assessed.unit_result.exclusion_reasons
    assert "focused_review_budget_exhausted" in assessed.reasons
    assert "focused_review_disagreement" not in assessed.reasons
    assert fake.aggregate(data, assessed).score is None
    assert fake.aggregate(data, assessed).counts.correct_issues == 0


def test_root_correction_cannot_move_expected_detection_to_an_unrelated_root():
    def initial(payload):
        value = conflict(payload)
        value["cards"][2].update(root_group="unrelated", expected_match=False)
        return value
    def reassignment(payload):
        value = corrected(payload)
        value["cards"][0]["expected_match"] = False
        value["cards"][2].update(root_group="unrelated", expected_match=True)
        return value
    data, sol = fake.evidence(), fake.Sol(initial, reassignment)
    assessed = fake.daily(data, sol, after=(*CARDS, {"id": "c"}))
    assert len(sol.calls) == 2
    assert assessed.private_detail["correction"]["cards"][2]["expected_match"] is True
    assert assessed.private_detail["resolved"]["cards"][2]["core"] == "unknown"
    assert "independent_expected_match_changed" in assessed.private_detail["unreviewed_candidates"]
    assert fake.aggregate(data, assessed).counts.correct_issues == 0


@pytest.mark.parametrize("obligation,candidate", [
    ("duplicate", "duplicate_root"),
    ("activation", "expected_activation_unconfirmed"),
    ("gap", "evidence_linkage_or_completeness"),
])
def test_correction_does_not_discharge_other_independent_review_obligations(obligation, candidate):
    def build(payload, *, invalid):
        value = conflict(payload) if invalid else corrected(payload)
        for index in (2, 3):
            value["cards"][index].update(
                expected_match=False,
                root_group="independent" if invalid else f"independent-{index}",
            )
        if obligation == "activation" and invalid:
            for attempt in value["attempts"]:
                attempt["observed"] = False
        if obligation == "gap" and invalid:
            value["limitations"] = ["incomplete_evidence"]
        if obligation != "duplicate":
            value["cards"][3]["root_group"] += "-different"
        return value
    sol = fake.Sol(lambda p: build(p, invalid=True), lambda p: build(p, invalid=False))
    data = fake.evidence()
    assessed = fake.daily(data, sol, after=(*CARDS, {"id": "c"}, {"id": "d"}))
    assert len(sol.calls) == 2
    assert candidate in assessed.private_detail["correction_scope"]["candidate_reasons"]
    assert candidate in assessed.private_detail["unreviewed_candidates"]
    assert ExclusionReason.INCOMPLETE_ASSESSMENT in assessed.unit_result.exclusion_reasons
    assert fake.aggregate(data, assessed).counts.correct_issues == 0


def test_historical_unknown_does_not_create_an_unrelated_current_review_obligation():
    history = {"id": "z"}
    assessed = fake.daily(
        fake.evidence(), fake.Sol(conflict, corrected), before=(history,), after=(*CARDS, history),
    )
    assert assessed.private_detail["correction_scope"]["unreviewed_core_aliases"] == []
    assert not assessed.unit_result.exclusion_reasons


@pytest.mark.parametrize("remaining", ["unknown", "duplicate", "noise", "activation", "gap"])
def test_correction_spends_review_slot_and_never_fabricates_resolution(remaining):
    def second(payload):
        value = corrected(payload)
        if remaining == "unknown":
            value["cards"][0].update(core="unknown", expected_match=False, root_group=None)
        elif remaining == "duplicate":
            value = fake.output(payload)
        elif remaining == "noise":
            value["cards"][1].update(core="incorrect", expected_match=False, root_group=None)
        elif remaining == "activation":
            for item in value["attempts"]:
                item["observed"] = False
        else:
            value["limitations"] = ["incomplete_evidence"]
        return value
    data, sol = fake.evidence(), fake.Sol(conflict, second)
    assessed = fake.daily(data, sol, after=CARDS)
    assert len(sol.calls) == 2
    assert "focused_review_budget_exhausted" in assessed.reasons
    assert ExclusionReason.INCOMPLETE_ASSESSMENT in assessed.unit_result.exclusion_reasons
    assert fake.aggregate(data, assessed).counts.correct_issues == 0
    assert "focused_review_disagreement" not in assessed.reasons
    assert assessed.private_detail["review"] is None
    if remaining == "unknown":
        assert ExclusionReason.UNKNOWN_CORE in assessed.unit_result.exclusion_reasons
    elif remaining in {"gap", "activation"}:
        assert ExclusionReason.INCOMPLETE_EVIDENCE in assessed.unit_result.exclusion_reasons


@pytest.mark.parametrize("mutation,code", [
    (lambda v: v["cards"][1]["citations"][0]["refs"].append("sibling-row"), "citation_invalid"),
    (lambda v: v["cards"][1].update(citations=[]), "proof_missing"),
    (lambda v: v["cards"][1].update(core="incorrect"), "judgment_invalid"),
    (lambda v: v["cards"][-1].update(card_alias="not-ours"), "card_coverage_invalid"),
    (lambda v: v["attempts"][0].update(sufficient=False), "judgment_invalid"),
    (lambda v: v.update(unreviewed=True), "output_invalid"),
])
def test_root_conflict_does_not_hide_other_invalidity_or_citation_tampering(mutation, code):
    def invalid(payload):
        value = conflict(payload)
        mutation(value)
        return value
    sol = fake.Sol(invalid)
    with pytest.raises(AssessmentError, match=code):
        fake.daily(fake.evidence(), sol, after=CARDS)
    assert len(sol.calls) == 1


def test_unowned_evidence_fails_before_any_model_call():
    data = fake.evidence()
    receipts = dict(data[2])
    receipts[2, "probe"] = replace(receipts[2, "probe"], response_id=receipts[1, "probe"].response_id)
    sol = fake.Sol(conflict, corrected)
    with pytest.raises(AssessmentError, match="response_reused"):
        fake.daily((*data[:2], receipts, data[3]), sol, after=CARDS)
    assert not sol.calls


def test_correction_context_admission_never_uses_part_of_invalid_positive_output():
    data = fake.evidence()
    sol = fake.Sol(conflict, corrected)
    fake.daily(data, sol, after=CARDS)
    from agent_insights_quality.assessment import _daily_transport
    from agent_insights_quality.assessment_partition import intern_payload, payload_size
    limit = payload_size(intern_payload(sol.calls[0]))
    assert payload_size(_daily_transport(sol.calls[1], limit)) > limit
    sol = fake.Sol(conflict, corrected)
    assessed = fake.daily(data, sol, after=CARDS, max_payload_bytes=limit)
    assert len(sol.calls) == 1
    assert assessed.reasons == ("root_correction_input_too_large",)
    assert all(card.core.value == "unknown" for card in assessed.unit_result.cards)
    assert assessed.private_detail["resolved"] is None


def save(records, kind, key, value):
    getattr(records, "save_" + kind)(key, value)


@pytest.fixture
def journal(tmp_path, monkeypatch):
    runner_fake.fake_storage(monkeypatch)
    store = RuntimeStore("daily", root=tmp_path)
    with store.ownership():
        records = store.run("synthetic")
        def make(sol, *, writer=save, **binding):
            return DailyAssessmentCalls(
                sol, records, "assessment/calls",
                {"source_revision": "source-one", "configured_assessor": {"deployment": "synthetic"},
                 **binding}, writer,
            )
        yield records, make


def test_journal_freezes_intent_invalid_output_feedback_and_correction_separately(journal):
    records, make = journal
    sol = fake.Sol(conflict, corrected)
    first = fake.daily(fake.evidence(), make(sol), after=CARDS)
    second = fake.daily(fake.evidence(), make(sol), after=CARDS)
    assert first.to_private_dict() == second.to_private_dict()
    assert len(sol.calls) == 2
    initial = records.read_artifact("assessment/calls/initial/output")["output"]
    request = records.read_artifact("assessment/calls/second/request")
    assert request["mode"] == "correction"
    assert request["payload"]["correction"] == {
        "initial": initial, "validation_error": {"code": "assessment_root_conflict"},
        "scope": first.private_detail["correction_scope"],
    }
    assert "untrusted data" in request["instructions"]
    assert "not an additional independent vote" in request["instructions"]
    assert request["prompt_hash"]
    assert records.read_artifact("assessment/calls/second/output")["output"] != initial
    assert records.read_completed("assessment/calls/second")["status"] == "completed"


def test_lossless_compacted_correction_keeps_frozen_raw_input_and_resumes(journal, monkeypatch):
    from agent_insights_quality import assessment
    from agent_insights_quality.assessment_partition import expand_payload
    monkeypatch.setattr(assessment, "_DAILY_COMPACTION_BYTES", 1)
    _, make = journal
    sol = fake.Sol(conflict, corrected)
    first = fake.daily(fake.evidence(), make(sol), after=CARDS)
    second = fake.daily(fake.evidence(), make(sol), after=CARDS)
    assert first.to_private_dict() == second.to_private_dict()
    assert len(sol.calls) == 2
    assert all(call["lossless_encoding"] == "json-path-references-v1" for call in sol.calls)
    initial, correction = map(expand_payload, sol.calls)
    assert {key: value for key, value in correction.items() if key != "correction"} == initial


def test_invalid_completed_correction_remains_terminal_on_resume(journal):
    _, make = journal
    sol = fake.Sol(conflict)
    for _ in range(2):
        with pytest.raises(AssessmentError, match="assessment_root_conflict") as caught:
            fake.daily(fake.evidence(), make(sol), after=CARDS)
        assert caught.value.private_detail["initial"] == caught.value.private_detail["correction"]
    assert len(sol.calls) == 2


@pytest.mark.parametrize("slot", ["initial", "second"])
def test_response_persisted_before_phase_crash_resumes_without_repeating_call(journal, slot):
    records, make = journal
    sol = fake.Sol(conflict, corrected)
    def crash(records, kind, key, value):
        if kind == "completed" and key == f"assessment/calls/{slot}":
            raise CheckpointError()
        save(records, kind, key, value)
    with pytest.raises(CheckpointError):
        fake.daily(fake.evidence(), make(sol, writer=crash), after=CARDS)
    assert records.read_artifact(f"assessment/calls/{slot}/output")
    assert records.read_completed(f"assessment/calls/{slot}", missing_ok=True) is None
    result = fake.daily(fake.evidence(), make(sol), after=CARDS)
    assert not result.unit_result.exclusion_reasons
    assert len(sol.calls) == 2


@pytest.mark.parametrize("slot", ["initial", "second"])
@pytest.mark.parametrize("failure", ["cancelled", "unknown", "rejected", "provider-root-code"])
def test_submitted_outcomes_are_not_blindly_repeated_or_generic_repair(journal, slot, failure):
    _, make = journal
    def fail(payload):
        if failure == "cancelled":
            raise asyncio.CancelledError()
        raise QualityError(
            "assessment_root_conflict" if failure == "provider-root-code" else "synthetic_provider",
            request_accepted=False if failure == "rejected" else None,
        )
    sol = fake.Sol(fail) if slot == "initial" else fake.Sol(conflict, fail)
    with pytest.raises((asyncio.CancelledError, QualityError)):
        fake.daily(fake.evidence(), make(sol), after=CARDS)
    count = len(sol.calls)
    with pytest.raises(QualityError):
        fake.daily(fake.evidence(), make(sol), after=CARDS)
    assert len(sol.calls) == count == (1 if slot == "initial" else 2)


@pytest.mark.parametrize("point", ["request", "submitting", "output"])
def test_checkpoint_failure_never_permits_unsafe_followup(journal, point):
    _, make = journal
    sol = fake.Sol(conflict, corrected)
    def crash(records, kind, key, value):
        if (
            key == f"assessment/calls/initial/{point}"
            or point == "submitting" and kind == "progress"
        ):
            raise CheckpointError()
        save(records, kind, key, value)
    with pytest.raises(CheckpointError):
        fake.daily(fake.evidence(), make(sol, writer=crash), after=CARDS)
    assert len(sol.calls) == (1 if point == "output" else 0)
    if point == "output":
        with pytest.raises(QualityError, match="outcome_unresolved"):
            fake.daily(fake.evidence(), make(sol), after=CARDS)
        assert len(sol.calls) == 1
    else:
        fake.daily(fake.evidence(), make(sol), after=CARDS)
        assert len(sol.calls) == 2


@pytest.mark.parametrize("change", ["source", "assessor", "input", "prompt", "schema", "mode"])
def test_changed_frozen_call_identity_is_not_a_new_budget(journal, change):
    records, make = journal
    sol = fake.Sol(conflict, corrected)
    fake.daily(fake.evidence(), make(sol), after=CARDS)
    if change in {"source", "assessor", "input"}:
        binding = (
            {"source_revision": "other-source"} if change == "source"
            else {"configured_assessor": {"deployment": "other"}} if change == "assessor" else {}
        )
        data = fake.evidence()
        if change == "input":
            data = (replace(data[0], expectation={"root_cause": "different"}), *data[1:])
        with pytest.raises(StateError):
            fake.daily(data, make(sol, **binding), after=CARDS)
    else:
        request = records.read_artifact("assessment/calls/second/request")
        request = {k: request[k] for k in ("instructions", "payload", "schema")}
        if change == "prompt":
            request["instructions"] += "\nDifferent instruction."
        elif change == "schema":
            request["schema"]["description"] = "different"
        else:
            request["payload"]["review"] = request["payload"].pop("correction")
        with pytest.raises(StateError):
            asyncio.run(make(sol).complete_json(**request))
    assert len(sol.calls) == 2


@pytest.mark.parametrize("evidence_gap", [False, True])
def test_old_completed_results_restore_without_new_contract_or_calls(tmp_path, monkeypatch, evidence_gap):
    runner_fake.fake_storage(monkeypatch)
    h = runner_fake.Harness(tmp_path)
    h.cloud.query_complete = not evidence_gap
    write = RecordStore.save_artifact
    def historical(records, key, value):
        if "unit_result" in value:
            value.pop("call_contract", None)
            value.pop("assessment_input_final", None)
        write(records, key, value)
    monkeypatch.setattr(RecordStore, "save_artifact", historical)
    result = h.daily()
    h.cloud.query_complete = True
    counts = len(h.sol.calls), len(h.cloud.events)
    original = RecordStore.read_completed
    def legacy(self, key, **kwargs):
        return None if key == "assessment-call-contract" else original(self, key, **kwargs)
    monkeypatch.setattr(RecordStore, "read_completed", legacy)
    assert h.daily().to_dict() == result.to_dict()
    assert (len(h.sol.calls), len(h.cloud.events)) == counts


@pytest.mark.parametrize("remaining", ["none", "unknown", "evidence_gap"])
def test_runner_correction_survives_final_save_crash_and_freezes_excluded_results(
    tmp_path, monkeypatch, remaining,
):
    runner_fake.fake_storage(monkeypatch)
    h = runner_fake.Harness(tmp_path)
    ensure, poll = h.cloud.ensure_monitor, h.cloud.get_insights_run
    async def monitor(name):
        result = await ensure(name)
        h.cloud.cards[name] = []
        return result
    async def insights(name, run_id):
        value = await poll(name, run_id)
        if h.cloud.cards[name]:
            h.cloud.cards[name] = list(CARDS)
        return value
    h.cloud.ensure_monitor, h.cloud.get_insights_run = monitor, insights
    def result(payload):
        if payload["target"]["validation_mode"] == "baseline":
            return fake.output(payload)
        if "correction" not in payload:
            return conflict(payload)
        value = corrected(payload)
        if remaining == "unknown":
            value["cards"][1].update(core="unknown", root_group=None, expected_match=False)
        elif remaining == "evidence_gap":
            value["limitations"] = ["incomplete_evidence"]
        return value
    h.sol = fake.Sol(result)
    original = RecordStore.save_artifact
    def crash(records, key, value):
        if "/assessments/" in key and "/calls/" not in key and value.get("private_detail", {}).get("correction"):
            raise CheckpointError()
        original(records, key, value)
    monkeypatch.setattr(RecordStore, "save_artifact", crash)
    with pytest.raises(CheckpointError):
        h.daily(test_run=True, rerun=1)
    counts = len(h.sol.calls), len(h.cloud.invocations), len(h.cloud.starts)
    events = len(h.cloud.events)
    target = h.catalog.targets[1]
    pending = h.store.run("trial").read(f"targets/{target.key}/assessment")
    monkeypatch.setattr(RecordStore, "save_artifact", original)
    for changes in ((), ("src/agent_insights_quality/assessment.py",)):
        with pytest.raises(StateError, match="assessment_pending_binding_mismatch"):
            h.daily(test_run=True, rerun=1, revision="source-two", changes=changes)
        assert len(h.cloud.events) == events and len(h.sol.calls) == counts[0]
        assert h.store.run("trial").read(f"targets/{target.key}/assessment") == pending
    first, second = h.daily(test_run=True, rerun=1), h.daily(test_run=True, rerun=1)
    assert first.to_dict() == second.to_dict()
    assert (first.score is None) == (remaining != "none")
    assert (len(h.sol.calls), len(h.cloud.invocations), len(h.cloud.starts)) == counts
    assert counts == (3, 40, 2)
    assert h.store.run("trial").read(f"targets/{target.key}/assessment")["artifact"] == pending["artifact"]


@pytest.mark.parametrize("legacy_binding", [False, True])
def test_evidence_recovery_pending_initial_resumes_frozen_snapshot_without_queries(
    tmp_path, monkeypatch, legacy_binding,
):
    runner_fake.fake_storage(monkeypatch)
    h = runner_fake.Harness(tmp_path)
    h.cloud.query_complete = False
    assert h.daily(test_run=True, rerun=1).score is None
    target = h.catalog.targets[1]
    records = h.store.run("trial")
    original_source = records.read(f"targets/{target.key}/source")
    original_visible = records.read_completed(original_source["work_key"] + "/insights")["visible_snapshot"]
    if legacy_binding:
        progress = RecordStore.save_progress
        init = DailyAssessmentCalls.__init__
        def legacy_progress(self, key, value):
            if key.endswith("/assessment"):
                value = {k: v for k, v in value.items() if k not in {"evidence_key", "evidence_run_id"}}
            progress(self, key, value)
        def legacy_init(self, sol, records, key, binding, save):
            binding = {k: v for k, v in binding.items() if k not in {"evidence_key", "evidence_run_id"}}
            init(self, sol, records, key, binding, save)
        monkeypatch.setattr(RecordStore, "save_progress", legacy_progress)
        monkeypatch.setattr(DailyAssessmentCalls, "__init__", legacy_init)
    completed = RecordStore.save_completed
    def crash(self, key, value):
        if key.startswith(f"targets/{target.key}/assessments/") and key.endswith("/calls/initial"):
            raise CheckpointError()
        completed(self, key, value)
    monkeypatch.setattr(RecordStore, "save_completed", crash)
    h.cloud.query_complete = True
    h.clock.value += timedelta(seconds=30)
    with pytest.raises(CheckpointError):
        h.daily(test_run=True, rerun=1)
    pending = records.read(f"targets/{target.key}/assessment")
    current_source = records.read(f"targets/{target.key}/source")
    assert current_source["result"] == original_source["result"]
    assert current_source["evidence_key"] != original_source["evidence_key"]
    assert records.read_artifact(pending["artifact"] + "/calls/initial/output")
    frozen = records.read_artifact(current_source["evidence_key"])
    counts = len(h.sol.calls), len(h.cloud.events), len(h.cloud.invocations), len(h.cloud.starts)
    h.clock.value += timedelta(seconds=60)
    monkeypatch.setattr(RecordStore, "save_completed", completed)
    result = h.daily(test_run=True, rerun=1)
    assert result.score == 100
    assert (len(h.sol.calls), len(h.cloud.events), len(h.cloud.invocations), len(h.cloud.starts)) == counts
    assert counts[0] == 4
    reference = records.read(f"targets/{target.key}/assessment")
    assert reference["artifact"] == pending["artifact"]
    detail = records.read_artifact(reference["artifact"])["private_detail"]
    assert detail["input"]["snapshot"] == frozen
    assert records.read_completed(current_source["work_key"] + "/insights")["visible_snapshot"] == original_visible


@pytest.mark.parametrize("corruption", [
    "work", "assessor", "evidence", "phase", "applied", "applied_output", "journal", "input",
    "target_mode", "expectation",
])
def test_runner_pending_binding_corruption_stops_before_any_provider(
    tmp_path, monkeypatch, corruption,
):
    runner_fake.fake_storage(monkeypatch)
    h = runner_fake.Harness(tmp_path, issues=0)
    target = h.catalog.targets[0]
    completed = RecordStore.save_completed
    def crash(self, key, value):
        if key.endswith("/calls/initial"):
            raise CheckpointError()
        completed(self, key, value)
    monkeypatch.setattr(RecordStore, "save_completed", crash)
    with pytest.raises(CheckpointError):
        h.daily(test_run=True, rerun=1)
    monkeypatch.setattr(RecordStore, "save_completed", completed)
    records = h.store.run("trial")
    key = f"targets/{target.key}/assessment"
    pending = records.read(key)
    counts = len(h.sol.calls), len(h.cloud.events)
    if corruption in {"work", "assessor", "evidence", "phase", "applied", "applied_output"}:
        broken = deepcopy(pending)
        if corruption == "work":
            broken["work_key"] += "-other"
        elif corruption == "assessor":
            broken["configured_assessor"]["deployment_name"] = "other"
        elif corruption in {"phase", "applied"}:
            broken["status"] = "invalid" if corruption == "phase" else "applied"
        elif corruption == "applied_output":
            broken.update(status="applied", artifact=pending["artifact"] + "/calls/initial/output")
        else:
            broken["evidence_key"] += "-other"
        with h.store.ownership():
            records.save_progress(key, broken)
    elif corruption == "expectation":
        h.catalog = replace(h.catalog, targets=(
            replace(target, expectation={"root_cause": "Changed without changing the frozen source."}),
        ))
    else:
        read = RecordStore.read_artifact
        def corrupted(self, key, **kwargs):
            value = read(self, key, **kwargs)
            if corruption == "journal" and key.endswith("/calls/binding"):
                value["source_revision"] = "different"
            elif corruption == "input" and key.endswith("/calls/initial/request"):
                value["payload"]["snapshot"]["observed_at"] = "2026-09-04T13:00:00+00:00"
            elif corruption == "target_mode" and key.endswith("/calls/initial/request"):
                value["payload"]["target"]["validation_mode"] = "deterministic"
            return value
        monkeypatch.setattr(RecordStore, "read_artifact", corrupted)
    with pytest.raises(StateError, match="assessment_pending_"):
        h.daily(test_run=True, rerun=1)
    assert (len(h.sol.calls), len(h.cloud.events)) == counts


def test_legacy_unfinished_calls_without_journal_fail_closed(tmp_path, monkeypatch):
    runner_fake.fake_storage(monkeypatch)
    h = runner_fake.Harness(tmp_path)
    h.sol.fail = True
    h.daily()
    count = len(h.sol.calls)
    original = RecordStore.read_completed
    def legacy(self, key, **kwargs):
        return None if key == "assessment-call-contract" else original(self, key, **kwargs)
    monkeypatch.setattr(RecordStore, "read_completed", legacy)
    h.sol.fail = False
    assert h.daily().score is None
    assert len(h.sol.calls) == count
    assert all(
        h.store.run("trial").read(f"targets/{target.key}/failure")["code"]
        == "assessment_legacy_call_state_unavailable"
        for target in h.catalog.targets
    )
