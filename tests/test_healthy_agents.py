from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

from agent_insights_quality.contracts import EXPECTED_AGENTS, ROOT
import pytest

from agent_insights_quality.healthy_agents import (
    load_healthy_agents,
    require_live_telemetry_qualification,
)
from agent_insights_quality.runtime import LiveTelemetryEvidence, RuntimeContractError


def _load_logic(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_five_exact_healthy_agents_have_reviewed_implementation_assets() -> None:
    agents = load_healthy_agents()
    assert {agent.id: agent.kind for agent in agents} == EXPECTED_AGENTS
    assert all(len(agent.fixtures) >= 3 for agent in agents)
    assert all(agent.definition["kind"] in {"prompt", "hosted"} for agent in agents)

    for agent in agents:
        assert all(fixture.id.startswith(agent.id.split("-", 2)[2]) for fixture in agent.fixtures)
        if agent.kind == "prompt":
            assert agent.source is None
            assert agent.definition["model"] == "${AIQ_MODEL_DEPLOYMENT_NAME}"
            assert agent.definition["tools"]
            for tool in agent.definition["tools"]:
                assert tool["strict"] is True
                assert tool["parameters"]["additionalProperties"] is False
        else:
            assert agent.source is not None
            assert agent.definition["protocol_versions"] == [
                {"protocol": "responses", "version": "1.0.0"}
            ]


def test_weather_contract_requires_tool_evidence_for_every_task() -> None:
    weather = next(agent for agent in load_healthy_agents() if agent.id == "aiq-001-weather")
    assert all(fixture.expected_tool_calls for fixture in weather.fixtures)
    assert all(fixture.tool_outputs for fixture in weather.fixtures)
    assert "Never infer" in weather.definition["instructions"]


def test_healthcare_contract_is_scheduling_only_and_confirmation_bounded() -> None:
    healthcare = next(
        agent for agent in load_healthy_agents() if agent.id == "aiq-002-healthcare"
    )
    instructions = healthcare.definition["instructions"]
    assert "Do not diagnose" in instructions
    create_tool = next(
        tool for tool in healthcare.definition["tools"] if tool["name"] == "appointment_create"
    )
    assert create_tool["parameters"]["properties"]["confirmed"] == {"const": True}
    assert set(create_tool["parameters"]["required"]) == {
        "slot_id",
        "patient_id",
        "provider_id",
        "date",
        "starts_at",
        "confirmed",
    }
    confirmation = healthcare.fixtures[-1]
    assert confirmation.tool_outputs["appointment_create"]["arguments"] == {
        "slot_id": "slot-101-0900",
        "patient_id": "patient-syn-001",
        "provider_id": "provider-101",
        "date": "2030-06-03",
        "starts_at": "09:00",
        "confirmed": True,
    }


def test_prompt_definition_resolves_model_only_at_runtime() -> None:
    weather = next(agent for agent in load_healthy_agents() if agent.id == "aiq-001-weather")
    resolved = weather.definition_for_deployment(model_deployment_name="runtime-model")
    assert resolved["model"] == "runtime-model"
    assert weather.definition["model"] == "${AIQ_MODEL_DEPLOYMENT_NAME}"
    finance = next(agent for agent in load_healthy_agents() if agent.id == "aiq-003-finance")
    hosted = finance.definition_for_deployment(
        model_deployment_name="runtime-model",
        project_endpoint="https://sample.services.ai.azure.com/api/projects/quality",
    )
    assert hosted["environment_variables"]["AZURE_AI_MODEL_DEPLOYMENT_NAME"] == "runtime-model"
    assert (
        hosted["environment_variables"]["AZURE_AI_PROJECT_ENDPOINT"]
        == "https://sample.services.ai.azure.com/api/projects/quality"
    )


def test_finance_logic_never_performs_transfers() -> None:
    logic = _load_logic(
        ROOT / "agents" / "finance-hosted" / "source" / "logic.py",
        "aiq_finance_logic",
    )
    assert "SYN-100 balance is USD 2450.00" in logic.execute_tool(
        "account_lookup", {"account_id": "SYN-100"}
    )
    assert "No transfer was attempted" in logic.execute_tool(
        "budget_calculation", {"account_id": "SYN-100", "monthly_limit": 1200}
    )
    assert all(tool["name"] != "transfer" for tool in logic.TOOLS)


def test_travel_logic_does_not_fabricate_or_book_without_confirmation() -> None:
    logic = _load_logic(
        ROOT / "agents" / "travel-hosted" / "source" / "logic.py",
        "aiq_travel_logic",
    )
    assert "No matching synthetic flight inventory" in logic.execute_tool(
        "flight_search",
        {"origin": "SEA", "destination": "SFO", "date": "2030-05-10"},
    )
    assert "No booking was made" in logic.execute_tool(
        "booking", {"inventory_id": "FL-SEA-PDX-101", "confirmed": False}
    )


def test_ticket_logic_uses_current_revision_and_bounded_escalation() -> None:
    logic = _load_logic(
        ROOT / "agents" / "support-ticket-hosted-image" / "container" / "logic.py",
        "aiq_ticket_logic",
    )
    assert "revision 4" in logic.execute_tool("ticket_read", {"ticket_id": "TKT-1001"})
    assert "not revision 3" in logic.execute_tool(
        "ticket_update",
        {"ticket_id": "TKT-1001", "status": "resolved", "expected_revision": 3},
    )
    preview = logic.execute_tool(
        "ticket_update",
        {"ticket_id": "TKT-1001", "status": "resolved", "expected_revision": 4},
    )
    assert "No state was persisted" in preview
    assert "revision 4" in logic.execute_tool("ticket_read", {"ticket_id": "TKT-1001"})
    assert "No escalation applied" in logic.execute_tool(
        "escalation", {"ticket_id": "TKT-1002"}
    )


def test_ticket_container_is_non_root_healthy_and_ghcr_portable() -> None:
    root = ROOT / "agents" / "support-ticket-hosted-image" / "container"
    dockerfile = (root / "Dockerfile").read_text(encoding="ascii")
    manifest = (root / "agent.yaml").read_text(encoding="ascii")
    assert "USER agent" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "/readiness" in dockerfile
    assert "EXPOSE 8088" in dockerfile
    assert "ghcr.io/ninghu/agent-insights-quality-ticket" in manifest
    assert "azurecr.io" not in manifest


def test_hosted_sources_compile_and_require_responses_server() -> None:
    for path in (
        ROOT / "agents" / "finance-hosted" / "source",
        ROOT / "agents" / "travel-hosted" / "source",
        ROOT / "agents" / "support-ticket-hosted-image" / "container",
    ):
        compile((path / "main.py").read_text(encoding="ascii"), str(path / "main.py"), "exec")
        requirements = (path / "requirements.txt").read_text(encoding="ascii")
        for requirement in (
            "azure-ai-agentserver-responses==2.0.0b0",
            "azure-ai-projects",
            "azure-identity",
            "opentelemetry-api",
        ):
            assert requirement in requirements
        runtime = (path / "model_runtime.py").read_text(encoding="ascii")
        assert "responses.create(" in runtime
        assert "model.responses.create" in runtime
        assert "gen_ai.tool.name" in runtime
        assert "tool.arguments" in runtime
        assert "tool.result" in runtime
        assert "previous_response_id" not in runtime
        assert "model_dump(exclude_none=True)" in runtime


def test_ghcr_workflow_never_pushes_pull_requests() -> None:
    workflow = (ROOT / ".github" / "workflows" / "build-ticket-agent.yml").read_text(
        encoding="ascii"
    )
    assert "if: github.event_name == 'pull_request'" in workflow
    assert "push: false" in workflow
    assert "github.event_name == 'push'" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "vars.AIQ_GHCR_PUBLISH_ENABLED == 'true'" in workflow
    assert "environment: ghcr-publish" in workflow
    assert "packages: write" in workflow
    assert "image_digest: ${{ steps.build.outputs.digest }}" in workflow
    assert "uses: actions/checkout@v" not in workflow
    assert "uses: docker/" in workflow
    assert all(
        len(line.rsplit("@", 1)[1].strip()) == 40
        for line in workflow.splitlines()
        if "uses:" in line
    )
    assert "azurecr.io" not in workflow


def test_all_healthy_fixtures_are_ascii_json() -> None:
    for path in sorted((ROOT / "agents").glob("*/healthy-traffic.json")):
        value = json.loads(path.read_text(encoding="ascii"))
        assert len(value) >= 3


def test_live_telemetry_qualification_requires_complete_exact_evidence() -> None:
    agents = load_healthy_agents()
    evidence = [
        LiveTelemetryEvidence(
            agent_id=agent.id,
            agent_name=f"{agent.id}-qualification",
            agent_version="1",
            fixture_id=fixture.id,
            response_id=f"response-{agent.id}-{index}",
            operation_id=f"operation-{agent.id}-{index}",
            span_kinds=frozenset(
                {"agent", "model", "tool"} if agent.kind != "prompt" else {"agent", "model"}
            ),
            tool_names=fixture.expected_tool_calls,
            tool_arguments=tuple(
                fixture.tool_outputs[name]["arguments"]
                for name in fixture.expected_tool_calls
            ),
            tool_results=tuple(
                fixture.tool_outputs[name]["result"]
                for name in fixture.expected_tool_calls
            ),
        )
        for agent in agents
        for index, fixture in enumerate(agent.fixtures)
    ]
    require_live_telemetry_qualification(evidence)

    incomplete = evidence[:-1]
    with pytest.raises(RuntimeContractError, match="coverage"):
        require_live_telemetry_qualification(incomplete)

    changed = list(evidence)
    hosted_index = next(
        index
        for index, item in enumerate(changed)
        if item.agent_id == "aiq-003-finance"
    )
    item = changed[hosted_index]
    changed[hosted_index] = LiveTelemetryEvidence(
        agent_id=item.agent_id,
        agent_name=item.agent_name,
        agent_version=item.agent_version,
        fixture_id=item.fixture_id,
        response_id=item.response_id,
        operation_id=item.operation_id,
        span_kinds=item.span_kinds,
        tool_names=item.tool_names,
        tool_arguments=item.tool_arguments,
        tool_results=("tampered",),
    )
    with pytest.raises(RuntimeContractError, match="inputs or outputs"):
        require_live_telemetry_qualification(changed)
