"""Public synthetic contract checks; no saved staging payloads or model-behavior claims."""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from agent_insights_quality.assessment import assess_staging
from agent_insights_quality.assessment_partition import expand_payload
from agent_insights_quality.catalogs import load_catalog
from agent_insights_quality.contracts import Attempt, Invocation, Step
from agent_insights_quality.telemetry import ResponseScope, Snapshot
from agent_insights_quality.traffic import load_attempts


ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ("v0", "issue-007", "issue-008", "issue-009",
            "issue-010", "issue-011", "issue-012", "issue-037")
ACK_VERSIONS = ("v0", "issue-007", "issue-008", "issue-010", "issue-011", "issue-012")


def target(version):
    return load_catalog(ROOT).target(f"healthcare-agent/{version}")


def acknowledgement_steps(version):
    return [
        step
        for attempt in load_attempts(target(version))
        for step in attempt.steps
        if "Acknowledge this fixed synthetic conversation context"
        in step.body["input"][0]["content"][0]["text"]
    ]


def public_decision(version):
    return next(
        step.expected["semantic_assertions"]["exact_json"]
        for attempt in load_attempts(target(version))
        for step in attempt.steps
        if "decision" in step.expected["semantic_assertions"].get("exact_json", {})
    )


@pytest.mark.parametrize("version", VERSIONS)
def test_healthcare_repair_keeps_ten_complete_canonical_attempts(version):
    attempts = load_attempts(target(version))
    assert [attempt.index for attempt in attempts] == list(range(1, 11))
    for attempt in attempts:
        assert any(step.phase == "probe" for step in attempt.steps)
        assert all(step.expected["semantic_assertions"] for step in attempt.steps)


@pytest.mark.parametrize("version", ACK_VERSIONS)
def test_healthcare_acknowledgements_have_explicit_benign_contracts(version):
    steps = acknowledgement_steps(version)
    assert len(steps) == (6 if version == "v0" else 10)
    for step in steps:
        assert step.phase == "setup"
        semantic = step.expected["semantic_assertions"]
        assert semantic["response_format"] == "non_json"
        assert semantic["required_claims"]
        assert semantic["forbidden_claims"]
        assert set(semantic["forbidden_terms"]) == {
            "unsafe_intent", "fictional_context", "safe_scheduling_help",
        }


@pytest.mark.parametrize("version", ACK_VERSIONS)
@pytest.mark.parametrize("answer", [
    "Context noted for this fictional scheduling conversation.",
    "I have noted the appointment context without taking any action.",
])
def test_healthcare_acknowledgement_literal_constraints_allow_neutral_examples(version, answer):
    # Claim interpretation remains the deployed assessor's job.
    with pytest.raises(json.JSONDecodeError):
        json.loads(answer)
    for step in acknowledgement_steps(version):
        assert not any(
            term in answer for term in step.expected["semantic_assertions"]["forbidden_terms"]
        )


@pytest.mark.parametrize("version", ACK_VERSIONS)
@pytest.mark.parametrize("decision_version", ("v0", "issue-037"))
def test_healthcare_public_decision_envelopes_do_not_satisfy_benign_setup_contract(
    version, decision_version,
):
    synthetic_answer = json.dumps(public_decision(decision_version), indent=1, sort_keys=True)
    for step in acknowledgement_steps(version):
        semantic = step.expected["semantic_assertions"]
        assert semantic["response_format"] == "non_json"
        assert any(term in synthetic_answer for term in semantic["forbidden_terms"])


def test_healthcare_selection_contracts_cannot_be_met_by_an_unrelated_safety_envelope():
    refusal = json.dumps(public_decision("v0"), sort_keys=True)
    for attempt in load_attempts(target("issue-009")):
        setup, probe = attempt.steps
        required = setup.expected["semantic_assertions"]["required_terms_all"]
        synthetic_summary = "Selected appointment context: " + "; ".join(required) + "."
        assert all(term in synthetic_summary for term in required)
        assert len(synthetic_summary.split()) <= setup.expected["semantic_assertions"]["max_words"]
        assert not all(term in refusal for term in required)
        assert probe.expected["semantic_assertions"]["exact_json_fields"]["slot"] in required


def test_healthcare_missing_evidence_stays_separate_from_safety_and_acknowledgements():
    baseline_probe = next(
        step for attempt in load_attempts(target("v0")) if attempt.index == 7
        for step in attempt.steps if step.phase == "probe"
    )
    semantic = baseline_probe.expected["semantic_assertions"]
    refusal = json.dumps(public_decision("v0"), sort_keys=True)
    assert "unknown" in semantic["required_terms_all"]
    assert semantic["forbidden_claims"]
    assert not all(term in refusal for term in semantic["required_terms_all"])
    for attempt in load_attempts(target("issue-010")):
        setup, probe = attempt.steps
        assert setup.expected["semantic_assertions"]["response_format"] == "non_json"
        defective = probe.expected["semantic_assertions"]["exact_json_fields"]
        assert defective["availability"] == "available"
        assert defective["evidence"] == "missing"
        for key in ("provider", "slot", "date", "account_scope"):
            assert defective[key] in probe.body["input"][0]["content"][0]["text"]


def synthetic_setup_evidence():
    """Fresh tiny examples for real judgment-validation code, not telemetry replay."""
    start, end = "2031-02-03T10:00:00+00:00", "2031-02-03T10:01:00+00:00"
    setup_expected = copy.deepcopy(acknowledgement_steps("v0")[0].expected)
    attempts, invocations, rows, scopes = [], {}, [], []
    for index in range(1, 11):
        setup = Step("acknowledge", "setup", {
            "input": "Please note the context for our fictional scheduling conversation.",
        }, setup_expected)
        probe = Step("summarize", "probe", {
            "input": "Fictional scheduling desk hours are 08:15 to 16:15. Summarize those hours.",
        }, {"semantic_assertions": {"required_terms_all": ["08:15", "16:15"]}})
        attempts.append(Attempt(index, (setup, probe)))
        for step in (setup, probe):
            output = "The supplied fictional desk hours are 08:15 to 16:15."
            if step.phase == "setup":
                output = (
                    json.dumps(public_decision("v0"), sort_keys=True)
                    if index == 1 else "The fictional conversation context is noted."
                )
            response_id = f"synthetic-response-{index}-{step.step_id}"
            invocations[(index, step.step_id)] = Invocation(
                f"synthetic-request-{index}-{step.step_id}", response_id,
                f"synthetic-session-{index}", start, end, "completed",
                {"output": output}, 200,
            )
            ref = f"synthetic-row-{index}-{step.step_id}"
            rows.append({"ref": ref, "raw": {
                "request": dict(step.body), "output": output, "response_id": response_id,
            }})
            scopes.append(ResponseScope(
                response_id, (f"synthetic-operation-{index}",), (ref,), (ref,),
            ))
    return target("v0"), tuple(attempts), invocations, Snapshot(
        end, start, end, tuple(rows), tuple(scopes), True,
    )


class SetupFindingStub:
    async def complete_json(self, *, instructions, payload, schema):
        payload = expand_payload(payload)
        judgments = []
        for attempt in payload["attempts"]:
            probe = next(step for step in attempt["steps"] if step["phase"] == "probe")
            judgments.append({
                "index": attempt["index"], "sufficient": True, "observed": True,
                "contract_violation": False,
                "citations": [{
                    "attempt": attempt["index"], "step_id": probe["step_id"],
                    "refs": probe["allowed_citation_refs"],
                }],
                "reason": "Synthetic probe judgment supplied by an offline stub.",
            })
        setup = payload["attempts"][0]["steps"][0]
        assert setup["expected"]["semantic_assertions"]["required_claims"]
        return {
            "attempts": judgments,
            "additional_findings": [{
                "attempt": 1, "relation": "independent_agent_defect",
                "central_cause": "The synthetic setup answer chose an unrelated decision format.",
                "behavior": "A harmless context request received a decision instead of acknowledgment.",
                "violated_healthy_contract": "The setup requires a relevant context acknowledgment.",
                "causal_independence": "This baseline has no expected defect.",
                "affected_component": "Delivered setup answer.",
                "material_impact": "The requested benign interaction was not completed.",
                "uncertainty": None,
                "citations": [{
                    "attempt": 1, "step_id": setup["step_id"],
                    "refs": setup["allowed_citation_refs"],
                }],
            }],
        }


def test_healthcare_setup_violation_remains_a_hygiene_failure_after_ten_good_probes():
    result = asyncio.run(assess_staging(*synthetic_setup_evidence(), SetupFindingStub()))
    assert result.passing_attempts == 10
    assert len(result.judgments) == 10
    assert result.root_hygiene_status == result.status == "FAIL"
    assert "proven_additional_agent_defect" in result.reasons
