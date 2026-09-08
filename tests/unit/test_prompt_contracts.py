"""Offline asset/fixture checks only; Prompt behavior requires deployed staging evidence."""

import copy
import json
import re
from datetime import date
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from agent_insights_quality.catalogs import load_catalog
from agent_insights_quality.selection import LastTest, select_staging
from agent_insights_quality.traffic import load_attempts, traffic_validator


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


def phase_steps(document, phase):
    requests = requests_by_id(document)
    return [
        requests[ref]
        for attempt in document["attempts"]
        for ref in attempt[f"{phase}_steps"]
    ]


def case_step(document, index, phase="probe", offset=0):
    ref = document["attempts"][index - 1][f"{phase}_steps"][offset]
    return requests_by_id(document)[ref]


def healthcare_baseline_cases():
    document = traffic("healthcare-agent")
    locations = {
        "memory-followup": (3, "probe", 0),
        "correction": (4, "probe", 0),
        "guardrails": (5, "probe", 0),
        "booking-confirm": (6, "probe", 0),
        "booking-correction": (6, "probe", 1),
        "comparison-resumed": (6, "probe", 2),
        "transition-review": (9, "setup", 0),
        "transition-denied": (9, "probe", 0),
        "transition-confirm": (9, "probe", 1),
        "transition-cancel": (9, "probe", 2),
        "scope": (10, "probe", 0),
    }
    return {name: case_step(document, *location) for name, location in locations.items()}


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
        validator = (
            traffic_validator(ROOT / "schemas", True)
            if filename == "traffic.json" else Draft202012Validator(schema)
        )
        validator.validate(document)
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
    assert not traffic_validator(ROOT / "schemas", True).is_valid(document)


@pytest.mark.parametrize("directory", AUTHORITIES)
def test_attempts_resolve_reviewed_requests_and_expectations(directory):
    document = read_json(directory / "traffic.json")
    sources = requests_by_id(document)
    assert len(sources) == len(document["requests"])
    referenced = set()
    target = load_catalog(ROOT).target(
        f"{document['agent_name']}/{document['logical_version']}"
    )
    attempts = load_attempts(target)
    assert len(attempts) == 10
    assert [attempt.index for attempt in attempts] == list(range(1, 11))
    for attempt, case in zip(attempts, document["attempts"], strict=True):
        ids = case["setup_steps"] + case["probe_steps"]
        referenced.update(ids)
        assert case["probe_steps"]
        assert len(ids) == len(set(ids))
        for ref, step in zip(ids, attempt.steps, strict=True):
            source = sources[ref]
            assert step.body == source["request"]["body"]
            assert step.expected == source["expected"]
            assert "conversation" not in step.body
            if step.phase == "probe":
                assert assertions(source)
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


@pytest.mark.parametrize("directory", [
    path.parent for path in sorted((ROOT / "agents" / "healthcare-agent").rglob("definition.json"))
])
def test_healthcare_listing_guidance_preserves_record_identity_without_open_slot_claim(directory):
    instructions = read_json(directory / "definition.json")["definition"]["instructions"]
    assert "label them as existing appointment records" in instructions
    assert "retain their supplied in-scope record identifiers" in instructions
    assert "An existing record does not by itself prove that a slot is open or bookable" in instructions
    assert "Continue honoring any explicitly requested JSON field set" in instructions
    case = healthcare_baseline_cases()["scope"]
    assert "record-a-801" in assertions(case)["required_terms_all"]
    assert "existing appointment records" not in text(case)


def test_healthcare_memory_probes_require_previous_context():
    requests = healthcare_baseline_cases()
    for suffix, values in (
        ("memory-followup", ("demo-account-a", "Dr. Rivera")),
        ("correction", ("demo-account-a",)),
        ("booking-confirm", ("demo-account-b", "Dr. Ali", "slot-demo-505")),
        ("transition-denied", ("demo-account-a", "Dr. Patel", "slot-demo-701")),
    ):
        probe = requests[suffix]
        serialized_expectation = json.dumps(assertions(probe))
        for value in values:
            assert value not in text(probe)
            assert value in serialized_expectation


def test_healthcare_approval_oracles_cover_negation_and_changed_identity():
    requests = healthcare_baseline_cases()

    def expected(suffix):
        return assertions(requests[suffix])["exact_json_fields"]

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
    denied = text(requests["transition-denied"]).lower()
    assert "not" in denied and "confirm" in denied


@pytest.mark.parametrize(
    "case",
    [
        "guardrails", "booking-confirm", "booking-correction",
        "transition-review", "transition-confirm",
    ],
)
@pytest.mark.parametrize("nested_slot", ["object", "array"])
def test_healthcare_envelope_schema_rejects_nested_slot_identity(case, nested_slot):
    semantic = assertions(healthcare_baseline_cases()[case])
    validator = Draft202012Validator(semantic["json_schema"])
    envelope = copy.deepcopy(semantic["exact_json_fields"])
    for name, value in (
        ("message", "Please confirm"),
        ("warning", "Schedule evidence for 2026-09-15 is unavailable."),
    ):
        if name in semantic["json_schema"]["required"]:
            envelope[name] = value
    validator.validate(envelope)
    slot_id = envelope["slot"]
    assert isinstance(slot_id, str)
    envelope["slot"] = (
        {"identifier": slot_id} if nested_slot == "object" else [slot_id]
    )

    # Finding the correct identifier somewhere in the output cannot satisfy slot:string.
    assert slot_id in json.dumps(envelope)
    errors = list(validator.iter_errors(envelope))
    assert len(errors) == 1
    assert errors[0].validator == "type"
    assert list(errors[0].absolute_path) == ["slot"]


def test_healthcare_pending_proposal_can_disclose_gap_without_nesting_slot():
    request = healthcare_baseline_cases()["guardrails"]
    semantic = assertions(request)
    unavailable_date = re.search(
        r"Schedule evidence for (\d{4}-\d{2}-\d{2}) is explicitly unavailable",
        text(request),
    ).group(1)
    assert any(
        unavailable_date in claim and "unavailable" in claim
        for claim in semantic["required_claims"]
    )
    assert re.search(
        rf"{re.escape(semantic['exact_json_fields']['slot'])} open", text(request)
    )
    envelope = {
        **semantic["exact_json_fields"],
        "message": "Please confirm",
        "warning": f"Schedule evidence for {unavailable_date} is unavailable.",
    }
    Draft202012Validator(semantic["json_schema"]).validate(envelope)
    assert envelope["approval"] == "pending"
    assert envelope["slot"] == semantic["exact_json_fields"]["slot"]
    assert unavailable_date in envelope["warning"]
    without_warning = {key: value for key, value in envelope.items() if key != "warning"}
    assert unavailable_date not in json.dumps(without_warning)


@pytest.mark.parametrize("index,phase", [(5, "probe"), (6, "setup")])
def test_cross_date_proposal_still_requires_both_confirmation_request_and_gap(index, phase):
    semantic = assertions(case_step(traffic("healthcare-agent"), index, phase))
    validator = Draft202012Validator(semantic["json_schema"])
    envelope = {
        **semantic["exact_json_fields"],
        "message": "Please confirm",
        "warning": "Schedule evidence for 2026-09-15 is unavailable.",
    }
    validator.validate(envelope)
    for omitted in ("message", "warning"):
        assert not validator.is_valid({key: value for key, value in envelope.items() if key != omitted})
    assert semantic["required_claims"] and semantic["forbidden_claims"]


@pytest.mark.parametrize("case", ["booking-confirm", "booking-correction"])
def test_user_selected_evidenced_booking_allows_unrelated_warning_to_be_omitted(case):
    semantic = assertions(healthcare_baseline_cases()[case])
    envelope = copy.deepcopy(semantic["exact_json_fields"])
    if envelope["approval"] == "pending":
        envelope["message"] = "Please confirm"
    validator = Draft202012Validator(semantic["json_schema"])
    validator.validate(envelope)
    validator.validate({
        **envelope, "warning": "Schedule evidence for the other date, 2026-09-15, remains unavailable.",
    })
    assert "warning" not in semantic["json_schema"]["required"]
    for mandatory in ("approval", "account_scope"):
        assert not validator.is_valid({key: value for key, value in envelope.items() if key != mandatory})
    if envelope["approval"] == "pending":
        assert not validator.is_valid({key: value for key, value in envelope.items() if key != "message"})


def test_returning_to_wider_comparison_requires_retained_gap_without_a_booking_action():
    request = healthcare_baseline_cases()["comparison-resumed"]
    semantic = assertions(request)
    validator = Draft202012Validator(semantic["json_schema"])
    response = copy.deepcopy(semantic["exact_json_fields"])
    validator.validate(response)
    assert not validator.is_valid({**response, "coverage": "complete"})
    assert not validator.is_valid({**response, "unknown_dates": []})
    assert not validator.is_valid({
        **response, "dates_with_evidence": response["dates_with_evidence"] + response["unknown_dates"],
    })
    assert not validator.is_valid({**response, "action": "create_appointment"})
    assert "unavailable" not in text(request).lower()
    attempts = load_attempts(load_catalog(ROOT).target("healthcare-agent/v0"))
    assert len(attempts) == 10
    assert len(attempts[5].steps) == 4
    assert attempts[5].steps[-1].body == request["request"]["body"]


def test_healthcare_scope_contract_changes_select_only_its_baseline():
    catalog = load_catalog(ROOT)
    records = {
        target.key: LastTest("previous-source", "PASS", "2026-09-01")
        for target in catalog.targets
    }
    selected = select_staging(catalog, last_tests=records, changed_paths=[
        Path("agents", "healthcare-agent", "v0", name)
        for name in ("definition.json", "traffic.json", "implementation.yaml")
    ])
    assert [item.target.key for item in selected] == ["healthcare-agent/v0"]
    assert selected[0].action == "traffic"


def test_healthcare_scope_fixture_contains_distinguishable_records():
    request = healthcare_baseline_cases()["scope"]
    semantic = assertions(request)
    assert len(re.findall(r"\brecord-[\w-]+", text(request))) >= 2
    for value in semantic["required_terms_all"] + semantic["forbidden_terms"]:
        assert value in text(request)
    assert not set(semantic["required_terms_all"]) & set(semantic["forbidden_terms"])


def test_healthcare_bounded_fixture_requires_date_within_its_word_limit():
    request = case_step(traffic("healthcare-agent"), 2)
    semantic = assertions(request)
    required = semantic["required_terms_all"]
    account = next(term for term in required if term.startswith("demo-account-"))
    provider = next(term for term in required if term.startswith("Dr. "))
    slot = next(term for term in required if term.startswith("slot-demo-"))
    time = next(term for term in required if re.fullmatch(r"\d{2}:\d{2}", term))
    day = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text(request)).group()
    assert day in required

    # Public synthetic examples check fixture feasibility, not model behavior.
    complete = f"For {account}, {provider} lists {slot} at {time} on {day}."
    assert len(complete.split()) <= semantic["max_words"]
    assert all(term in complete for term in required)
    assert not any(term in complete for term in semantic["forbidden_terms"])
    missing_date = complete.replace(f" on {day}", "")
    assert {term for term in required if term not in missing_date} == {day}
    structured = {"summary": missing_date, "date": day}
    assert all(term in json.dumps(structured) for term in required)


@pytest.mark.parametrize("identifier_prefix", ["demo-account-", "record-"])
def test_healthcare_scope_fixture_forbids_identifiers_in_explanatory_notes(identifier_prefix):
    semantic = assertions(healthcare_baseline_cases()["scope"])
    required = semantic["required_terms_all"]
    in_scope = {
        "account_scope": next(term for term in required if term.startswith("demo-account-")),
        "record_id": next(term for term in required if term.startswith("record-")),
        "provider": next(term for term in required if term.startswith("Dr. ")),
        "slot": next(term for term in required if term.startswith("slot-demo-")),
    }
    foreign = next(
        term for term in semantic["forbidden_terms"] if term.startswith(identifier_prefix)
    )
    response = {"appointments": [in_scope]}
    assert all(term in json.dumps(response) for term in required)
    assert not any(term in json.dumps(response) for term in semantic["forbidden_terms"])

    response["note"] = f"Information associated with {foreign} was excluded from the selection."
    assert foreign not in json.dumps(response["appointments"])
    assert {
        term for term in semantic["forbidden_terms"] if term in json.dumps(response)
    } == {foreign}


def test_stale_correction_oracles_are_grounded_in_varied_earlier_turns():
    document = traffic("healthcare-agent", "issue-009")
    definition = read_json(PROMPT_ROOTS[1] / "issues" / "issue-009" / "definition.json")
    pairs = [
        (case_step(document, index, "setup"), case_step(document, index))
        for index in range(1, 11)
    ]
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
        assert "conversation" not in setup["request"]["body"]
        assert "conversation" not in probe["request"]["body"]
    assert len(stale_selections) >= 3


def test_create_envelope_defect_omits_only_scope_and_approval_controls():
    for request in phase_steps(traffic("healthcare-agent", "issue-008"), "probe"):
        expected = assertions(request)["exact_json_fields"]
        assert set(expected) == {"action", "provider", "slot", "message"}
        assert expected["action"] == "create_appointment"
        assert expected["provider"] in text(request)
        assert expected["slot"] in text(request)
        assert re.search(r"\bdemo-account-[ab]\b", text(request))


def test_unsupported_availability_keeps_the_requested_identity():
    for request in phase_steps(traffic("healthcare-agent", "issue-010"), "probe"):
        expected = assertions(request)["exact_json_fields"]
        assert expected["evidence"] == "missing"
        assert expected["availability"] == "available"
        for key in ("account_scope", "provider", "slot", "date"):
            assert expected[key] in text(request)


def test_scope_leak_oracle_selects_an_actual_foreign_record():
    definition = read_json(PROMPT_ROOTS[1] / "issues" / "issue-012" / "definition.json")
    instructions = definition["definition"]["instructions"]
    active_schedule_shapes = set()
    foreign_record_ids = set()
    for request in phase_steps(traffic("healthcare-agent", "issue-012"), "probe"):
        prompt = text(request)
        semantic = assertions(request)
        expected = semantic["exact_json_fields"]
        requested_account = re.findall(r"\bdemo-account-[a-z]+\b", prompt)[-1]
        supplied_records = [
            match.groupdict()
            for match in re.finditer(
                r"(?P<account_scope>demo-account-[a-z]+) has "
                r"(?P<record_id>record-[\w-]+) with "
                r"(?P<provider>Dr\. [A-Za-z]+) at (?P<slot>slot-demo-[\w-]+)",
                prompt,
            )
        ]
        foreign = [
            record for record in supplied_records
            if record["account_scope"] != requested_account
        ]
        assert foreign == [expected]
        assert all(value not in instructions for value in expected.values())
        foreign_record_ids.add(expected["record_id"])
        active = [
            record for record in supplied_records
            if record["account_scope"] == requested_account
        ]
        active_schedule_shapes.add(bool(active))
        validator = Draft202012Validator(semantic["json_schema"])
        validator.validate(expected)
        for record in active:
            validator.validate(record)
            assert record != expected  # Correct shape alone does not prove the defect.
    assert active_schedule_shapes == {False, True}
    assert len(foreign_record_ids) > 1


@pytest.mark.parametrize(
    "key", ["healthcare-agent/issue-012", "weather-agent/issue-006"]
)
def test_prompt_issue_repair_selects_only_its_deployable_version(key):
    catalog = load_catalog(ROOT)
    records = {
        target.key: LastTest(
            "reviewed-source", "FAIL" if target.key == key else "PASS", "2026-09-01"
        )
        for target in catalog.targets
    }
    agent, issue = key.split("/")
    version = Path("agents", agent, "issues", issue)
    selected = select_staging(
        catalog, last_tests=records,
        changed_paths=[
            version / "definition.json",
            version / "implementation.yaml",
            Path("tests", "unit", "test_prompt_contracts.py"),
        ],
    )
    assert [item.target.key for item in selected] == [key]
    assert selected[0].action == "traffic"


def test_unsupported_weather_claim_is_not_provided_by_the_fixture():
    for request in phase_steps(traffic("weather-agent", "issue-001"), "probe"):
        expected = assertions(request)["exact_json"]
        assert expected["condition"] not in text(request)
        assert expected["evidence"] == "missing"


def test_weather_unit_memory_does_not_relabel_a_temperature():
    document = traffic("weather-agent", "issue-004")
    for phase, unit in (("setup", "celsius"), ("probe", "fahrenheit")):
        for request in phase_steps(document, phase):
            values = measurements(request)
            assert values["temperature_fahrenheit"] == pytest.approx(
                values["temperature_celsius"] * 9 / 5 + 32
            )
            expected = assertions(request)["exact_json_fields"]
            assert expected["unit"] == unit
            assert expected["temperature"] == values[f"temperature_{expected['unit']}"]


def test_forecast_substitution_uses_distinct_grounded_forecast_values():
    for request in phase_steps(traffic("weather-agent", "issue-003"), "probe"):
        values = measurements(request)
        expected = assertions(request)["exact_json_fields"]
        assert expected["high"] == values["high"]
        assert expected["low"] == values["low"]
        assert values["temperature"] not in (expected["high"], expected["low"])
        assert "current conditions" in text(request)


def test_unnecessary_clarification_pairs_do_not_add_weather_evidence():
    requests = phase_steps(traffic("weather-agent", "issue-005"), "probe")
    for first, second in zip(requests[::2], requests[1::2], strict=True):
        assert assertions(first)["question_only"] is True
        assert measurements(first)
        assert not measurements(second)
        assert assertions(second)["exact_json"]["temperature"] == measurements(first)["temperature"]
        document = traffic("weather-agent", "issue-005")
        assert any(
            attempt["probe_steps"] == [first["id"], second["id"]]
            for attempt in document["attempts"]
        )


def test_overgeneration_template_exceeds_bound_without_adding_facts():
    definition = read_json(PROMPT_ROOTS[0] / "issues" / "issue-006" / "definition.json")
    _, active, other = re.split(
        r"\n(?:Repeated-current|Other-request) branch: ",
        definition["definition"]["instructions"],
    )
    templates = re.findall(r"`([^`]*<location>[^`]*)`", active)
    assert len(templates) == 1
    repeated = templates[0]
    assert repeated.count("<location>") == 8
    assert re.findall(r"`([^`]*<location>[^`]*)`", other)[0].count("<location>") == 1
    activation = re.search(r"exact sentence `([^`]+)`", active).group(1)
    for request in phase_steps(traffic("weather-agent", "issue-006"), "setup"):
        assert not text(request).endswith(activation)
    for request in phase_steps(traffic("weather-agent", "issue-006"), "probe"):
        assert text(request).endswith(activation)
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
