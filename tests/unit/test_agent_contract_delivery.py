"""Actual Agent assets through offline provider boundaries, not model-behavior proof."""

import copy
import io
import json
import zipfile
from pathlib import Path

import pytest

from agent_insights_quality.catalogs import load_catalog
from agent_insights_quality.providers.artifacts import prepare_artifact
from agent_insights_quality.selection import LastTest, select_staging
from agent_insights_quality.traffic import load_attempts
from test_provider_runtime import (
    FakeTransport, deployed, environment as environment, response, run, runtime,
)


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("version", ["v0", "issue-011"])
def test_healthcare_deployment_delivers_the_complete_version_owned_definition(
    environment, version,
):
    target = load_catalog(ROOT).target(f"healthcare-agent/{version}")
    definition = json.loads(
        (target.version_root / "definition.json").read_text(encoding="utf-8")
    )["definition"]
    wire = FakeTransport(
        response(status=404), response({"version": "42", "status": "active"}, 201),
    )
    result = run(runtime(environment, wire).ensure_deployment(
        target, "reviewed-source", None, lambda value: None,
    ))

    submitted = json.loads(wire.requests[-1].body)
    assert submitted["definition"] == definition
    assert set(submitted["definition"]) == {"kind", "model", "instructions"}
    assert result.agent_type == "prompt"
    assert result.provider_version == "42"


@pytest.mark.parametrize("version,index", [
    ("v0", 6), ("v0", 9), ("issue-011", 5), ("issue-011", 9),
])
def test_healthcare_canonical_turns_keep_their_response_chain_and_request_contract(
    environment, version, index,
):
    target = load_catalog(ROOT).target(f"healthcare-agent/{version}")
    attempt = next(item for item in load_attempts(target) if item.index == index)
    original = copy.deepcopy(attempt)
    wire = FakeTransport(*[
        response({"id": f"response-{number}", "status": "completed"})
        for number in range(1, len(attempt.steps) + 1)
    ])
    provider = runtime(environment, wire)
    deployment = deployed(target)
    previous = None

    for number, step in enumerate(attempt.steps, start=1):
        result = run(provider.invoke(
            deployment, step, request_id=f"request-{number}", session_id=None,
            previous_response_id=previous, persist=lambda value: None,
        ))
        expected = {
            **step.body,
            "agent_reference": {
                "type": "agent_reference", "name": deployment.agent_name, "version": "42",
            },
            "store": True,
        }
        if previous is not None:
            expected["previous_response_id"] = previous
        assert json.loads(wire.requests[-1].body) == expected
        assert result.response_id == f"response-{number}"
        previous = result.response_id

    assert attempt == original
    assert not wire.replies


def test_finance_package_contains_the_unmodified_version_owned_instruction_source(environment):
    target = load_catalog(ROOT).target("finance-agent/v0")
    artifact = run(prepare_artifact(
        target, environment, "reviewed-source", images=None, hosted_environment={},
    ))
    with zipfile.ZipFile(io.BytesIO(artifact.archive)) as archive:
        for name in ("finance.py", "app.py", "tools.py", "retry.py", "observability.py"):
            assert archive.read(f"source/{name}") == (
                target.version_root / "source" / name
            ).read_bytes()
    assert artifact.definition["code_configuration"]["entry_point"] == [
        "python", "-m", "source.app",
    ]


def test_version_owned_agent_contract_repairs_select_only_the_three_changed_targets():
    catalog = load_catalog(ROOT)
    records = {
        target.key: LastTest("reviewed-source", "PASS", "2026-09-01")
        for target in catalog.targets
    }
    selected = select_staging(
        catalog, last_tests=records,
        changed_paths=[
            Path("agents", "healthcare-agent", "v0", "definition.json"),
            Path("agents", "healthcare-agent", "issues", "issue-011", "definition.json"),
            Path("agents", "finance-agent", "v0", "source", "finance.py"),
            Path("tests", "unit", "test_agent_contract_delivery.py"),
        ],
    )
    assert {item.target.key for item in selected} == {
        "healthcare-agent/v0", "healthcare-agent/issue-011", "finance-agent/v0",
    }
    assert all(item.action == "traffic" for item in selected)
