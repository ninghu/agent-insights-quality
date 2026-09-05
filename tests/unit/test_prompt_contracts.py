"""Offline asset/fixture checks only; Prompt behavior requires deployed staging evidence."""

import copy
import json
import re
from datetime import date
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[2]
PROMPT_ROOTS = [
    ROOT / "agents" / "weather-agent",
    ROOT / "agents" / "healthcare-agent",
]
AUTHORITIES = [
    pytest.param(path.parent, id=str(path.parent.relative_to(ROOT / "agents")))
    for root in PROMPT_ROOTS
    for path in sorted(root.rglob("definition.json"))
]


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def traffic(agent, version="v0"):
    root = ROOT / "agents" / agent
    return read_json(
        (root / "v0" if version == "v0" else root / "issues" / version) / "traffic.json"
    )


def text(step):
    return step["request"]["body"]["input"][0]["content"][0]["text"]


def assertions(step):
    return step["expected"]["semantic_assertions"]


def requests_by_id(document):
    return {step["id"]: step for step in document["requests"]}


def dictionaries(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child)
    elif isinstance(value, list):
        for child in value:
            yield from dictionaries(child)


def measurements(step):
    return {
        key: float(value)
        for key, value in re.findall(
            r"\b(temperature(?:_celsius|_fahrenheit)?|high|low)=(-?\d+(?:\.\d+)?)",
            text(step),
        )
    }


@pytest.mark.parametrize("name", ["agent", "issue"])
def test_catalogs_match_their_schemas(name):
    schema = read_json(ROOT / "schemas" / f"{name}-catalog.schema.json")
    Draft202012Validator.check_schema(schema)
    catalog = yaml.safe_load(
        (ROOT / "catalogs" / f"{name.upper()}_CATALOG.yaml").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(catalog)


def test_validation_mode_is_reviewed_catalog_data():
    schema = read_json(ROOT / "schemas" / "issue-catalog.schema.json")
    catalog = yaml.safe_load(
        (ROOT / "catalogs" / "ISSUE_CATALOG.yaml").read_text(encoding="utf-8")
    )
    item_schema = schema["properties"]["issues"]["items"]
    validator = Draft202012Validator(item_schema)
    for mode in ("deterministic", "model_mediated"):
        example = copy.deepcopy(catalog["issues"][0])
        example["validation_mode"] = mode
        validator.validate(example)
    example["validation_mode"] = "infer_from_runtime"
    assert not validator.is_valid(example)


@pytest.mark.parametrize("directory", AUTHORITIES)
def test_prompt_assets_are_pure_and_schema_valid(directory):
    for filename, schema_name in (
        ("definition.json", "prompt-definition.schema.json"),
        ("traffic.json", "prompt-traffic.schema.json"),
    ):
        document = read_json(directory / filename)
        schema = read_json(ROOT / "schemas" / schema_name)
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(document)
        for item in dictionaries(document):
            assert not {
                "tools", "tool_fixtures", "tool_choice", "function_call", "function_call_output"
            }.intersection(item)
            assert "execute_tool" not in item.get("required_operations", [])
    definition = read_json(directory / "definition.json")
    document = read_json(directory / "traffic.json")
    assert definition["metadata"]["logical_version"] == document["logical_version"]
    assert definition["name"] == document["agent_name"]


@pytest.mark.parametrize("location", ["top_level", "definition"])
def test_prompt_definition_schema_rejects_tools(location):
    document = read_json(PROMPT_ROOTS[0] / "v0" / "definition.json")
    target = document if location == "top_level" else document["definition"]
    target["tools"] = [{"type": "function", "name": "synthetic_lookup"}]
    schema = read_json(ROOT / "schemas" / "prompt-definition.schema.json")
    assert not Draft202012Validator(schema).is_valid(document)


@pytest.mark.parametrize(
    "violation", ["fixture", "tool_operation", "trace_operation", "assistant_input"]
)
def test_prompt_traffic_schema_rejects_non_prompt_requests(violation):
    document = traffic("weather-agent")
    request = document["requests"][0]
    if violation == "fixture":
        request["tool_fixtures"] = {"synthetic_lookup": {}}
    elif violation == "tool_operation":
        request["expected"]["required_operations"] = ["invoke_agent", "execute_tool"]
    elif violation == "trace_operation":
        request["expected"]["trace_assertions"] = [
            {"name": "unexpected_tool", "kind": "operation_sequence",
             "operations": ["invoke_agent", "execute_tool"]}
        ]
    else:
        request["request"]["body"]["input"][0]["role"] = "assistant"
    schema = read_json(ROOT / "schemas" / "prompt-traffic.schema.json")
    assert not Draft202012Validator(schema).is_valid(document)


@pytest.mark.parametrize("directory", AUTHORITIES)
def test_scenarios_preserve_reviewed_requests_and_expectations(directory):
    document = read_json(directory / "traffic.json")
    sources = requests_by_id(document)
    assert len(sources) == len(document["requests"])
    referenced = set()
    conversation_groups = set()
    step_ids = set()
    issues = yaml.safe_load(
        (ROOT / "catalogs" / "ISSUE_CATALOG.yaml").read_text(encoding="utf-8")
    )["issues"]
    modes = {issue["id"]: issue["validation_mode"] for issue in issues}
    for scenario in document["validation_rules"]["scenarios"]:
        expected_mode = (
            "baseline" if document["logical_version"] == "v0"
            else modes[document["logical_version"]]
        )
        assert scenario["validation_mode"] == expected_mode
        assert scenario["fixtures"] == []
        assert scenario["n"] == len(scenario["attempts"]) == 10
        assert scenario["k"] == 6
        assert [attempt["index"] for attempt in scenario["attempts"]] == list(range(1, 11))
        for attempt in scenario["attempts"]:
            group = attempt["conversation_group"]
            assert group not in conversation_groups
            conversation_groups.add(group)
            ids = attempt["parameters"]["source_request_ids"]
            referenced.update(ids)
            count = len(attempt["probe_steps"])
            assert count > 0
            paired_steps = attempt["probe_steps"]
            if len(ids) > count:
                paired_steps = attempt["setup_steps"] + paired_steps
            assert len(paired_steps) == len(ids)
            for source_id, step in zip(ids, paired_steps, strict=True):
                source = sources[source_id]
                expected_request = copy.deepcopy(source["request"])
                expected_request["body"]["conversation"]["id"] = "$validation_conversation"
                expected_request["body"]["input"][0]["content"][0]["text"] = (
                    f"Fixed synthetic case {attempt['index']:02d}. {text(source)}"
                )
                assert step["request"] == expected_request
                assert assertions(step) == assertions(source)
                assert assertions(step)
                assert step["expected"]["http_status"] == source["expected"]["http_status"]
            for step in attempt["setup_steps"] + attempt["probe_steps"]:
                assert step["id"] not in step_ids
                step_ids.add(step["id"])
    assert referenced == set(sources)


@pytest.mark.parametrize("directory", AUTHORITIES)
def test_expected_json_values_satisfy_their_response_schema(directory):
    document = read_json(directory / "traffic.json")
    for item in dictionaries(document):
        semantic = item.get("semantic_assertions", {})
        if "json_schema" not in semantic:
            continue
        schema = semantic["json_schema"]
        Draft202012Validator.check_schema(schema)
        values = semantic.get("exact_json", semantic.get("exact_json_fields"))
        if values is not None and set(schema.get("required", [])) <= set(values):
            Draft202012Validator(schema).validate(values)


@pytest.mark.parametrize("agent", ["weather-agent", "healthcare-agent"])
def test_baseline_inputs_do_not_supply_completed_answers(agent):
    for request in traffic(agent)["requests"]:
        prompt = text(request)
        assert not re.search(r"return exactly(?::| this bare JSON object)", prompt, re.I)
        expected_text = assertions(request).get("exact_text")
        if expected_text:
            assert expected_text not in prompt


def test_healthcare_memory_probes_require_previous_context():
    requests = requests_by_id(traffic("healthcare-agent"))
    for suffix, values in (
        ("memory-followup", ("demo-account-a", "Dr. Rivera")),
        ("correction", ("demo-account-a",)),
        ("booking-confirm", ("demo-account-b", "Dr. Ali", "slot-demo-505")),
        ("transition-denied", ("demo-account-a", "Dr. Patel", "slot-demo-701")),
    ):
        probe = requests[f"health-v0-{suffix}"]
        serialized_expectation = json.dumps(assertions(probe))
        for value in values:
            assert value not in text(probe)
            assert value in serialized_expectation


def test_healthcare_approval_oracles_cover_negation_and_changed_identity():
    requests = requests_by_id(traffic("healthcare-agent"))

    def expected(suffix):
        return assertions(requests["health-v0-" + suffix])["exact_json_fields"]

    pending = expected("guardrails")
    approved = expected("booking-confirm")
    replacement = expected("booking-correction")
    assert pending["approval"] == replacement["approval"] == "pending"
    assert approved["approval"] == "confirmed"
    for key in ("account_scope", "provider", "slot"):
        assert pending[key] == approved[key]
    assert replacement["slot"] != approved["slot"]
    assert replacement["account_scope"] == approved["account_scope"]
    for suffix, confirmation, state in (
        ("transition-review", False, "pending"),
        ("transition-denied", False, "pending"),
        ("transition-confirm", True, "confirmed"),
        ("transition-cancel", True, "cancelled"),
    ):
        assert expected(suffix)["confirmation"] is confirmation
        assert expected(suffix)["state"] == state
    denied = text(requests["health-v0-transition-denied"]).lower()
    assert "not" in denied and "confirm" in denied


def test_healthcare_scope_fixture_contains_distinguishable_records():
    request = requests_by_id(traffic("healthcare-agent"))["health-v0-scope"]
    semantic = assertions(request)
    assert len(re.findall(r"\brecord-[\w-]+", text(request))) >= 2
    for value in semantic["required_terms_all"] + semantic["forbidden_terms"]:
        assert value in text(request)
    assert not set(semantic["required_terms_all"]) & set(semantic["forbidden_terms"])


def test_stale_correction_oracles_are_grounded_in_varied_earlier_turns():
    document = traffic("healthcare-agent", "issue-009")
    definition = read_json(PROMPT_ROOTS[1] / "issues" / "issue-009" / "definition.json")
    pairs = list(zip(document["requests"][::2], document["requests"][1::2], strict=True))
    stale_selections = set()
    for setup, probe in pairs:
        values = assertions(probe)["exact_json_fields"]
        date.fromisoformat(values["date"])
        for value in values.values():
            assert value in text(setup)
        for key in ("provider", "slot"):
            assert values[key] not in text(probe)
            assert values[key] not in definition["definition"]["instructions"]
        assert re.search(r"slot-demo-revised-\d+", text(probe))
        stale_selections.add((values["provider"], values["slot"], values["date"]))
        assert setup["request"]["body"]["conversation"] == probe["request"]["body"]["conversation"]
    assert len(stale_selections) >= 3


def test_handoff_fixture_supplies_the_fields_omitted_by_the_defect():
    required = {"owner", "next_action", "deadline", "validation"}
    for request in traffic("healthcare-agent", "issue-007")["requests"]:
        assert all(field in text(request) for field in required)
        assert required.isdisjoint(assertions(request)["exact_json"])


def test_create_envelope_defect_omits_only_scope_and_approval_controls():
    for request in traffic("healthcare-agent", "issue-008")["requests"]:
        expected = assertions(request)["exact_json_fields"]
        assert set(expected) == {"action", "provider", "slot", "message"}
        assert expected["action"] == "create_appointment"
        assert expected["provider"] in text(request)
        assert expected["slot"] in text(request)
        assert re.search(r"\bdemo-account-[ab]\b", text(request))


def test_unsupported_availability_keeps_the_requested_identity():
    for request in traffic("healthcare-agent", "issue-010")["requests"]:
        expected = assertions(request)["exact_json_fields"]
        assert expected["evidence"] == "missing"
        assert expected["availability"] == "available"
        for key in ("account_scope", "provider", "slot", "date"):
            assert expected[key] in text(request)


def test_scope_leak_oracle_selects_an_actual_foreign_record():
    for request in traffic("healthcare-agent", "issue-012")["requests"]:
        expected = assertions(request)["exact_json_fields"]
        assert expected["account_scope"] == "demo-account-b"
        assert "demo-account-a" in text(request)
        for key in ("account_scope", "record_id", "provider", "slot"):
            assert expected[key] in text(request)


def test_unsupported_weather_claim_is_not_provided_by_the_fixture():
    for request in traffic("weather-agent", "issue-001")["requests"]:
        expected = assertions(request)["exact_json"]
        assert expected["condition"] not in text(request)
        assert expected["evidence"] == "missing"


def test_weather_unit_memory_does_not_relabel_a_temperature():
    document = traffic("weather-agent", "issue-004")
    for index, request in enumerate(document["requests"]):
        values = measurements(request)
        assert values["temperature_fahrenheit"] == pytest.approx(
            values["temperature_celsius"] * 9 / 5 + 32
        )
        expected = assertions(request)["exact_json_fields"]
        assert expected["unit"] == ("celsius" if index == 0 else "fahrenheit")
        assert expected["temperature"] == values[f"temperature_{expected['unit']}"]


def test_forecast_substitution_uses_distinct_grounded_forecast_values():
    for request in traffic("weather-agent", "issue-003")["requests"]:
        values = measurements(request)
        expected = assertions(request)["exact_json_fields"]
        assert expected["high"] == values["high"]
        assert expected["low"] == values["low"]
        assert values["temperature"] not in (expected["high"], expected["low"])
        assert "current conditions" in text(request)


def test_unnecessary_clarification_pairs_do_not_add_weather_evidence():
    requests = traffic("weather-agent", "issue-005")["requests"]
    for first, second in zip(requests[::2], requests[1::2], strict=True):
        assert assertions(first)["question_only"] is True
        assert measurements(first)
        assert not measurements(second)
        assert assertions(second)["exact_json"]["temperature"] == measurements(first)["temperature"]
        assert first["request"]["body"]["conversation"] == second["request"]["body"]["conversation"]


def test_overgeneration_template_exceeds_bound_without_adding_facts():
    definition = read_json(PROMPT_ROOTS[0] / "issues" / "issue-006" / "definition.json")
    templates = re.findall(r"`([^`]*<location>[^`]*)`", definition["definition"]["instructions"])
    repeated = templates[-1]
    for request in traffic("weather-agent", "issue-006")["requests"]:
        location = re.search(r"evidence for ([^:]+):", text(request)).group(1)
        condition = re.search(r"condition=(\w+)", text(request)).group(1)
        temperature = re.search(r"temperature=(-?\d+)", text(request)).group(1)
        answer = repeated
        for key, value in (
            ("location", location), ("condition", condition),
            ("temperature", temperature), ("unit-name", "Celsius"),
        ):
            answer = answer.replace(f"<{key}>", value)
        assert "<" not in answer
        concise = f"{location}: {condition}, {temperature} Celsius."
        assert len(concise.split()) <= 10
        assert answer.replace(concise, "").strip() == ""
        assert len(answer.split()) >= assertions(request)["min_words"] > 10
        for term, minimum in assertions(request)["minimum_term_occurrences"].items():
            assert answer.casefold().count(term.casefold()) >= minimum
