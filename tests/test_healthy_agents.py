from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

from agent_insights_quality.contracts import EXPECTED_AGENTS, ROOT
from agent_insights_quality.healthy_agents import load_healthy_agents


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


def test_prompt_definition_resolves_model_only_at_runtime() -> None:
    weather = next(agent for agent in load_healthy_agents() if agent.id == "aiq-001-weather")
    resolved = weather.definition_for_deployment(model_deployment_name="runtime-model")
    assert resolved["model"] == "runtime-model"
    assert weather.definition["model"] == "${AIQ_MODEL_DEPLOYMENT_NAME}"


def test_finance_logic_never_performs_transfers() -> None:
    logic = _load_logic(
        ROOT / "agents" / "finance-hosted" / "source" / "logic.py",
        "aiq_finance_logic",
    )
    assert "SYN-100 balance is USD 2450.00" in logic.handle(
        "account-summary account=SYN-100 currency=USD"
    )
    assert "No transfer was attempted" in logic.handle(
        "prepare-budget account=SYN-100 monthly_limit=1200"
    )
    assert "not authorized" in logic.handle(
        "transfer account=SYN-100 destination=SYN-200 amount=50"
    )


def test_travel_logic_does_not_fabricate_or_book_without_confirmation() -> None:
    logic = _load_logic(
        ROOT / "agents" / "travel-hosted" / "source" / "logic.py",
        "aiq_travel_logic",
    )
    assert "No matching synthetic flight inventory" in logic.handle(
        "search-trip origin=SEA destination=SFO date=2030-05-10"
    )
    assert "No booking was made" in logic.handle(
        "request-booking inventory=FL-SEA-PDX-101 confirmed=false"
    )


def test_ticket_logic_uses_current_revision_and_bounded_escalation() -> None:
    logic = _load_logic(
        ROOT / "agents" / "support-ticket-hosted-image" / "container" / "logic.py",
        "aiq_ticket_logic",
    )
    assert "revision 4" in logic.handle("read-ticket ticket=TKT-1001")
    assert "not revision 3" in logic.handle(
        "update-ticket ticket=TKT-1001 status=resolved expected_revision=3"
    )
    assert "No escalation applied" in logic.handle("escalate-ticket ticket=TKT-1002")


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
        assert requirements.strip() == "azure-ai-agentserver-responses==2.0.0b0"


def test_ghcr_workflow_never_pushes_pull_requests() -> None:
    workflow = (ROOT / ".github" / "workflows" / "build-ticket-agent.yml").read_text(
        encoding="ascii"
    )
    assert "if: github.event_name == 'pull_request'" in workflow
    assert "push: false" in workflow
    assert "if: github.event_name != 'pull_request'" in workflow
    assert "packages: write" in workflow
    assert "image_digest: ${{ steps.build.outputs.digest }}" in workflow
    assert "azurecr.io" not in workflow


def test_all_healthy_fixtures_are_ascii_json() -> None:
    for path in sorted((ROOT / "agents").glob("*/healthy-traffic.json")):
        value = json.loads(path.read_text(encoding="ascii"))
        assert len(value) >= 3
