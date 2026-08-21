from __future__ import annotations

import importlib.util
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

from agent_insights_quality.contracts import EXPECTED_AGENTS, ROOT
import pytest

from agent_insights_quality.healthy_agents import (
    load_healthy_agents,
    require_live_telemetry_qualification,
)
from agent_insights_quality.agent_runtime import (
    DeploymentReceipt,
    InvocationReceipt,
    LiveSpanEvidence,
    LiveTelemetryEvidence,
    RuntimeContractError,
)


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
    assert "(github.event_name == 'push' || github.event_name == 'workflow_dispatch')" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "vars.AIQ_GHCR_PUBLISH_ENABLED == 'true'" in workflow
    assert "environment: ghcr-publish" in workflow
    assert "ref: ${{ github.sha }}" in workflow
    assert "persist-credentials: false" in workflow
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


def _live_qualification_contract():
    agents = load_healthy_agents()
    run_id = "live-qualification-20300101"
    window_start = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
    window_end = window_start + timedelta(minutes=15)
    deployments = [
        DeploymentReceipt(
            agent_name=f"{agent.id}-qualification",
            agent_version="7",
            agent_type=agent.kind,
            artifact_digest="sha256:" + f"{index + 1:064x}",
            run_id=run_id,
            status="active",
            source_digest=(
                "sha256:" + f"{index + 101:064x}"
                if agent.kind == "hosted_code"
                else None
            ),
            image_digest=(
                "sha256:" + f"{index + 201:064x}"
                if agent.kind == "hosted_custom_container"
                else None
            ),
        )
        for index, agent in enumerate(agents)
    ]
    deployment_by_id = dict(zip((agent.id for agent in agents), deployments, strict=True))
    invocations = [
        InvocationReceipt(
            fixture_id=fixture.id,
            agent_name=deployment_by_id[agent.id].agent_name,
            agent_version=deployment_by_id[agent.id].agent_version,
            response_id=f"response-{agent.id}-{index}",
            invocation_id=f"invocation-{agent.id}-{index}",
            request_id=f"request-{agent.id}-{index}",
            session_id=(f"session-{agent.id}-{index}" if agent.kind != "prompt" else None),
            output_text=fixture.output_contains,
            called_tools=fixture.expected_tool_calls,
        )
        for agent in agents
        for index, fixture in enumerate(agent.fixtures)
    ]
    invocation_by_key = {
        (agent.id, receipt.fixture_id): receipt
        for agent in agents
        for receipt in invocations
        if receipt.agent_name == deployment_by_id[agent.id].agent_name
    }
    evidence = [
        LiveTelemetryEvidence(
            run_id=run_id,
            agent_id=agent.id,
            agent_name=deployment_by_id[agent.id].agent_name,
            agent_version=deployment_by_id[agent.id].agent_version,
            fixture_id=fixture.id,
            response_id=invocation_by_key[(agent.id, fixture.id)].response_id,
            invocation_id=invocation_by_key[(agent.id, fixture.id)].invocation_id,
            request_id=invocation_by_key[(agent.id, fixture.id)].request_id,
            session_id=invocation_by_key[(agent.id, fixture.id)].session_id,
            operation_id=hashlib.sha256(
                f"{agent.id}:{fixture.id}".encode("ascii")
            ).hexdigest()[:32],
            observed_at=window_start + timedelta(seconds=index + 1),
            spans=(
                LiveSpanEvidence(
                    operation_id=hashlib.sha256(
                        f"{agent.id}:{fixture.id}".encode("ascii")
                    ).hexdigest()[:32],
                    span_id="1" * 16,
                    parent_span_id=None,
                    observed_at=window_start + timedelta(seconds=index + 1),
                    kind="agent",
                    name="invoke_agent",
                ),
                LiveSpanEvidence(
                    operation_id=hashlib.sha256(
                        f"{agent.id}:{fixture.id}".encode("ascii")
                    ).hexdigest()[:32],
                    span_id="2" * 16,
                    parent_span_id="1" * 16,
                    observed_at=window_start + timedelta(seconds=index + 2),
                    kind="model",
                    name="model.responses.create",
                ),
                *(
                    LiveSpanEvidence(
                        operation_id=hashlib.sha256(
                            f"{agent.id}:{fixture.id}".encode("ascii")
                        ).hexdigest()[:32],
                        span_id=f"{tool_index + 3:016x}",
                        parent_span_id="1" * 16,
                        observed_at=window_start
                        + timedelta(seconds=index + tool_index + 3),
                        kind="tool",
                        name=f"tool.{name}",
                        tool_name=name,
                        tool_arguments=fixture.tool_outputs[name]["arguments"],
                        tool_result=fixture.tool_outputs[name]["result"],
                    )
                    for tool_index, name in enumerate(fixture.expected_tool_calls)
                ),
            ),
        )
        for agent in agents
        for index, fixture in enumerate(agent.fixtures)
    ]
    return {
        "run_id": run_id,
        "window_start": window_start,
        "window_end": window_end,
        "deployments": deployments,
        "invocations": invocations,
        "evidence": evidence,
    }


def test_live_telemetry_qualification_binds_exact_receipts_and_span_evidence() -> None:
    contract = _live_qualification_contract()
    require_live_telemetry_qualification(**contract)

    incomplete = {**contract, "evidence": contract["evidence"][:-1]}
    with pytest.raises(RuntimeContractError, match="coverage"):
        require_live_telemetry_qualification(**incomplete)

    changed = list(contract["evidence"])
    hosted_index = next(
        index
        for index, item in enumerate(changed)
        if item.agent_id == "aiq-003-finance"
    )
    item = changed[hosted_index]
    changed[hosted_index] = replace(
        item,
        spans=(
            *item.spans[:-1],
            replace(item.spans[-1], tool_result="tampered"),
        ),
    )
    with pytest.raises(RuntimeContractError, match="inputs or outputs"):
        require_live_telemetry_qualification(**{**contract, "evidence": changed})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("operation_id", "not-an-operation-id", "operation IDs"),
        ("operation_id", "0" * 32, "operation IDs"),
        ("response_id", "stale-response", "current receipt"),
        ("session_id", "stale-session", "current receipt"),
        ("agent_version", "previous", "current receipt"),
        (
            "observed_at",
            datetime(2029, 12, 31, 23, 59, tzinfo=timezone.utc),
            "stale",
        ),
    ],
)
def test_live_telemetry_qualification_rejects_stale_or_malformed_evidence(
    field: str,
    value: object,
    message: str,
) -> None:
    contract = _live_qualification_contract()
    evidence = list(contract["evidence"])
    evidence[0] = replace(evidence[0], **{field: value})
    with pytest.raises(RuntimeContractError, match=message):
        require_live_telemetry_qualification(**{**contract, "evidence": evidence})


def test_live_telemetry_qualification_rejects_unrelated_receipts_and_bad_hierarchy() -> None:
    contract = _live_qualification_contract()
    deployments = list(contract["deployments"])
    deployments[0] = replace(deployments[0], run_id="stale-run")
    with pytest.raises(RuntimeContractError, match="stale or inactive"):
        require_live_telemetry_qualification(
            **{**contract, "deployments": deployments}
        )

    malformed_deployments = list(contract["deployments"])
    malformed_deployments[0] = replace(
        malformed_deployments[0],
        artifact_digest="mutable",
    )
    with pytest.raises(RuntimeContractError, match="receipt is malformed"):
        require_live_telemetry_qualification(
            **{**contract, "deployments": malformed_deployments}
        )

    invocations = list(contract["invocations"])
    invocations.append(replace(invocations[0], response_id="unrelated-response"))
    with pytest.raises(RuntimeContractError, match="invocation receipts"):
        require_live_telemetry_qualification(
            **{**contract, "invocations": invocations}
        )

    evidence = list(contract["evidence"])
    evidence[0] = replace(
        evidence[0],
        spans=(
            evidence[0].spans[0],
            replace(evidence[0].spans[1], parent_span_id="f" * 16),
            *evidence[0].spans[2:],
        ),
    )
    with pytest.raises(RuntimeContractError, match="parent-child"):
        require_live_telemetry_qualification(**{**contract, "evidence": evidence})

    malformed_spans = list(contract["evidence"])
    malformed_spans[0] = replace(
        malformed_spans[0],
        spans=(
            replace(malformed_spans[0].spans[0], span_id="0" * 16),
            *malformed_spans[0].spans[1:],
        ),
    )
    with pytest.raises(RuntimeContractError, match="span IDs"):
        require_live_telemetry_qualification(
            **{**contract, "evidence": malformed_spans}
        )

    wrong_operation = list(contract["evidence"])
    wrong_operation[0] = replace(
        wrong_operation[0],
        spans=(
            replace(wrong_operation[0].spans[0], operation_id="f" * 32),
            *wrong_operation[0].spans[1:],
        ),
    )
    with pytest.raises(RuntimeContractError, match="enclosing operation"):
        require_live_telemetry_qualification(
            **{**contract, "evidence": wrong_operation}
        )

    stale_span = list(contract["evidence"])
    stale_span[0] = replace(
        stale_span[0],
        spans=(
            replace(
                stale_span[0].spans[0],
                observed_at=contract["window_start"] - timedelta(seconds=1),
            ),
            *stale_span[0].spans[1:],
        ),
    )
    with pytest.raises(RuntimeContractError, match="stale"):
        require_live_telemetry_qualification(
            **{**contract, "evidence": stale_span}
        )

    reused_invocations = list(contract["invocations"])
    reused_request_id = list(contract["evidence"])
    reused_invocations[0] = replace(
        reused_invocations[0],
        request_id=reused_request_id[0].operation_id,
    )
    reused_request_id[0] = replace(
        reused_request_id[0],
        request_id=reused_request_id[0].operation_id,
    )
    with pytest.raises(RuntimeContractError, match="current receipt"):
        require_live_telemetry_qualification(
            **{
                **contract,
                "invocations": reused_invocations,
                "evidence": reused_request_id,
            }
        )

    missing_model = list(contract["evidence"])
    missing_model[0] = replace(
        missing_model[0],
        spans=(
            missing_model[0].spans[0],
            *missing_model[0].spans[2:],
        ),
    )
    with pytest.raises(RuntimeContractError, match="core span hierarchy"):
        require_live_telemetry_qualification(
            **{**contract, "evidence": missing_model}
        )

    with pytest.raises(RuntimeContractError, match="bounded UTC window"):
        require_live_telemetry_qualification(
            **{
                **contract,
                "window_end": contract["window_start"] + timedelta(hours=2),
            }
        )


def test_live_telemetry_qualification_checks_prompt_tool_arguments_and_results() -> None:
    contract = _live_qualification_contract()
    evidence = list(contract["evidence"])
    prompt_index = next(
        index
        for index, item in enumerate(evidence)
        if item.agent_id == "aiq-001-weather"
    )
    item = evidence[prompt_index]
    evidence[prompt_index] = replace(
        item,
        spans=(
            *item.spans[:-1],
            replace(item.spans[-1], tool_arguments={"location_id": "stale"}),
        ),
    )
    with pytest.raises(RuntimeContractError, match="inputs or outputs"):
        require_live_telemetry_qualification(**{**contract, "evidence": evidence})

    healthcare_index = next(
        index
        for index, item in enumerate(contract["evidence"])
        if item.agent_id == "aiq-002-healthcare"
        and item.fixture_id == "healthcare-confirmed-create"
    )
    typed_evidence = list(contract["evidence"])
    item = typed_evidence[healthcare_index]
    arguments = dict(item.spans[-1].tool_arguments or {})
    arguments["confirmed"] = 1
    typed_evidence[healthcare_index] = replace(
        item,
        spans=(
            *item.spans[:-1],
            replace(item.spans[-1], tool_arguments=arguments),
        ),
    )
    with pytest.raises(RuntimeContractError, match="inputs or outputs"):
        require_live_telemetry_qualification(
            **{**contract, "evidence": typed_evidence}
        )


def test_live_telemetry_qualification_requires_request_correlation() -> None:
    contract = _live_qualification_contract()
    invocations = list(contract["invocations"])
    evidence = list(contract["evidence"])
    invocations[0] = replace(invocations[0], request_id=None, invocation_id=None)
    evidence[0] = replace(evidence[0], request_id=None, invocation_id=None)
    with pytest.raises(RuntimeContractError, match="invocation receipts are invalid"):
        require_live_telemetry_qualification(
            **{
                **contract,
                "invocations": invocations,
                "evidence": evidence,
            }
        )
