"""The loaded attempts, not unused templates, are the executable authority."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from agent_insights_quality.catalogs import load_catalog
from agent_insights_quality.contracts import Attempt, Step, Target
from agent_insights_quality.results import UnitId
from agent_insights_quality.traffic import load_attempts, validate_traffic


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def document():
    return {
        "contract_version": "2.0",
        "agent_name": "synthetic-agent",
        "logical_version": "v0",
        "data_class": "synthetic_public_safe",
        "traffic_source": "endpoint_requests",
        "requests": [{
            "id": "request-one",
            "request": {
                "method": "POST", "path": "/responses",
                "headers": {"content-type": "application/json"},
                "body": {"input": [{
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Read synthetic account A."}],
                }]},
            },
            "expected": {"http_status": 200, "semantic_assertions": {"exact_text": "A"}},
        }],
        "attempts": [
            {"index": index, "setup_steps": [], "probe_steps": ["request-one"]}
            for index in range(1, 11)
        ],
    }


def validate(document, *, is_prompt=False):
    validate_traffic(document, is_prompt=is_prompt, schema_root=ROOT / "schemas")


def test_all_reviewed_attempts_resolve_without_losing_turns():
    catalog = load_catalog(ROOT)
    turns = 0
    for target in catalog.targets:
        attempts = load_attempts(target)
        assert len(attempts) == 10
        document = json.loads((target.version_root / "traffic.json").read_text(encoding="utf-8"))
        requests = {request["id"]: request for request in document["requests"]}
        assert {ref for case in document["attempts"]
                for ref in case["setup_steps"] + case["probe_steps"]} == requests.keys()
        for attempt, case in zip(attempts, document["attempts"], strict=True):
            assert isinstance(attempt, Attempt)
            expected_phases = ["setup"] * len(case["setup_steps"]) + ["probe"] * len(case["probe_steps"])
            assert [step.phase for step in attempt.steps] == expected_phases
            assert attempt.index == case["index"]
            assert attempt.parameters == case.get("parameters", {})
            assert len({step.step_id for step in attempt.steps}) == len(attempt.steps)
            for step, ref in zip(
                attempt.steps, case["setup_steps"] + case["probe_steps"], strict=True,
            ):
                assert isinstance(step, Step)
                assert step.body == requests[ref]["request"]["body"]
                assert step.expected == requests[ref]["expected"]
                assert "conversation" not in step.body and "previous_response_id" not in step.body
                assert "activation_gate" not in step.expected
                assert "defect_observed" not in step.expected
            turns += len(attempt.steps)
    assert turns == 834


@pytest.mark.parametrize("violation", [
    "legacy", "digest", "control", "unknown_ref", "duplicate_request",
    "unused_request", "nine_attempts", "repeated_index", "unordered",
    "empty_probe", "conversation", "previous_response", "self_label",
    "tool_fixture", "assistant_input",
])
def test_invalid_contract_is_not_silently_accepted(document, violation):
    first = document["requests"][0]
    if violation == "legacy":
        document["contract_version"] = "1.0"
    elif violation == "digest":
        document["execution_digest"] = "obsolete"
    elif violation == "control":
        document["v0_control_predicate"] = {}
    elif violation == "unknown_ref":
        document["attempts"][0]["probe_steps"] = ["unknown"]
    elif violation == "duplicate_request":
        document["requests"].append(copy.deepcopy(first))
    elif violation == "unused_request":
        extra = copy.deepcopy(first)
        extra["id"] = "unused"
        document["requests"].append(extra)
    elif violation == "nine_attempts":
        document["attempts"].pop()
    elif violation == "repeated_index":
        document["attempts"][1]["index"] = 1
    elif violation == "unordered":
        document["attempts"].reverse()
    elif violation == "empty_probe":
        document["attempts"][0]["probe_steps"] = []
    elif violation == "conversation":
        first["request"]["body"]["conversation"] = {"id": "$validation_conversation"}
    elif violation == "previous_response":
        first["request"]["body"]["previous_response_id"] = "not-runtime"
    elif violation == "self_label":
        first["expected"]["defect_observed"] = True
    elif violation == "tool_fixture":
        first["tool_fixtures"] = {}
    else:
        first["request"]["body"]["input"][0]["role"] = "assistant"
    with pytest.raises((ValueError, ValidationError)):
        validate(document)


def test_prompt_trace_contract_cannot_request_tool_calls(document):
    document["requests"][0]["expected"]["trace_assertions"] = [{
        "name": "ordered_calls", "kind": "operation_sequence",
        "operations": ["invoke_agent", "execute_tool"],
    }]
    validate(document)
    with pytest.raises(ValidationError):
        validate(document, is_prompt=True)


def test_loader_preserves_repeated_turns_and_isolates_mutations(document, tmp_path):
    version = tmp_path / "agents" / "synthetic-agent" / "v0"
    version.mkdir(parents=True)
    schema_root = tmp_path / "schemas"
    schema_root.mkdir()
    for name in ("traffic.schema.json", "prompt-traffic.schema.json"):
        (schema_root / name).write_bytes((ROOT / "schemas" / name).read_bytes())
    document["attempts"][0]["setup_steps"] = ["request-one"]
    document["attempts"][0]["probe_steps"] = ["request-one", "request-one"]
    path = version / "traffic.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    target = Target(UnitId("synthetic-agent", "v0"), "prompt", "baseline", version, version, {})
    attempts = load_attempts(target)
    assert [step.phase for step in attempts[0].steps] == ["setup", "probe", "probe"]
    assert len({step.step_id for step in attempts[0].steps}) == 3
    attempts[0].steps[0].body["input"][0]["content"][0]["text"] = "Mutated locally."
    assert attempts[0].steps[1].body["input"][0]["content"][0]["text"] == "Read synthetic account A."
    assert attempts[1].steps[0].body["input"][0]["content"][0]["text"] == "Read synthetic account A."
    assert load_attempts(target)[0].steps[0].body["input"][0]["content"][0]["text"] == "Read synthetic account A."
    document["logical_version"] = "issue-001"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        load_attempts(target)
