import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agent_insights_quality.assessment import DAILY_SCHEMA, STAGING_SCHEMA
from agent_insights_quality.contracts import Environment
from agent_insights_quality.providers import AzureSol, HttpResponse, SolResponseError
from agent_insights_quality.providers.sol import _wire_schema
from agent_insights_quality.selection import Selection
import test_runner as fake


class Transport:
    def __init__(self, result):
        self.result = result
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        return HttpResponse(200, body=json.dumps({
            "status": "completed",
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(self.result)},
            ]}],
        }).encode())


def complete(schema, result):
    transport = Transport(result)
    environment = Environment(
        "staging", "synthetic", "project", "https://example.invalid/api/projects/project",
        "/synthetic/telemetry", "storage", "registry", "swedencentral", "SwedenCentral",
    )
    value = asyncio.run(AzureSol(environment, transport=transport).complete_json(
        instructions="Synthetic assessment", payload={}, schema=schema,
    ))
    return value, json.loads(transport.requests[0].body)


def judgment(*, staging, duplicate=False):
    attempts = [{
        "index": index, "sufficient": True, "observed": True,
        "citations": [{
            "attempt": index, "step_id": "probe",
            "refs": ["synthetic-row", "synthetic-row"] if duplicate else ["synthetic-row"],
        }],
        "reason": "Synthetic evidence",
        **({"contract_violation": False} if staging else {}),
    } for index in range(1, 11)]
    return {"attempts": attempts, **({} if staging else {"cards": [], "limitations": []})}


@pytest.mark.parametrize("schema", [STAGING_SCHEMA, DAILY_SCHEMA])
def test_actual_assessment_wire_omits_only_unsupported_unique_items(schema):
    original = deepcopy(schema)
    result = judgment(staging=schema is STAGING_SCHEMA)
    value, body = complete(schema, result)
    format = body["text"]["format"]
    assert format["strict"] is True
    expected = deepcopy(original)
    expected["properties"]["attempts"]["items"]["properties"]["citations"]["items"]["properties"]["refs"].pop("uniqueItems")
    if schema is DAILY_SCHEMA:
        expected["properties"]["cards"]["items"]["properties"]["citations"]["items"]["properties"]["refs"].pop("uniqueItems", None)
        expected["properties"]["limitations"].pop("uniqueItems")
    assert format["schema"] == expected
    assert schema == original
    assert value == result


@pytest.mark.parametrize("schema", [STAGING_SCHEMA, DAILY_SCHEMA])
def test_duplicate_citation_references_remain_invalid_locally(schema):
    result = judgment(staging=schema is STAGING_SCHEMA, duplicate=True)
    Draft202012Validator(_wire_schema(schema)).validate(result)
    with pytest.raises(SolResponseError, match="sol_output_schema_invalid"):
        complete(schema, result)
    assert schema["properties"]["attempts"]["items"]["properties"]["citations"]["items"]["properties"]["refs"]["uniqueItems"]


def test_duplicate_daily_limitations_remain_invalid_locally():
    result = judgment(staging=False)
    result["limitations"] = ["incomplete_evidence", "incomplete_evidence"]
    with pytest.raises(SolResponseError, match="sol_output_schema_invalid"):
        complete(DAILY_SCHEMA, result)


@pytest.mark.parametrize("keyword", [
    "properties", "patternProperties", "$defs", "definitions", "dependentSchemas", "dependencies",
])
def test_schema_maps_preserve_property_names_and_transform_child_schemas(keyword):
    schema = {
        keyword: {"uniqueItems": {"type": "array", "uniqueItems": True, "minItems": 1}},
        "default": {"uniqueItems": True, "items": {"uniqueItems": True}},
        "const": {"uniqueItems": True},
        "enum": [{"uniqueItems": True}],
        "examples": [{"properties": {"uniqueItems": True}}],
        "x-synthetic-extension": {"uniqueItems": True},
    }
    original = deepcopy(schema)
    projected = _wire_schema(schema)
    expected = deepcopy(original)
    expected[keyword]["uniqueItems"].pop("uniqueItems")
    assert projected == expected
    assert schema == original


@pytest.mark.parametrize("keyword", [
    "items", "additionalItems", "additionalProperties", "unevaluatedItems",
    "unevaluatedProperties", "contains", "propertyNames", "not", "if", "then", "else",
    "contentSchema",
])
def test_single_schema_keywords_are_traversed(keyword):
    assert _wire_schema({keyword: {"uniqueItems": True, "type": "array", "maxItems": 2}}) == {
        keyword: {"type": "array", "maxItems": 2},
    }
    assert _wire_schema({keyword: False}) == {keyword: False}


@pytest.mark.parametrize("keyword", ["items", "prefixItems", "allOf", "anyOf", "oneOf"])
def test_composition_and_tuple_schemas_are_traversed(keyword):
    assert _wire_schema({keyword: [
        {"type": "array", "uniqueItems": False}, True,
        {"properties": {"uniqueItems": {"type": "boolean"}}},
    ]}) == {keyword: [
        {"type": "array"}, True, {"properties": {"uniqueItems": {"type": "boolean"}}},
    ]}


def test_field_named_unique_items_remains_in_actual_request_and_local_response():
    schema = {
        "type": "object",
        "properties": {
            "uniqueItems": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
        },
        "required": ["uniqueItems"],
        "additionalProperties": False,
    }
    value, body = complete(schema, {"uniqueItems": ["synthetic"]})
    assert value == {"uniqueItems": ["synthetic"]}
    assert body["text"]["format"]["schema"]["properties"]["uniqueItems"] == {
        "type": "array", "items": {"type": "string"},
    }
    assert body["text"]["format"]["schema"]["required"] == ["uniqueItems"]


def test_explicit_sol_reassessment_reuses_failed_staging_requests_and_raw_snapshots(tmp_path, monkeypatch):
    fake.fake_storage(monkeypatch)
    harness = fake.Harness(tmp_path, profile="staging")
    original = harness.sol.complete_json

    async def rejected(**kwargs):
        raise SolResponseError(
            "sol_http_error", {"error": {"code": "invalid_json_schema"}}, status=400,
            request_accepted=False,
        )

    monkeypatch.setattr(harness.sol, "complete_json", rejected)
    initial = harness.staging()
    assert all(item["status"] == "INCOMPLETE" for item in initial["results"])
    calls = (
        len(harness.cloud.invocations), sum(harness.cloud.deployment_calls.values()),
        len(harness.cloud.events),
    )
    monkeypatch.setattr(harness.sol, "complete_json", original)
    path = Path("src/agent_insights_quality/providers/sol.py")
    selections = tuple(
        Selection(target, "reassess", ("evaluation_changed",))
        for target in harness.catalog.targets
    )
    repaired = harness.staging(
        "repaired-stage", selections=selections, revision="source-two", changes=(path,),
    )
    assert all(item["status"] == "PASS" for item in repaired["results"])
    assert (len(harness.cloud.invocations), sum(harness.cloud.deployment_calls.values())) == calls[:2]
    assert all(event[0] == "assessment" for event in harness.cloud.events[calls[2]:])
    for old, new in zip(initial["results"], repaired["results"], strict=True):
        assert old["evidence_key"] == new["evidence_key"]
        assert new["traffic_source_revision"] == "source-one"
        assert new["source_revision"] == "source-two"
