"""Versioned staging aggregation; Daily readiness and scoring are separate."""

from copy import deepcopy
from dataclasses import replace

import pytest

from agent_insights_quality.assessment import AssessmentError, reassess_staging_policy
from agent_insights_quality.results import COVERAGE_POLICY
from agent_insights_quality.settings import RuntimeSettings
from agent_insights_quality.staging_policy import STAGING_POLICY, StagingPolicy
import test_assessment as fake
import test_assessment_partitions as partitioned


@pytest.mark.parametrize("mode", ["baseline", "deterministic", "model_mediated"])
@pytest.mark.parametrize("observations,insufficient,status", [
    (8, (), "PASS"),
    (7, (), "FAIL"),
    (6, (7, 8, 9, 10), "INCOMPLETE"),
    (8, (9, 10), "PASS"),
])
def test_eight_required_with_all_ten_judged(mode, observations, insufficient, status):
    data = fake.evidence(mode, missing=insufficient)
    sol = partitioned.StageSol(observations=observations)
    result = fake.stage(data, sol)
    assert result.status == status
    assert result.passing_attempts == observations
    assert len(result.judgments) == 10
    assert [item["index"] for item in result.judgments] == list(range(1, 11))
    assert result.to_private_dict()["minimum_required"] == STAGING_POLICY.minimum_required == 8
    assert result.to_private_dict()["policy_version"] == STAGING_POLICY.version
    if status == "FAIL":
        assert result.reasons == ("observation_threshold_not_met",)
        assert not any(item["contract_violation"] for item in result.judgments)
    elif status == "INCOMPLETE":
        assert result.reasons == ("insufficient_evidence",)


@pytest.mark.parametrize("mode", ["baseline", "deterministic"])
def test_eight_observations_never_outvote_proven_contract_violation(mode):
    result = fake.stage(
        fake.evidence(mode),
        partitioned.StageSol(observations=8, violation=10),
    )
    assert result.passing_attempts == 8
    assert result.status == "FAIL" and result.reasons == ("proven_contract_violation",)
    assert len(result.judgments) == 10


@pytest.mark.parametrize("mode", ["baseline", "deterministic"])
def test_proven_strict_violation_wins_over_an_oversized_unjudged_group(mode):
    data = partitioned.independent(mode)
    limit = partitioned.single_group_limit(partitioned.raw_payload(data))
    records = list(data[3].records)
    records[0] = {**records[0], "raw": {**records[0]["raw"], "synthetic_blob": "x" * limit * 2}}
    data = (*data[:3], replace(data[3], records=tuple(records)))
    result = fake.stage(data, partitioned.StageSol(violation=10), max_payload_bytes=limit)
    assert result.status == "FAIL" and result.passing_attempts == 8
    assert result.reasons == ("proven_contract_violation",)
    assert result.private_detail["partitions"][0]["status"] == "oversized"


def test_model_mediated_permitted_nonobservations_remain_nonviolations():
    result = fake.stage(fake.evidence(), partitioned.StageSol(observations=8))
    assert result.status == "PASS"
    assert [item["observed"] for item in result.judgments] == [True] * 8 + [False] * 2
    assert not any(item["contract_violation"] for item in result.judgments)


@pytest.mark.parametrize("version,minimum", [("", 8), ("invalid version", 8), ("policy-v1", True),
                                           ("policy-v1", 0), ("policy-v1", 11)])
def test_policy_is_explicit_bounded_configuration_not_runtime_fallback(version, minimum):
    with pytest.raises(ValueError):
        StagingPolicy(version, minimum)


def test_daily_readiness_and_partial_coverage_are_unchanged():
    assert RuntimeSettings().readiness_attempts == 6
    assert RuntimeSettings().attempts == 10
    assert COVERAGE_POLICY.max_excluded_units == 2
    data = fake.evidence(missing=(7, 8, 9, 10))
    daily = fake.daily(data)
    assert not daily.unit_result.exclusion_reasons
    assert fake.aggregate(data, daily).score == 100
    assert fake.stage(data).status == "INCOMPLETE"


@pytest.mark.parametrize("versioned", [False, True])
def test_reaggregate_retained_six_pass_without_model_or_changing_raw_input(versioned):
    data = fake.evidence(missing=(7, 8, 9, 10))
    prior = fake.stage(data, policy=StagingPolicy("synthetic-six-v1", 6)).to_private_dict()
    assert prior["status"] == "PASS"
    if not versioned:
        prior.pop("policy_version")
        prior.pop("minimum_required")
    original = deepcopy(prior)
    result = reassess_staging_policy(*data, prior)
    assert result.status == "INCOMPLETE" and result.passing_attempts == 6
    assert result.minimum_required == 8 and result.policy_version == STAGING_POLICY.version
    assert prior == original
    assert result.private_detail["input"] == prior["private_detail"]["input"]
    assert result.private_detail["output"] == prior["private_detail"]["output"]
    assert result.private_detail["partitions"] == prior["private_detail"]["partitions"]
    assert result.judgments == prior["judgments"]
    assert result.private_detail["policy_reassessment"]["previous_minimum_required"] == (6 if versioned else None)


def test_reaggregate_all_partitions_and_ignore_previous_status_or_counts():
    data = partitioned.independent()
    prior = fake.stage(
        data, partitioned.StageSol(observations=7),
        max_payload_bytes=partitioned.single_group_limit(partitioned.raw_payload(data)),
        policy=StagingPolicy("synthetic-six-v1", 6),
    ).to_private_dict()
    assert len(prior["private_detail"]["partitions"]) == 10
    prior.update(status="PASS", passing_attempts=10)
    result = reassess_staging_policy(*data, prior)
    assert result.status == "FAIL" and result.passing_attempts == 7
    assert result.reasons == ("observation_threshold_not_met",)


@pytest.mark.parametrize("mutation", [
    lambda prior: prior["private_detail"]["output"]["attempts"].pop(),
    lambda prior: prior["private_detail"]["output"]["attempts"][-1].update(index=1),
    lambda prior: prior["private_detail"]["output"]["attempts"][0].update(sufficient=False),
    lambda prior: prior["private_detail"]["output"]["attempts"][0]["citations"][0].update(refs=["unknown-ref"]),
    lambda prior: prior["private_detail"]["output"]["attempts"][0]["citations"][0].update(refs=["endpoint-01-02"]),
    lambda prior: prior["private_detail"]["output"]["attempts"][0]["citations"][0].update(attempt=2),
    lambda prior: prior["judgments"][0].update(observed=False),
    lambda prior: prior["private_detail"]["partitions"][0].update(status="pending"),
    lambda prior: prior["private_detail"]["partitions"][0]["output"]["attempts"].pop(),
    lambda prior: prior["private_detail"]["input"]["snapshot"].update(query_complete=False),
])
def test_policy_reuse_revalidates_every_judgment_citation_and_input(mutation):
    data = fake.evidence()
    prior = fake.stage(data).to_private_dict()
    mutation(prior)
    with pytest.raises(AssessmentError):
        reassess_staging_policy(*data, prior)


def test_changed_expectations_cannot_borrow_prior_judgment_authority():
    data = fake.evidence()
    prior = fake.stage(data).to_private_dict()
    changed = replace(data[0], expectation={"root_cause": "A different reviewed defect"})
    with pytest.raises(AssessmentError, match="assessment_policy_input_mismatch"):
        reassess_staging_policy(changed, *data[1:], prior)


def test_unjudged_oversized_partitions_are_not_complete_retained_model_judgments():
    data = fake.evidence()
    prior = fake.stage(data, max_payload_bytes=1).to_private_dict()
    with pytest.raises(AssessmentError, match="assessment_policy_judgments_incomplete"):
        reassess_staging_policy(*data, prior)
