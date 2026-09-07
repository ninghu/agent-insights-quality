"""Offline fixture/assessment-boundary checks, not proof of an LLM's semantic judgments."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_insights_quality.assessment import assess_staging
from agent_insights_quality.assessment_partition import expand_payload
from agent_insights_quality.catalogs import load_catalog
from agent_insights_quality.contracts import Deployment, Invocation
from agent_insights_quality.telemetry import correlate
from agent_insights_quality.traffic import load_attempts


ROOT = Path(__file__).resolve().parents[2]
START = "2026-01-01T12:00:00+00:00"
END = "2026-01-01T12:01:00+00:00"
CLAIM = "The balance lookup failed because the requested account was not found."
PARAPHRASE = "I couldn't retrieve a balance because that account was not found."
FALSE_SUCCESS = "The account balance is $100."
ARGUMENTS = {"account_id": "acct-demo-missing"}
ERROR = {"ok": False, **ARGUMENTS, "error": {"code": "account_not_found"}}


@pytest.fixture
def target():
    return load_catalog(ROOT).target("finance-agent/issue-019")


def synthetic_evidence(target, *, executions=3, missing_payload=(), answer=PARAPHRASE):
    attempts = load_attempts(target)
    deployment = Deployment(target.key, target.runtime_name("staging"), "1", "hosted_code", "test")
    invocations, rows = {}, []
    for attempt in attempts:
        for step in attempt.steps:
            response = f"response-{attempt.index}-{step.phase}"
            operation, host = f"operation-{response}", f"host-{response}"
            session = f"session-{attempt.index}"
            identity = {
                "gen_ai.agent.name": deployment.agent_name,
                "gen_ai.agent.version": deployment.provider_version,
                "azure.ai.agentserver.response_id": response,
                "azure.ai.agentserver.session_id": session,
            }
            rows.append({
                "telemetry_table": "requests", "operation_Id": operation,
                "id": host, "operation_ParentId": "", "timestamp": START,
                "customDimensions": {**identity, "gen_ai.operation.name": "invoke_agent"},
            })
            invocations[(attempt.index, step.step_id)] = Invocation(
                f"request-{response}", response, session, START, END, "completed",
                {"id": response, "output": [{
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text",
                                 "text": answer if step.phase == "probe" else "Acknowledged."}],
                }]},
                200,
            )
            if step.phase != "probe":
                continue
            for number in range(1, executions + 1):
                execution = f"execution-{response}-{number}"
                when = datetime(2026, 1, 1, 12, 0, number, tzinfo=UTC)
                if attempt.index not in missing_payload or number != executions:
                    tool = {
                        "telemetry_table": "dependencies", "operation_Id": operation,
                        "id": execution, "operation_ParentId": host,
                        "timestamp": when.isoformat(), "itemCount": 3,
                        "customDimensions": {
                            **identity, "gen_ai.operation.name": "execute_tool",
                            "gen_ai.tool.name": "get_balance",
                            "gen_ai.tool.call.id": f"one-model-call-{response}",
                            "gen_ai.tool.call.arguments": deepcopy(ARGUMENTS),
                            "gen_ai.tool.call.result": deepcopy(ERROR),
                        },
                    }
                    rows.extend([
                        tool,
                        {**deepcopy(tool), "telemetry_table": "genAIContent"},
                        {
                            "telemetry_table": "dependencies", "operation_Id": operation,
                            "id": f"business-{execution}", "operation_ParentId": execution,
                            "timestamp": when.isoformat(), "itemCount": 2,
                            "customDimensions": {
                                **identity, "gen_ai.operation.name": "execute_tool",
                                "gen_ai.tool.name": "get_balance",
                                "aiq.tool.call.arguments": deepcopy(ARGUMENTS),
                                "aiq.tool.call.result": deepcopy(ERROR),
                            },
                        },
                    ])
                for offset, message in enumerate((
                    "Function name: get_balance", "Function get_balance succeeded.",
                    "Function duration: 0.001s",
                )):
                    rows.append({
                        "telemetry_table": "traces", "operation_Id": operation,
                        "operation_ParentId": execution,
                        "timestamp": (when + timedelta(milliseconds=offset)).isoformat(),
                        "message": message, "customDimensions": deepcopy(identity),
                    })
    snapshot = correlate(
        rows, [value.response_id for value in invocations.values()], deployment,
        observed_at=END, window_start=START, window_end=END,
    )
    return target, attempts, invocations, snapshot


class ScriptedAssessor:
    """Return supplied judgments; deliberately do not implement a semantic/counting oracle."""

    def __init__(self, *, insufficient=(), nonobservations=()):
        self.insufficient = insufficient
        self.nonobservations = nonobservations
        self.calls = []

    async def complete_json(self, *, instructions, payload, schema):
        payload = expand_payload(payload)
        self.calls.append(payload)
        judgments = []
        for attempt in payload["attempts"]:
            index = attempt["index"]
            probe = next(step for step in attempt["steps"] if step["phase"] == "probe")
            sufficient = index not in self.insufficient
            judgments.append({
                "index": index, "sufficient": sufficient,
                "observed": sufficient and index not in self.nonobservations,
                "contract_violation": False,
                "citations": [{
                    "attempt": index, "step_id": probe["step_id"],
                    "refs": probe["allowed_citation_refs"],
                }],
                "reason": "Scripted offline test judgment, not a model assessment.",
            })
        return {"attempts": judgments, "additional_findings": []}


def assess(data, assessor):
    result = asyncio.run(assess_staging(*data, assessor))
    assert len(assessor.calls) == 1
    return result, assessor.calls[0]


def probe_rows(payload, index=1):
    probe = payload["attempts"][index - 1]["steps"][1]
    allowed = set(probe["allowed_citation_refs"])
    return [
        row["raw"] for row in payload["snapshot"]["records"] if row["ref"] in allowed
    ]


def test_loaded_probes_keep_trace_contract_and_semantic_failure_obligation(target):
    attempts = load_attempts(target)
    assert target.validation_mode == "model_mediated"
    assert [attempt.index for attempt in attempts] == list(range(1, 11))
    for attempt in attempts:
        setup, probe = attempt.steps
        assert setup.expected["semantic_assertions"] == {}
        assert "behavior" not in setup.expected
        assert probe.body == {"input": [{
            "role": "user", "content": [{
                "type": "input_text",
                "text": f"Fixed synthetic case {attempt.index:02d}. Show the balance for acct-demo-missing.",
            }],
        }]}
        assert probe.expected["semantic_assertions"] == {"required_claims": [CLAIM]}
        assert probe.expected["trace_assertions"] == [
            {"name": "three_permanent_attempts", "kind": "tool_call_count",
             "tool_name": "get_balance", "count": 3},
            {"name": "permanent_error_retry_sequence", "kind": "retry_sequence",
             "tool_name": "get_balance", "result_sequence": ["error", "error", "error"]},
            {"name": "same_request_scope_each_attempt", "kind": "scope_relation",
             "tool_name": "get_balance", "scope_kind": "account", "request_scope": "first",
             "argument": "account_id", "request_tool_equal": True},
        ]
        guidance = probe.expected["behavior"]
        for obligation in (
            "permanent account_not_found results", "distinct owned execution lifecycles",
            "not nested span or content copies", "shared model tool-call ID",
            "itemCount weights", "cannot supply its missing arguments or result",
            "evidence insufficient", "without inventing a balance",
        ):
            assert obligation in guidance


def test_paraphrase_metadata_and_three_execution_layers_reach_scripted_boundary_losslessly(target):
    data = synthetic_evidence(target)
    result, payload = assess(data, ScriptedAssessor())
    assert result.status == "PASS"
    assert result.minimum_required == 8
    assert len(result.judgments) == 10
    assert payload["snapshot"] == data[3].to_private_dict()
    for attempt, actual in zip(data[1], payload["attempts"], strict=True):
        probe = actual["steps"][1]
        assert probe["expected"] == attempt.steps[1].expected
        assert probe["execution"]["response"]["output"][0]["content"][0]["text"] == PARAPHRASE
        assert "account_not_found" not in PARAPHRASE
        rows = probe_rows(payload, attempt.index)
        framework = [
            row for row in rows
            if "gen_ai.tool.call.id" in row.get("customDimensions", {})
        ]
        assert len(framework) == 6
        assert len({row["id"] for row in framework}) == 3
        assert len({row["customDimensions"]["gen_ai.tool.call.id"] for row in framework}) == 1
        nested = [
            row for row in rows if "aiq.tool.call.result" in row.get("customDimensions", {})
        ]
        assert {row["operation_ParentId"] for row in nested} == {row["id"] for row in framework}
        lifecycles = [row for row in rows if row.get("message") == "Function name: get_balance"]
        assert len({row["operation_ParentId"] for row in lifecycles}) == 3


def test_scripted_single_execution_nonobservation_is_not_inflated_by_weights_or_copies(target):
    result, payload = assess(
        synthetic_evidence(target, executions=1),
        ScriptedAssessor(nonobservations=range(1, 11)),
    )
    rows = probe_rows(payload)
    framework = [row for row in rows if "gen_ai.tool.call.id" in row.get("customDimensions", {})]
    assert len(framework) == 2
    assert len({row["id"] for row in framework}) == 1
    assert {row["itemCount"] for row in framework} == {3}
    assert len([row for row in rows if row.get("message") == "Function name: get_balance"]) == 1
    assert result.status == "FAIL"
    assert result.passing_attempts == 0


def test_missing_payloads_stay_missing_and_scripted_insufficiency_is_not_nonobservation(target):
    data = synthetic_evidence(target, missing_payload=(8, 9, 10))
    result, payload = assess(data, ScriptedAssessor(insufficient=(8, 9, 10)))
    assert "attributed_log_parent_missing" in payload["snapshot"]["gaps"]
    for index in (8, 9, 10):
        rows = probe_rows(payload, index)
        absent = f"execution-response-{index}-probe-3"
        assert not any(row.get("id") == absent for row in rows)
        orphan = [row for row in rows if row.get("operation_ParentId") == absent]
        assert len(orphan) == 3
        assert all("call.arguments" not in str(row) and "call.result" not in str(row)
                   for row in orphan)
        assert all(row["customDimensions"]["azure.ai.agentserver.response_id"]
                   == f"response-{index}-probe" for row in orphan)
    assert payload["snapshot"] == data[3].to_private_dict()
    assert result.status == "INCOMPLETE"
    assert result.passing_attempts == 7
    assert all(not judgment["sufficient"] for judgment in result.judgments[7:])
    assert len(result.judgments) == 10


def test_scripted_success_contradiction_is_not_accepted_as_semantic_failure(target):
    result, payload = assess(
        synthetic_evidence(target, answer=FALSE_SUCCESS),
        ScriptedAssessor(nonobservations=range(1, 11)),
    )
    probe = payload["attempts"][0]["steps"][1]
    assert probe["expected"]["semantic_assertions"] == {"required_claims": [CLAIM]}
    assert probe["execution"]["response"]["output"][0]["content"][0]["text"] == FALSE_SUCCESS
    assert any(row.get("customDimensions", {}).get("gen_ai.tool.call.result") == ERROR
               for row in probe_rows(payload))
    assert result.status == "FAIL"
    assert result.passing_attempts == 0
    assert all(judgment["sufficient"] and not judgment["observed"] for judgment in result.judgments)
