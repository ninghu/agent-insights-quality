"""Offline schema/policy/ownership tests, not evidence of assessor semantic accuracy."""

from copy import deepcopy
from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

from agent_insights_quality.assessment import (
    AssessmentError, STAGING_SCHEMA, reassess_staging_policy, staging_hygiene_fields,
)
from agent_insights_quality.staging_policy import STAGING_POLICY, StagingPolicy
import test_assessment as fake
import test_assessment_partitions as partitioned


def finding(payload, *, index=1, phase="probe", relation="independent_agent_defect"):
    attempt = next(item for item in payload["attempts"] if item["index"] == index)
    step = next(item for item in attempt["steps"] if item["phase"] == phase)
    return {
        "attempt": index, "relation": relation,
        "central_cause": "The summary truncation drops the required weather warning.",
        "violated_healthy_contract": "Retain the supplied safety warning in the summary.",
        "causal_independence": (
            "Omitting the safety warning is independent of the expected stale-temperature "
            "selection: correcting the temperature would not restore the omitted warning."
        ),
        "affected_component": "Delivered forecast summary",
        "behavior": "The scoped response omits the warning present in the tool facts.",
        "material_impact": "The required weather hazard warning is lost.",
        "uncertainty": None,
        "citations": [{
            "attempt": index, "step_id": step["step_id"], "refs": step["allowed_citation_refs"],
        }],
    }


def runtime_data(mode="model_mediated"):
    data = fake.evidence(mode)
    target = replace(data[0], expectation={
        "root_cause": "Forecast selects stale temperature rather than current supplied facts.",
        "healthy_contract": "Use current temperature and retain supplied weather hazard warnings.",
    })
    invocations = {
        key: replace(item, response={
            "id": item.response_id, "output": "Forecast: 18 degrees.",
        }) for key, item in data[2].items()
    }
    snapshot = replace(data[3], records=tuple({
        **row, "raw": {
            **row["raw"], "current_temperature": 23, "stale_temperature": 18,
            "weather_warning": "Severe synthetic storm", "output": "Forecast: 18 degrees.",
        },
    } for row in data[3].records))
    return target, data[1], invocations, snapshot


def with_finding(*, relation="independent_agent_defect", phase="probe", observations=8, mutate=None):
    def respond(payload):
        result = fake.output(payload, stage=True)
        for attempt in result["attempts"][observations:]:
            attempt["observed"] = False
        item = finding(payload, phase=phase, relation=relation)
        if relation == "handled_or_operational":
            item["violated_healthy_contract"] = None
        if relation == "unresolved_additional_root":
            item["uncertainty"] = "The scoped internal output is truncated; its final use is unknown."
        if mutate:
            mutate(item)
        result["additional_findings"] = [item]
        return result
    return fake.Sol(respond)


def probe_observation(data, *, response, trace):
    invocations = dict(data[2])
    invocations[(1, "probe")] = replace(invocations[(1, "probe")], response=response)
    records = tuple({
        **row, "raw": {"synthetic_response": "response-1-probe", **trace},
    } if row["ref"] == "row-1-probe" else row for row in data[3].records)
    return *data[:2], invocations, replace(data[3], records=records)


def test_expected_eight_with_no_additional_findings_passes_new_policy_once():
    sol = partitioned.StageSol(observations=8)
    result = fake.stage(runtime_data(), sol)
    assert result.status == result.root_hygiene_status == "PASS"
    assert result.passing_attempts == 8 and len(result.judgments) == 10
    assert result.additional_findings == () and result.root_hygiene_reasons == ()
    assert result.policy_version == "staging-root-hygiene-v3" and result.minimum_required == 8
    assert len(sol.calls) == 1


@pytest.mark.parametrize("mode", ["baseline", "deterministic", "model_mediated"])
def test_expected_observation_and_independent_agent_defect_coexist_and_fail(mode):
    sol = with_finding()
    result = fake.stage(runtime_data(mode), sol)
    assert result.status == result.root_hygiene_status == "FAIL"
    assert result.passing_attempts == 8
    assert result.judgments[0]["observed"] and not result.judgments[0]["contract_violation"]
    assert result.reasons == ("proven_additional_agent_defect",)
    assert result.additional_findings == tuple(result.private_detail["output"]["additional_findings"])
    assert result.private_detail["partitions"][0]["output"]["additional_findings"]
    assert len(sol.calls) == 1


def test_one_expected_cause_with_wrong_output_retry_latency_remains_one_root():
    data = probe_observation(
        runtime_data(), response={"output": "Forecast: 18 degrees. Severe synthetic storm."},
        trace={
            "selected_temperature": 18, "current_temperature": 23,
            "retries": [{"selected_temperature": 18}, {"selected_temperature": 18}],
            "retry_duration_ms": 420, "output": "Forecast: 18 degrees. Severe synthetic storm.",
        },
    )
    def symptoms(item):
        item.update(
            central_cause="The expected stale-value selection triggers retries of that same bad value.",
            causal_independence="Wrong answer, retry and latency all follow the expected stale selection.",
            behavior="The scoped trace repeats the stale forecast before delivering it.",
            material_impact="Three symptoms of the expected causal defect.",
        )
    result = fake.stage(data, with_finding(relation="expected_root_consequence", mutate=symptoms))
    assert result.status == result.root_hygiene_status == "PASS" and result.passing_attempts == 8
    assert len(result.additional_findings) == 1


def test_handled_dependency_errors_and_truthful_operational_observation_are_retained():
    data = runtime_data()
    invocations = dict(data[2])
    invocations[(1, "probe")] = replace(invocations[(1, "probe")], response={
        "output": "Primary support lookup unavailable; the approved fallback resolved the request.",
    })
    records = list(data[3].records)
    records[1] = {**records[1], "raw": {
        "dependency_status": 503, "fallback_status": 200, "request_resolved": True,
    }}
    def operational(item):
        item.update(
            central_cause="The primary dependency returned a real 503.",
            affected_component="Support primary dependency and approved fallback",
            behavior="Primary lookup failed; scoped fallback succeeded and resolved the request.",
            causal_independence="Recovery meets the Agent contract; the dependency failure is real.",
            material_impact="Dependency availability observation, not an additional Agent violation.",
        )
    result = fake.stage(
        (*data[:2], invocations, replace(data[3], records=tuple(records))),
        with_finding(relation="handled_or_operational", mutate=operational),
    )
    assert result.status == "PASS"
    assert result.additional_findings[0]["relation"] == "handled_or_operational"
    assert result.additional_findings[0]["violated_healthy_contract"] is None


@pytest.mark.parametrize("relation,status", [
    ("independent_agent_defect", "FAIL"), ("unresolved_additional_root", "INCOMPLETE"),
])
def test_material_internal_surface_requires_independence_and_impact_not_delivered_claim(relation, status):
    data = probe_observation(
        runtime_data(), response={"output": "Two-day itinerary with a quoted total of 200."},
        trace={
            "internal_review": "Review pending. Review pending. Review pending.",
            "budget_verification": None,
            "review_complete": relation == "independent_agent_defect",
            "delivered_itinerary": "Two-day itinerary with a quoted total of 200.",
        },
    )
    data = (replace(data[0], expectation={
        "root_cause": "The expected route-order defect.",
        "healthy_contract": "Internally verify budget and order routes correctly.",
    }), *data[1:])
    def internal(item):
        item.update(
            central_cause="Internal travel review repeats irrelevant text instead of checking the budget.",
            affected_component="Internal travel review, not the delivered itinerary",
            behavior="The review trace contains repeated padding; budget verification is absent.",
            violated_healthy_contract="Verify the itinerary budget in the internal review.",
            causal_independence="Correcting the expected route-order defect would not restore this review.",
            material_impact="The required budget check is lost, independently of route ordering.",
        )
    result = fake.stage(data, with_finding(relation=relation, mutate=internal))
    assert result.status == result.root_hygiene_status == status
    assert result.passing_attempts == 8
    assert len(result.judgments) == 10


def test_option_cardinality_without_material_loss_is_not_an_additional_root():
    data = probe_observation(
        runtime_data(), response={"output": "Options A and B are the two suitable routes."},
        trace={
            "supplied_routes": ["A", "B", "C", "D"], "matching_constraints": ["A", "B"],
            "selected_routes": ["A", "B"], "retained_decision_fields": ["cost", "accessibility"],
        },
    )
    def selection(item):
        item.update(
            central_cause="Selection of the two relevant options from four supplied options.",
            affected_component="Travel option selection",
            behavior="Two options satisfy the requested constraints and retain decision-critical content.",
            causal_independence="No separate obligation is violated; selection alone proves no extra defect.",
            material_impact=None,
        )
    result = fake.stage(data, with_finding(relation="handled_or_operational", mutate=selection))
    assert result.status == "PASS"


@pytest.mark.parametrize("observations,status", [(8, "PASS"), (7, "FAIL")])
def test_expected_misses_are_not_reclassified_as_additional_roots(observations, status):
    result = fake.stage(runtime_data(), partitioned.StageSol(observations=observations))
    assert result.status == status and result.additional_findings == ()
    assert result.root_hygiene_status == "PASS"
    assert not any(item["contract_violation"] for item in result.judgments)
    assert result.reasons == (() if status == "PASS" else ("observation_threshold_not_met",))


@pytest.mark.parametrize("mutation,error", [
    (lambda item: item.update(citations=[]), "output_invalid"),
    (lambda item: item["citations"][0].update(refs=["unknown"]), "citation_invalid"),
    (lambda item: item["citations"][0].update(refs=["endpoint-01-02"]), "proof_missing"),
    (lambda item: item["citations"][0].update(refs=["row-1-probe"]), "proof_missing"),
    (lambda item: item["citations"][0].update(attempt=2), "citation_invalid"),
    (lambda item: item["citations"][0].update(refs=["endpoint-01-02", "row-2-probe"]), "citation_invalid"),
    (lambda item: item.update(citations=[
        {"attempt": 1, "step_id": "setup", "refs": ["row-1-setup"]},
        {"attempt": 1, "step_id": "probe", "refs": ["endpoint-01-02"]},
    ]), "proof_missing"),
])
@pytest.mark.parametrize("relation", [
    "independent_agent_defect", "expected_root_consequence",
    "handled_or_operational", "unresolved_additional_root",
])
def test_every_additional_candidate_needs_same_turn_owned_endpoint_trace_pair(mutation, error, relation):
    with pytest.raises(AssessmentError, match=error):
        fake.stage(runtime_data(), with_finding(relation=relation, mutate=mutation))


@pytest.mark.parametrize("field", [
    "central_cause", "causal_independence", "affected_component", "behavior",
    "violated_healthy_contract", "material_impact",
])
@pytest.mark.parametrize("value", ["", "   ", "x" * 2001])
def test_additional_diagnosis_fields_are_bounded_nonblank_not_model_labels_alone(field, value):
    with pytest.raises(AssessmentError, match="output_invalid"):
        fake.stage(runtime_data(), with_finding(mutate=lambda item: item.update({field: value})))


@pytest.mark.parametrize("mutation", [
    lambda item: item.update(violated_healthy_contract=None),
    lambda item: item.update(material_impact=None),
    lambda item: item.update(uncertainty="Still unproven but labelled proven"),
    lambda item: item.update(relation="handled_or_operational"),
    lambda item: item.update(relation="unresolved_additional_root"),
])
def test_relation_field_semantics_cannot_be_inconsistent(mutation):
    with pytest.raises(AssessmentError, match="additional_finding_invalid"):
        fake.stage(runtime_data(), with_finding(mutate=mutation))


def test_baseline_cannot_claim_consequences_of_a_nonexistent_expected_root():
    with pytest.raises(AssessmentError, match="additional_finding_invalid"):
        fake.stage(runtime_data("baseline"), with_finding(relation="expected_root_consequence"))


def test_unowned_raw_sibling_is_not_proof_even_with_shared_operation():
    data = runtime_data()
    snapshot = replace(data[3], records=(*data[3].records, {
        "ref": "unowned-sibling", "raw": {"operation_Id": "synthetic-operation", "label": "defect"},
    }))
    def unowned(item):
        item["citations"][0]["refs"].append("unowned-sibling")
    with pytest.raises(AssessmentError, match="citation_invalid"):
        fake.stage((*data[:3], snapshot), with_finding(mutate=unowned))


def test_setup_proves_additional_finding_but_not_expected_activation_or_readiness():
    data = runtime_data()
    invocations = dict(data[2])
    invocations.pop((1, "probe"))
    result = fake.stage((*data[:2], invocations, data[3]), with_finding(phase="setup", observations=10))
    assert result.status == "FAIL" and result.passing_attempts == 9
    assert result.judgments[0]["sufficient"] is False
    def wrong_activation(payload):
        value = fake.output(payload, stage=True)
        value["attempts"][0]["citations"] = finding(payload, phase="setup")["citations"]
        return value
    with pytest.raises(AssessmentError, match="proof_missing"):
        fake.stage(data, fake.Sol(wrong_activation))


@pytest.mark.parametrize("relation,status", [
    ("independent_agent_defect", "FAIL"), ("unresolved_additional_root", "INCOMPLETE"),
])
def test_last_partition_diagnostics_apply_after_all_ten_without_extra_model_calls(relation, status):
    def mutate(value, payload, count):
        item = finding(payload, index=payload["attempts"][0]["index"], relation=relation)
        if relation == "unresolved_additional_root":
            item["uncertainty"] = "Contract-relevant scope is truncated."
        if count == 10:
            value["additional_findings"].append(item)
    data = partitioned.independent()
    sol = partitioned.StageSol(observations=8, mutate=mutate)
    result = fake.stage(data, sol, max_payload_bytes=partitioned.single_group_limit(partitioned.raw_payload(data)))
    assert result.status == status and result.passing_attempts == 8
    assert len(sol.calls) == len(result.judgments) == 10
    assert result.additional_findings[0]["attempt"] == 10
    assert result.private_detail["partitions"][-1]["output"]["additional_findings"]
    assert all(part["status"] == "completed" for part in result.private_detail["partitions"])


def test_cross_partition_proof_cannot_be_borrowed_even_if_valid_in_whole_packet():
    original = partitioned.raw_payload(partitioned.independent())
    def mutate(value, payload, count):
        if count == 2:
            value["additional_findings"] = [finding(original, index=1)]
    data = partitioned.independent()
    sol = partitioned.StageSol(mutate=mutate)
    with pytest.raises(AssessmentError, match="output_invalid") as error:
        fake.stage(data, sol, max_payload_bytes=partitioned.single_group_limit(original))
    assert len(sol.calls) == 2
    assert error.value.private_detail["partitions"][0]["status"] == "completed"
    assert error.value.private_detail["partitions"][1]["status"] == "failed"


def test_repeated_observations_are_retained_without_root_counting_or_observation_bonus():
    def mutate(value, payload, count):
        value["additional_findings"] = [finding(payload, index=payload["attempts"][0]["index"])]
    data = partitioned.independent()
    result = fake.stage(data, partitioned.StageSol(mutate=mutate),
                        max_payload_bytes=partitioned.single_group_limit(partitioned.raw_payload(data)))
    assert result.status == "FAIL" and result.passing_attempts == 10
    assert len(result.additional_findings) == 10
    assert len({item["central_cause"] for item in result.additional_findings}) == 1
    assert "score" not in result.to_private_dict()
    restored = reassess_staging_policy(*data, result.to_private_dict())
    assert restored.additional_findings == result.additional_findings


def test_new_model_boundary_requires_explicit_additional_findings_and_request_semantics():
    def omit(payload):
        value = fake.output(payload, stage=True)
        value.pop("additional_findings")
        return value
    with pytest.raises(AssessmentError, match="output_invalid"):
        fake.stage(runtime_data(), fake.Sol(omit))
    sol = with_finding()
    fake.stage(runtime_data(), sol)
    schema = sol.schemas[0]
    Draft202012Validator.check_schema(schema)
    valid = fake.output(sol.calls[0], stage=True)
    valid["additional_findings"] = [finding(sol.calls[0])]
    assert Draft202012Validator(schema).is_valid(valid)
    valid["additional_findings"][0]["material_impact"] = None
    assert not Draft202012Validator(schema).is_valid(valid)
    assert STAGING_SCHEMA["properties"]["additional_findings"]["maxItems"] == 100


def test_additional_lists_are_bounded_for_whole_target_and_each_partition():
    def too_many(value, payload, count):
        index = payload["attempts"][0]["index"]
        value["additional_findings"] = [finding(payload, index=index)] * (len(payload["attempts"]) * 10 + 1)
    data = partitioned.independent()
    for limit in (2_000_000, partitioned.single_group_limit(partitioned.raw_payload(data))):
        with pytest.raises(AssessmentError, match="output_invalid"):
            fake.stage(data, partitioned.StageSol(mutate=too_many), max_payload_bytes=limit)


@pytest.mark.parametrize("version,minimum", [("staging-observations-v2", 8), ("synthetic-six-v1", 6)])
def test_real_legacy_output_retains_policy_and_cannot_gain_v3_hygiene_from_threshold_migration(version, minimum):
    data = runtime_data()
    previous = fake.legacy_staging_result(
        fake.stage(data, policy=StagingPolicy(version, minimum)).to_private_dict(),
    )
    original = deepcopy(previous)
    fields = staging_hygiene_fields(previous)
    assert fields["root_hygiene_status"] == "NOT_EVALUATED" and fields["additional_findings"] is None
    result = reassess_staging_policy(*data, previous)
    assert result.status == "INCOMPLETE" and result.passing_attempts == 10
    assert result.policy_version == STAGING_POLICY.version
    assert result.root_hygiene_status == "NOT_EVALUATED"
    assert result.additional_findings is None
    assert result.reasons == ("legacy_root_hygiene_not_evaluated",)
    assert previous == original and previous["status"] == "PASS"
    assert previous["policy_version"] == version and previous["minimum_required"] == minimum
    assert result.private_detail["output"] == previous["private_detail"]["output"]
    assert result.private_detail["partitions"] == previous["private_detail"]["partitions"]
    historic_threshold = reassess_staging_policy(*data, previous, policy=StagingPolicy("staging-observations-v2", 8))
    assert historic_threshold.status == "PASS" and historic_threshold.root_hygiene_status == "NOT_EVALUATED"


def test_unversioned_history_is_explicitly_not_evaluated_without_changing_original():
    previous = {"status": "PASS", "passing_attempts": 6, "reasons": []}
    original = deepcopy(previous)
    assert staging_hygiene_fields(previous)["root_hygiene_status"] == "NOT_EVALUATED"
    assert previous == original
    with pytest.raises(AssessmentError, match="hygiene_checkpoint_invalid"):
        staging_hygiene_fields({**previous, "policy_version": STAGING_POLICY.version})


@pytest.mark.parametrize("mutation", [
    lambda value: value["private_detail"]["output"]["additional_findings"].clear(),
    lambda value: value["private_detail"]["partitions"][-1]["output"]["additional_findings"].clear(),
    lambda value: value["additional_findings"][0].update(central_cause="Rewritten conclusion"),
    lambda value: value.pop("root_hygiene_status"),
    lambda value: value["private_detail"]["partitions"][0]["indices"].append(10),
    lambda value: value["private_detail"]["partitions"][0]["input"]["snapshot"]["records"].clear(),
    lambda value: value["private_detail"]["partitions"][0]["input"]["snapshot"]["records"][0]["raw"].update(
        output="An unrelated rewritten response",
    ),
    lambda value: value.update(root_hygiene_status="PASS", root_hygiene_reasons=[]),
    lambda value: value.update(root_hygiene_status=[]),
])
def test_retained_hygiene_judgments_partition_provenance_and_summary_must_agree(mutation):
    data = runtime_data()
    previous = fake.stage(data, with_finding()).to_private_dict()
    # Wire serialization gives partition/output/summary independent object graphs.
    import json
    previous = json.loads(json.dumps(previous))
    mutation(previous)
    with pytest.raises(AssessmentError):
        reassess_staging_policy(*data, previous)


def test_new_policy_checkpoint_cannot_hide_missing_review_behind_pass():
    previous = fake.stage(runtime_data()).to_private_dict()
    previous.update(
        root_hygiene_status="NOT_EVALUATED", additional_findings=None,
        root_hygiene_reasons=["legacy_root_hygiene_not_evaluated"],
    )
    with pytest.raises(AssessmentError, match="hygiene_checkpoint_invalid"):
        staging_hygiene_fields(previous)


def test_no_attributable_evidence_cannot_claim_hygiene_pass_from_empty_findings():
    result = fake.stage(fake.evidence(no_trace=range(1, 11)))
    assert result.status == result.root_hygiene_status == "INCOMPLETE"
    assert "insufficient_hygiene_evidence" in result.root_hygiene_reasons
    assert result.additional_findings == ()


@pytest.mark.parametrize("field,value", [
    ("observed_at", "2099-01-01T00:00:00Z"),
    ("query_complete", False),
    ("scopes", []),
    ("gaps", ["missing_critical_scope"]),
])
def test_policy_reassessment_checks_complete_partition_snapshot_provenance(field, value):
    import json
    data = runtime_data()
    previous = json.loads(json.dumps(fake.stage(data).to_private_dict()))
    previous["private_detail"]["partitions"][0]["input"]["snapshot"][field] = value
    with pytest.raises(AssessmentError, match="policy_input_mismatch"):
        reassess_staging_policy(*data, previous)
