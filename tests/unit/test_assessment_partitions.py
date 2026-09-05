"""Offline raw-envelope partitioning and conservative holistic Daily assessment."""

from copy import deepcopy
from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

import test_assessment as fake
from agent_insights_quality.assessment import AssessmentError
from agent_insights_quality.assessment_partition import (
    conversation_groups, expand_payload, intern_payload, partition_payload, payload_size,
)
from agent_insights_quality.errors import QualityError
from agent_insights_quality.results import ExclusionReason
from agent_insights_quality.telemetry import ResponseScope


def independent(mode="model_mediated"):
    data = fake.evidence(mode)
    snapshot = replace(data[3], scopes=tuple(
        replace(scope, operation_ids=(f"operation-{scope.response_id.split('-')[1]}",))
        for scope in data[3].scopes
    ))
    return (*data[:3], snapshot)


def judgment(payload, index, *, observed=True, violation=False):
    attempt = next(item for item in payload["attempts"] if item["index"] == index)
    step = next(step for step in attempt["steps"] if step["phase"] == "probe")
    refs = step["allowed_citation_refs"]
    sufficient = step["endpoint_ref"] is not None and len(refs) > 1
    return {
        "index": index, "sufficient": sufficient, "observed": observed and sufficient,
        "contract_violation": violation and sufficient,
        "citations": [{
            "attempt": index, "step_id": step["step_id"], "refs": refs,
        }] if sufficient else [],
        "reason": "Actual synthetic response and complete raw scoped evidence.",
    }


class StageSol:
    def __init__(self, *, observations=10, violation=None, mutate=None):
        self.observations = observations
        self.violation = violation
        self.mutate = mutate
        self.calls = []
        self.schemas = []

    async def complete_json(self, *, instructions, payload, schema):
        self.calls.append(deepcopy(payload))
        self.schemas.append(deepcopy(schema))
        value = {"attempts": [
            judgment(
                payload, item["index"],
                observed=item["index"] <= self.observations and item["index"] != self.violation,
                violation=item["index"] == self.violation,
            )
            for item in payload["attempts"]
        ]}
        if self.mutate:
            self.mutate(value, payload, len(self.calls))
        return value


def raw_payload(data):
    return fake.stage(data, StageSol()).private_detail["input"]


def single_group_limit(payload):
    partitions = partition_payload(payload, 1)
    return max(payload_size(part.payload) for part in partitions) + 32


def assert_exact_partition_cover(payload, partitions):
    assert sorted(index for part in partitions for index in part.indices) == list(range(1, 11))
    originals = {item["index"]: item for item in payload["attempts"]}
    decoded = [expand_payload(part.payload) for part in partitions]
    for part in decoded:
        for attempt in part["attempts"]:
            assert attempt == originals[attempt["index"]]
    for name in ("snapshot", "visible_snapshot"):
        if name not in payload:
            continue
        source = payload[name]
        refs = {}
        scopes = {}
        for part in decoded:
            snapshot = part[name]
            for row in snapshot["records"]:
                assert row == next(item for item in source["records"] if item["ref"] == row["ref"])
                refs[row["ref"]] = row
            for scope in snapshot["scopes"]:
                scopes[scope["response_id"]] = scope
            for key in set(source) - {"records", "scopes"}:
                assert snapshot[key] == source[key]
        assert set(refs) == {row["ref"] for row in source["records"]}
        assert scopes == {scope["response_id"]: scope for scope in source["scopes"]}


def test_staging_batches_cover_all_ten_and_every_raw_envelope_without_new_traffic():
    data = independent()
    original_invocations = deepcopy(data[2])
    payload = raw_payload(data)
    limit = single_group_limit(payload)
    sol = StageSol()
    result = fake.stage(data, sol, max_payload_bytes=limit)
    assert result.status == "PASS" and result.passing_attempts == 10
    assert len(result.judgments) == 10
    assert len(sol.calls) == 10
    assert all(payload_size(call) <= limit for call in sol.calls)
    for call, schema in zip(sol.calls, sol.schemas, strict=True):
        indices = [item["index"] for item in call["attempts"]]
        attempt_schema = schema["properties"]["attempts"]
        assert attempt_schema["minItems"] == attempt_schema["maxItems"] == len(indices)
        assert attempt_schema["items"]["properties"]["index"]["enum"] == indices
        Draft202012Validator.check_schema(schema)
    assert_exact_partition_cover(payload, partition_payload(payload, limit))
    assert result.private_detail["input"] == payload
    assert all(part["status"] == "completed" for part in result.private_detail["partitions"])
    assert data[2] == original_invocations


def test_real_two_megabyte_default_splits_complete_raw_conversations_instead_of_rejecting_all():
    data = independent()
    records = tuple({
        **row, "raw": {**row["raw"], "complete_synthetic_blob": "x" * 110_000},
    } for row in data[3].records)
    data = (*data[:3], replace(data[3], records=records))
    sol = StageSol()
    result = fake.stage(data, sol)
    assert payload_size(result.private_detail["input"]) > 2_000_000
    assert result.status == "PASS" and result.passing_attempts == 10
    assert len(sol.calls) == 2
    assert all(payload_size(call) <= 2_000_000 for call in sol.calls)
    assert {
        row["ref"]: row for call in sol.calls for row in call["snapshot"]["records"]
    } == {row["ref"]: row for row in records}


@pytest.mark.parametrize("link", ["session", "operation", "raw_ref", "prior_response", "visible"])
def test_shared_dependencies_join_whole_attempts_transitively(link):
    payload = raw_payload(independent())
    if link == "session":
        for index in (1, 2, 3):
            payload["attempts"][index - 1]["steps"][0]["execution"]["session_id"] = "joined-session"
    elif link == "operation":
        for scope in payload["snapshot"]["scopes"]:
            if scope["response_id"] in {"response-1-probe", "response-2-setup", "response-3-probe"}:
                scope["operation_ids"] = ["joined-operation"]
    elif link == "raw_ref":
        scopes = payload["snapshot"]["scopes"]
        scopes[2]["evidence_refs"].append(scopes[1]["evidence_refs"][0])
        scopes[4]["evidence_refs"].append(scopes[3]["evidence_refs"][0])
    elif link == "prior_response":
        payload["attempts"][1]["steps"][0]["request"]["previous_response_id"] = "response-1-probe"
        payload["attempts"][2]["steps"][0]["execution"]["response"]["previous_response_id"] = "response-2-probe"
    else:
        payload["visible_snapshot"] = deepcopy(payload["snapshot"])
        for scope in payload["visible_snapshot"]["scopes"]:
            if scope["response_id"] in {"response-1-probe", "response-2-setup", "response-3-probe"}:
                scope["operation_ids"] = ["joined-visible-operation"]
    assert conversation_groups(payload)[0] == (1, 2, 3)
    parts = partition_payload(payload, single_group_limit(payload))
    assert any({1, 2, 3} <= set(part.indices) for part in parts)
    assert_exact_partition_cover(payload, parts)


def test_noncontiguous_groups_are_not_renumbered_or_split():
    payload = raw_payload(independent())
    payload["attempts"][4]["steps"][0]["execution"]["session_id"] = "session-1"
    groups = conversation_groups(payload)
    assert groups[0] == (1, 5)
    partitions = partition_payload(payload, single_group_limit(payload))
    assert any(part.indices == (1, 5) for part in partitions)
    assert_exact_partition_cover(payload, partitions)


def test_unassigned_raw_records_and_unplanned_scopes_remain_shared_context():
    data = independent()
    snapshot = replace(
        data[3],
        records=(*data[3].records, {
            "ref": "unassigned", "raw": {"unexpected": {"all": ["synthetic content"]}},
        }),
        scopes=(*data[3].scopes, ResponseScope(
            "unplanned-response", ("unplanned-operation",), ("unassigned",), ("unassigned",),
        )),
    )
    payload = raw_payload((*data[:3], snapshot))
    parts = partition_payload(payload, single_group_limit(payload))
    for part in parts:
        assert payload["snapshot"]["records"][-1] in part.payload["snapshot"]["records"]
        assert payload["snapshot"]["scopes"][-1] in part.payload["snapshot"]["scopes"]
    assert_exact_partition_cover(payload, parts)


def test_one_oversized_indivisible_group_is_explicitly_incomplete_not_resampled():
    data = independent()
    limit = single_group_limit(raw_payload(data))
    records = list(data[3].records)
    records[0] = {**records[0], "raw": {**records[0]["raw"], "complete_blob": "synthetic" * limit}}
    data = (*data[:3], replace(data[3], records=tuple(records)))
    sol = StageSol()
    result = fake.stage(data, sol, max_payload_bytes=limit)
    assert result.status == "INCOMPLETE" and result.passing_attempts == 9
    assert result.reasons == ("assessment_conversation_too_large",)
    assert result.judgments[0]["sufficient"] is False
    assert len(sol.calls) == 9
    oversized = result.private_detail["partitions"][0]
    assert oversized["status"] == "oversized" and oversized["indices"] == [1]
    assert oversized["input"]["snapshot"]["records"][0] == records[0]
    assert len(result.judgments) == 10


def test_shared_operation_over_all_attempts_stays_indivisible():
    data = fake.evidence()
    limit = payload_size(raw_payload(data)) - 1
    sol = StageSol()
    result = fake.stage(data, sol, max_payload_bytes=limit)
    assert result.status == "INCOMPLETE" and sol.calls == []
    assert result.private_detail["partitions"][0]["indices"] == list(range(1, 11))
    assert len(result.judgments) == 10


@pytest.mark.parametrize("mode,observations,status", [
    ("baseline", 6, "PASS"), ("deterministic", 6, "PASS"),
    ("model_mediated", 6, "PASS"), ("model_mediated", 5, "FAIL"),
])
def test_partitioning_preserves_six_of_ten_threshold(mode, observations, status):
    data = independent(mode)
    sol = StageSol(observations=observations)
    result = fake.stage(data, sol, max_payload_bytes=single_group_limit(raw_payload(data)))
    assert result.status == status and result.passing_attempts == observations
    assert len(sol.calls) == len(result.judgments) == 10


@pytest.mark.parametrize("mode", ["baseline", "deterministic"])
def test_late_partition_contract_violation_still_disqualifies(mode):
    data = independent(mode)
    sol = StageSol(violation=10)
    result = fake.stage(data, sol, max_payload_bytes=single_group_limit(raw_payload(data)))
    assert result.status == "FAIL" and result.passing_attempts == 9
    assert result.reasons == ("proven_contract_violation",)
    assert len(sol.calls) == 10


@pytest.mark.parametrize("mutation", [
    lambda output: output.update(extra="invalid"),
    lambda output: output["attempts"].clear(),
    lambda output: output["attempts"].append(deepcopy(output["attempts"][0])),
    lambda output: output["attempts"][0].update(index=1),
    lambda output: output["attempts"][0]["citations"][0].update(attempt=1),
    lambda output: output["attempts"][0]["citations"][0]["refs"].append("row-1-probe"),
    lambda output: output["attempts"][0]["citations"][0]["refs"].append("unknown-ref"),
])
def test_bad_partition_outputs_retain_successful_partitions_and_reject_cross_group_proof(mutation):
    def mutate(output, payload, count):
        if count == 2:
            mutation(output)
    data = independent()
    sol = StageSol(mutate=mutate)
    with pytest.raises(AssessmentError) as failure:
        fake.stage(data, sol, max_payload_bytes=single_group_limit(raw_payload(data)))
    detail = failure.value.private_detail
    assert len(sol.calls) == 2
    assert detail["partitions"][0]["status"] == "completed"
    assert detail["partitions"][0]["output"]["attempts"][0]["index"] == 1
    assert detail["partitions"][1]["status"] == "failed"
    assert detail["partitions"][1]["output"] is not None
    assert detail["input"] == raw_payload(data)


def test_provider_failure_retains_earlier_partition_outputs_and_remote_semantics():
    def fail(output, payload, count):
        if count == 2:
            raise QualityError("synthetic_sol_failure", request_accepted=None, retryable=False)
    data = independent()
    sol = StageSol(mutate=fail)
    with pytest.raises(QualityError) as failure:
        fake.stage(data, sol, max_payload_bytes=single_group_limit(raw_payload(data)))
    assert failure.value.code == "synthetic_sol_failure"
    assert failure.value.request_accepted is None and not failure.value.retryable
    detail = failure.value.private_detail
    assert detail["partitions"][0]["status"] == "completed"
    assert detail["partitions"][1]["status"] == "failed"
    assert detail["failure"]["code"] == "synthetic_sol_failure"


def test_lossless_interning_reconstructs_nested_references_without_interpreting_raw_fields():
    common = {"blob": "synthetic" * 500}
    nested = {"child": common, "copy": common}
    payload = {
        "snapshot": nested, "visible_snapshot": nested,
        "cards": [nested, common, None, {"references": "raw field, not decoder metadata"}],
        "reference": {"path": ["snapshot"], "source": ["untrusted"]},
    }
    encoded = intern_payload(payload)
    assert encoded["lossless_encoding"] == "json-path-references-v1"
    assert payload_size(encoded) < payload_size(payload)
    assert expand_payload(encoded) == payload
    assert payload["snapshot"]["copy"] == common
    assert intern_payload({"synthetic": 1}) == {"synthetic": 1}


def test_daily_dedup_keeps_full_cards_pages_revisions_and_raw_evidence_in_one_judgment():
    data = independent()
    old = {"id": "a", "body": "synthetic card evidence " * 250}
    newer = {**old, "updated_at": fake.END, "title": "updated"}
    history = {"id": "b", "body": "synthetic historical evidence " * 250}
    baseline_sol = fake.Sol()
    fake.daily(data, baseline_sol, before=(old, history), after=(newer, newer, history))
    original = baseline_sol.calls[0]
    limit = payload_size(intern_payload(original))
    assert limit < payload_size(original)
    sol = fake.Sol()
    result = fake.daily(
        data, sol, before=(old, history), after=(newer, newer, history), max_payload_bytes=limit,
    )
    assert len(sol.calls) == 1
    assert sol.calls[0]["lossless_encoding"] == "json-path-references-v1"
    assert expand_payload(sol.calls[0]) == original == result.private_detail["input"]
    assert len(original["card_snapshots"]["after"]) == 3
    assert fake.aggregate(data, result).score == 100.0
    assert result.private_detail["transport_input"] == sol.calls[0]


def test_daily_holistic_overflow_has_no_chunk_votes_noise_or_false_miss():
    data = independent()
    original = fake.daily(data).private_detail["input"]
    normalized = intern_payload(original)
    limit = payload_size(normalized) // 3
    sol = fake.Sol()
    result = fake.daily(data, sol, max_payload_bytes=limit)
    assert sol.calls == []
    assert result.reasons == ("assessment_context_incomplete",)
    assert result.unit_result.exclusion_reasons == (ExclusionReason.INCOMPLETE_ASSESSMENT,)
    assert all(card.core.value == "unknown" for card in result.unit_result.cards)
    assert fake.aggregate(data, result).score is None
    assert result.private_detail["input"] == original
    plan = result.private_detail["partition_plan"]
    assert len(plan) > 1
    assert sorted(index for part in plan for index in part["indices"]) == list(range(1, 11))
    assert all(part["status"] == "not_submitted_holistic_context_required" for part in plan)
    assert expand_payload(result.private_detail["transport_input"]) == original


def test_daily_normalized_focused_review_is_single_and_uses_original_stable_refs():
    data = independent()
    sol = fake.Sol()
    fake.daily(data, sol, after=())
    review = sol.calls[1]
    limit = payload_size(intern_payload(review))
    bounded_sol = fake.Sol()
    result = fake.daily(data, bounded_sol, after=(), max_payload_bytes=limit)
    assert len(bounded_sol.calls) == 2
    assert payload_size(bounded_sol.calls[1]) <= limit
    assert expand_payload(bounded_sol.calls[1]) == review
    assert result.private_detail["review"] is not None
    assert fake.aggregate(data, result).score == 0.0
    assert result.private_detail["resolved"]["attempts"][0]["citations"][0]["refs"] == [
        "endpoint-01-02", "row-1-probe",
    ]


def test_daily_lossless_transport_preserves_holistic_noise_and_duplicate_root_counts():
    def mixed(payload):
        output = fake.output(payload)
        output["cards"][-1].update(core="incorrect", root_group=None, expected_match=False)
        return output

    data = independent()
    cards = tuple({"id": name, "body": "synthetic card content " * 300} for name in ("a", "b", "c"))
    unbounded_sol = fake.Sol(mixed)
    original = fake.daily(data, unbounded_sol, after=cards)
    limit = max(payload_size(intern_payload(call)) for call in unbounded_sol.calls)
    assert limit < payload_size(unbounded_sol.calls[0])
    bounded_sol = fake.Sol(mixed)
    normalized = fake.daily(data, bounded_sol, after=cards, max_payload_bytes=limit)
    assert normalized.unit_result == original.unit_result
    assert len(bounded_sol.calls) == 2
    result = fake.aggregate(data, normalized)
    assert (
        result.counts.correct_issues, result.counts.noise_cards, result.counts.duplicate_cards,
    ) == (1, 1, 1)
    assert result.score == 44.4
    assert [
        expand_payload(call) for call in bounded_sol.calls
    ] == unbounded_sol.calls


def test_decoded_proof_still_requires_pre_insights_raw_values_not_just_reused_refs():
    data = independent()
    current = replace(data[3], records=tuple(
        {**row, "raw": {**row["raw"], "new_late_value": "synthetic"}}
        for row in data[3].records
    ))
    later_data = (*data[:3], current)
    original_sol = fake.Sol()
    fake.daily(later_data, original_sol, visible_snapshot=data[3])
    limit = payload_size(intern_payload(original_sol.calls[0]))
    result = fake.daily(
        later_data, fake.Sol(), visible_snapshot=data[3], max_payload_bytes=limit,
    )
    assert "card_proof_not_visible_before_insights" in result.reasons
    assert fake.aggregate(later_data, result).score is None


def test_transport_decoder_paths_are_not_original_evidence_citations():
    data = independent()
    original = fake.daily(data).private_detail["input"]
    limit = payload_size(intern_payload(original))

    def fabricated(payload):
        output = fake.output(payload)
        output["cards"][0]["citations"][0]["refs"].append("document.snapshot.records.1")
        return output

    with pytest.raises(AssessmentError, match="citation_invalid"):
        fake.daily(data, fake.Sol(fabricated), max_payload_bytes=limit)
