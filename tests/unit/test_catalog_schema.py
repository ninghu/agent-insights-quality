"""Reviewed catalog metadata excludes obsolete, ignored trace-shape gates."""

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[2]


def catalog_and_validator(name):
    document = yaml.safe_load(
        (ROOT / "catalogs" / f"{name.upper()}_CATALOG.yaml").read_text(encoding="utf-8")
    )
    schema = json.loads(
        (ROOT / "schemas" / f"{name}-catalog.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(document)
    return document, validator


@pytest.mark.parametrize(
    "field,value",
    [
        ("trace_contract", {
            "minimum_traces": 5,
            "operations": ["invoke_agent", "chat"],
            "anomaly": "unsupported_final_answer",
        }),
        ("minimum_traces", 5),
        ("operations", ["invoke_agent", "chat"]),
        ("anomaly", "unsupported_final_answer"),
    ],
)
def test_issue_catalog_rejects_obsolete_trace_metadata(field, value):
    document, validator = catalog_and_validator("issue")
    for index, issue in enumerate(document["issues"]):
        assert field not in issue
        issue[field] = value
        errors = list(validator.iter_errors(document))
        assert len(errors) == 1
        assert errors[0].validator == "additionalProperties"
        assert list(errors[0].absolute_path) == ["issues", index]
        del issue[field]


@pytest.mark.parametrize("value", ["uniform", "required_per_request"])
def test_agent_catalog_rejects_obsolete_baseline_trace_gate(value):
    document, validator = catalog_and_validator("agent")
    for index, agent in enumerate(document["agents"]):
        baseline = agent["baseline_contract"]
        assert "trace_operations" not in baseline
        baseline["trace_operations"] = value
        errors = list(validator.iter_errors(document))
        assert len(errors) == 1
        assert errors[0].validator == "additionalProperties"
        assert list(errors[0].absolute_path) == ["agents", index, "baseline_contract"]
        del baseline["trace_operations"]
