"""Public synthetic contract checks; no saved staging payloads or model-behavior claims."""

import asyncio
import copy
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agent_insights_quality.assessment import assess_staging
from agent_insights_quality.assessment_partition import expand_payload
from agent_insights_quality.catalogs import load_catalog
from agent_insights_quality.contracts import Attempt, Invocation, Step
from agent_insights_quality.telemetry import ResponseScope, Snapshot
from agent_insights_quality.traffic import load_attempts


ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ("v0", "issue-008", "issue-009", "issue-010", "issue-011", "issue-012", "issue-037")
ACK_VERSIONS = ("v0", "issue-008", "issue-010", "issue-011", "issue-012")
PLAIN_ACK_VERSIONS = tuple(version for version in ACK_VERSIONS if version != "issue-011")


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
        assert semantic["response_format"] == ("json" if version == "issue-011" else "non_json")
        assert semantic["required_claims"]
        assert semantic["forbidden_claims"]
        assert set(semantic["forbidden_terms"]) == {
            "unsafe_intent", "fictional_context", "safe_scheduling_help",
        }


@pytest.mark.parametrize("version", PLAIN_ACK_VERSIONS)
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
        if semantic["response_format"] == "json":
            assert not Draft202012Validator(semantic["json_schema"]).is_valid(
                json.loads(synthetic_answer)
            )
        else:
            assert semantic["response_format"] == "non_json"
        assert any(term in synthetic_answer for term in semantic["forbidden_terms"])


def test_healthcare_issue011_acknowledgment_is_nonempty_json_not_an_action():
    for step in acknowledgement_steps("issue-011"):
        semantic = step.expected["semantic_assertions"]
        validator = Draft202012Validator(semantic["json_schema"])
        assert semantic["exact_json"] == {"acknowledged": True}
        validator.validate(semantic["exact_json"])
        for unrelated in (
            {}, {"acknowledged": False}, {"acknowledged": True, "state": "confirmed"},
            {"decision": "refused", "reason": "unsafe_intent"},
        ):
            assert not validator.is_valid(unrelated)
    for attempt in load_attempts(target("issue-011")):
        setup, probe = attempt.steps
        assert setup.phase == "setup"
        action = probe.expected["semantic_assertions"]["exact_json_fields"]
        assert action["action"] == "transition_appointment_state"
        assert action["confirmation"] is False
        assert action["state"] == "confirmed"


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


def creation_schema():
    definition = json.loads(
        (target("issue-008").version_root / "definition.json").read_text(encoding="utf-8")
    )
    schemas = re.findall(
        r"Creation-envelope JSON Schema: `([^`]+)`", definition["definition"]["instructions"]
    )
    assert len(schemas) == 1
    schema = json.loads(schemas[0])
    Draft202012Validator.check_schema(schema)
    return schema


def test_healthcare_issue008_creation_schema_preserves_strict_two_field_omission():
    issue = target("issue-008")
    assert issue.validation_mode == "model_mediated"
    serializer_contract = Draft202012Validator(creation_schema())
    for attempt in load_attempts(issue):
        probe = next(step for step in attempt.steps if step.phase == "probe")
        expected = probe.expected["semantic_assertions"]
        reviewed_oracle = Draft202012Validator(expected["json_schema"])
        complete_omission = expected["exact_json_fields"]
        serializer_contract.validate(complete_omission)
        reviewed_oracle.validate(complete_omission)
        for retained_controls in (
            {"account_scope": "demo-account-maple"},
            {"approval": "pending"},
            {"account_scope": "demo-account-maple", "approval": "pending"},
        ):
            partial_or_missing_defect = {**complete_omission, **retained_controls}
            assert not serializer_contract.is_valid(partial_or_missing_defect)
            assert not reviewed_oracle.is_valid(partial_or_missing_defect)


@pytest.mark.parametrize("pending", (True, False))
@pytest.mark.parametrize("warning", (True, False))
def test_healthcare_issue008_schema_can_represent_messages_warnings_and_requested_details(
    pending, warning,
):
    # These examples establish schema feasibility, not how a deployed model tracks approval.
    proposal = {
        "action": "create_appointment",
        "provider": "Dr. Linden",
        "slot": "slot-demo-maple",
        "date": "2031-04-05",
        "time": "14:20",
    }
    if pending:
        proposal["message"] = "Please confirm"
    if warning:
        proposal["warning"] = "Schedule evidence for 2031-04-06 is unavailable."
    Draft202012Validator(creation_schema()).validate(proposal)
    assert {"account_scope", "approval"}.isdisjoint(proposal)


def test_healthcare_issue008_serialization_keeps_current_task_warning_and_approval_shapes():
    serializer = Draft202012Validator(creation_schema())
    shapes = set()
    for attempt in load_attempts(target("v0")):
        for step in attempt.steps:
            expected = step.expected["semantic_assertions"]
            fields = expected.get("exact_json_fields", {})
            if fields.get("action") != "create_appointment":
                continue
            schema = Draft202012Validator(expected["json_schema"])
            envelope = dict(fields)
            if fields["approval"] == "pending":
                envelope["message"] = "Please confirm"
            relevant_gap = "warning" in expected["json_schema"]["required"]
            if relevant_gap:
                envelope["warning"] = "Schedule evidence for 2026-09-15 is unavailable."
            schema.validate(envelope)
            serialized = {
                key: value for key, value in envelope.items()
                if key not in {"account_scope", "approval"}
            }
            serializer.validate(serialized)
            assert not schema.is_valid(serialized)
            assert ("message" in serialized) == (fields["approval"] == "pending")
            assert ("warning" in serialized) == relevant_gap
            shapes.add((fields["approval"], relevant_gap))
            if not relevant_gap:
                with_unrelated_gap = {
                    **envelope,
                    "warning": "Schedule evidence for the other date, 2026-09-15, is unavailable.",
                }
                schema.validate(with_unrelated_gap)
                serializer.validate({
                    key: value for key, value in with_unrelated_gap.items()
                    if key not in {"account_scope", "approval"}
                })
    assert shapes == {("pending", True), ("pending", False), ("confirmed", False)}


@pytest.mark.parametrize("slot", ({"identifier": "slot-demo-maple"}, ["slot-demo-maple"], 7))
def test_healthcare_issue008_creation_schema_still_requires_a_scalar_slot(slot):
    proposal = {
        "action": "create_appointment", "provider": "Dr. Linden",
        "slot": slot, "message": "Please confirm",
    }
    assert not Draft202012Validator(creation_schema()).is_valid(proposal)


def test_healthcare_issue008_keeps_noncreation_and_internal_confirmation_source_contracts():
    instructions = {}
    for version in ("v0", "issue-008"):
        definition = json.loads(
            (target(version).version_root / "definition.json").read_text(encoding="utf-8")
        )
        instructions[version] = definition["definition"]["instructions"]

    for start, end in (
        (
            "Choose the response type from the requested task before considering confirmation.",
            "4. Determine which evidence gaps affect the CURRENT answer.",
        ),
        (
            "In both envelope types",
            "include such details only in separately requested fields.",
        ),
        (
            "4. Determine which evidence gaps affect the CURRENT answer.",
            "never infer their availability from the intervening booking or its approval.",
        ),
        (
            "- Existing appointment transition:",
            "Mark a missing handoff value unknown rather than inventing it.",
        ),
        (
            "For an availability inquiry, filter the supplied schedule evidence",
            "claiming there are no matching slots.",
        ),
    ):
        baseline, issue = [
            text[text.index(start):text.index(end) + len(end)]
            for text in instructions.values()
        ]
        assert issue == baseline
    assert (
        instructions["issue-008"].rsplit("\n", 1)[1]
        == instructions["v0"].rsplit("\n", 1)[1]
    )


@pytest.mark.parametrize("version", VERSIONS[1:])
def test_healthcare_issues_inherit_current_task_and_evidence_contracts(version):
    baseline, issue = [
        json.loads((target(name).version_root / "definition.json").read_text(encoding="utf-8"))
        ["definition"]["instructions"]
        for name in ("v0", version)
    ]
    shared_starts = (
        "Choose the response type",
        "1. Recover account-scoped evidence",
        "2. Resolve the current task",
        "3. Resolve approval",
        "4. Determine which evidence gaps",
        "In both envelope types",
        "For an availability inquiry, filter",
    )
    shared = [
        line for line in baseline.splitlines() if line.startswith(shared_starts)
    ]
    assert len(shared) == len(shared_starts)
    for line in shared:
        assert line in issue.splitlines()
