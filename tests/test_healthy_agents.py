from __future__ import annotations

import ast
import importlib.util
import hashlib
import json
import runpy
import sys
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
from agent_insights_quality.insights.telemetry import (
    TelemetryExpectation,
    correlate_complete_traces,
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
                assert all(
                    property_schema.get("type")
                    in {"array", "boolean", "integer", "number", "object", "string"}
                    for property_schema in tool["parameters"]["properties"].values()
                )
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
    assert create_tool["parameters"]["properties"]["confirmed"] == {
        "type": "boolean",
        "const": True,
    }
    cancel_tool = next(
        tool for tool in healthcare.definition["tools"] if tool["name"] == "appointment_cancel"
    )
    assert cancel_tool["parameters"]["properties"]["confirmed"] == {
        "type": "boolean",
        "const": True,
    }
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


def test_every_prompt_fixture_names_one_unambiguous_expected_tool() -> None:
    for agent in load_healthy_agents():
        if agent.kind != "prompt":
            continue
        assert "always produce a grounded textual final answer" in agent.definition[
            "instructions"
        ]
        for fixture in agent.fixtures:
            assert len(fixture.expected_tool_calls) == 1
            expected_tool = fixture.expected_tool_calls[0]
            assert f"Call only {expected_tool}" in fixture.input
            if any(
                marker in fixture.input
                for marker in ("location_id", "provider_id", "slot_id")
            ):
                assert "prerequisite lookup" in fixture.input


def test_every_hosted_fixture_command_prefix_maps_to_its_exact_tool() -> None:
    expected = {
        "aiq-003-finance": {
            "account-summary": "account_lookup",
            "transactions": "transaction_search",
            "prepare-budget": "budget_calculation",
        },
        "aiq-004-travel": {
            "search-trip": "flight_search",
            "plan-itinerary": "itinerary",
            "request-booking": "booking",
        },
        "aiq-005-ticket": {
            "read-ticket": "ticket_read",
            "triage-ticket": "customer_context",
            "update-ticket": "ticket_update",
        },
    }
    for agent in load_healthy_agents():
        if agent.kind == "prompt":
            continue
        assert agent.source is not None
        tree = ast.parse((agent.source / "logic.py").read_text(encoding="ascii"))
        instruction_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "INSTRUCTIONS"
                for target in node.targets
            )
        )
        instructions = ast.literal_eval(instruction_node.value)
        fixture_mapping = {
            fixture.input.split(" ", 1)[0]: fixture.expected_tool_calls[0]
            for fixture in agent.fixtures
        }
        assert fixture_mapping == expected[agent.id]
        for prefix, tool_name in fixture_mapping.items():
            assert f"{prefix} -> {tool_name}" in instructions
        assert "Call exactly that one tool" in instructions
        assert "return its result verbatim" in instructions


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


# ---------------------------------------------------------------------------
# Helpers shared by all scenario_runtime behavioral tests
# ---------------------------------------------------------------------------

_WORKTREE_ROOT = Path(__file__).resolve().parents[1]
_HOSTED_SCENARIO_PATHS = [
    _WORKTREE_ROOT / "agents" / "finance-hosted" / "source" / "scenario_runtime.py",
    _WORKTREE_ROOT / "agents" / "travel-hosted" / "source" / "scenario_runtime.py",
    (
        _WORKTREE_ROOT
        / "agents"
        / "support-ticket-hosted-image"
        / "container"
        / "scenario_runtime.py"
    ),
]
_SR_IDS = ["finance", "travel", "ticket"]


def _load_scenario_runtime(path: Path) -> ModuleType:
    return _load_logic(path, f"scenario_runtime_{path.parent.name}")


def _make_config(
    operations: list[dict],
    version_key: str = "test-vk",
    scenario_id: str = "test-scenario",
) -> str:
    return json.dumps(
        {
            "schema_version": "1.0.0",
            "version_key": version_key,
            "scenarios": [{"scenario_id": scenario_id, "operations": operations}],
        },
        separators=(",", ":"),
    )


class _ScenarioCtx:
    """Context-manager that sets AIQ_SCENARIO_CONFIGURATION for a test."""

    def __init__(self, config: str) -> None:
        import os as _os

        self._os = _os
        self._config = config
        self._orig: str | None = None

    def __enter__(self) -> None:
        self._orig = self._os.environ.get("AIQ_SCENARIO_CONFIGURATION")
        self._os.environ["AIQ_SCENARIO_CONFIGURATION"] = self._config

    def __exit__(self, *_: object) -> None:
        if self._orig is None:
            self._os.environ.pop("AIQ_SCENARIO_CONFIGURATION", None)
        else:
            self._os.environ["AIQ_SCENARIO_CONFIGURATION"] = self._orig


def _rt(path: Path, cfg: str, scenario_id: str = "test-scenario") -> object:
    """Load ScenarioRuntime with the given config and activate scenario_id."""
    mod = _load_scenario_runtime(path)
    with _ScenarioCtx(cfg):
        rt = mod.ScenarioRuntime()
    rt.select_scenario(
        scenario_id,
        {
            "agent_name": "aiq-003-finance-live",
            "agent_version": "7",
            "model_deployment": "terra-deployment",
        },
    )
    return rt


# ---------------------------------------------------------------------------
# Schema validation tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_schema_rejects_phase_key(sr_path: Path) -> None:
    """The reviewed schema does not include 'phase'; extra keys cause rejection."""
    bad = json.dumps(
        {
            "schema_version": "1.0.0",
            "phase": "faulted",
            "version_key": "vk",
            "scenarios": [{"scenario_id": "x", "operations": []}],
        }
    )
    mod = _load_scenario_runtime(sr_path)
    with _ScenarioCtx(bad), pytest.raises(RuntimeError, match="invalid"):
        mod.ScenarioRuntime()


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_schema_requires_nonempty_version_key(sr_path: Path) -> None:
    bad = json.dumps(
        {
            "schema_version": "1.0.0",
            "version_key": "",
            "scenarios": [{"scenario_id": "x", "operations": []}],
        }
    )
    mod = _load_scenario_runtime(sr_path)
    with _ScenarioCtx(bad), pytest.raises(RuntimeError, match="invalid"):
        mod.ScenarioRuntime()


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_schema_accepts_valid_config(sr_path: Path) -> None:
    cfg = _make_config([])
    mod = _load_scenario_runtime(sr_path)
    with _ScenarioCtx(cfg):
        rt = mod.ScenarioRuntime()
    assert rt._version_key == "test-vk"


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_schema_rejects_old_operations_key(sr_path: Path) -> None:
    """Schema uses 'scenarios', not the old flat 'operations' key."""
    bad = json.dumps(
        {"schema_version": "1.0.0", "version_key": "vk", "operations": []}
    )
    mod = _load_scenario_runtime(sr_path)
    with _ScenarioCtx(bad), pytest.raises(RuntimeError, match="invalid"):
        mod.ScenarioRuntime()


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_select_scenario_fails_closed_on_unknown_id(sr_path: Path) -> None:
    """select_scenario raises on any scenario_id not present in the config."""
    cfg = _make_config([], scenario_id="aiq-scn-001")
    mod = _load_scenario_runtime(sr_path)
    with _ScenarioCtx(cfg):
        rt = mod.ScenarioRuntime()
    with pytest.raises(RuntimeError, match="Unknown scenario_id"):
        rt.select_scenario("aiq-scn-not-real")


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_select_scenario_is_noop_when_no_config_loaded(sr_path: Path) -> None:
    """With no configuration the runtime is a no-op and any scenario_id is accepted."""
    mod = _load_scenario_runtime(sr_path)
    with _ScenarioCtx(""):
        rt = mod.ScenarioRuntime()
    rt.select_scenario("any-scenario-id")  # must not raise
    dispatched: list[int] = []
    rt.run_tool("lookup", {"account_id": "SYN-1"}, lambda n, a: dispatched.append(1) or "ok")
    assert dispatched, "no-config runtime must dispatch normally"


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_scenario_routing_configured_reflects_loaded_config(sr_path: Path) -> None:
    """scenario_routing_configured is False with no config and True when scenarios are loaded."""
    mod = _load_scenario_runtime(sr_path)

    with _ScenarioCtx(""):
        rt_empty = mod.ScenarioRuntime()
    assert not rt_empty.scenario_routing_configured, (
        "no-config runtime must report routing not configured"
    )

    cfg = _make_config([])
    with _ScenarioCtx(cfg):
        rt_configured = mod.ScenarioRuntime()
    assert rt_configured.scenario_routing_configured, (
        "runtime with scenarios config must report routing configured"
    )


# ---------------------------------------------------------------------------
# Source-patch operations (runtime-variants.yaml catalog)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_tool_arguments_remove_field_entity_id_removes_id_alias(sr_path: Path) -> None:
    """tool_arguments/remove_field/entity_id must remove the first *_id key."""
    cfg = _make_config(
        [{"target": "tool_arguments", "action": "remove_field", "value": "entity_id"}]
    )
    dispatched: list[dict] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(dict(a))
        return "ok"

    rt = _rt(sr_path, cfg)
    rt.run_tool("lookup", {"account_id": "SYN-100", "extra": "x"}, execute)
    assert dispatched, "execute must be called"
    assert "account_id" not in dispatched[0], "entity_id alias must remove *_id key"


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_tool_arguments_replace_value_entity_id_alias(sr_path: Path) -> None:
    """tool_arguments/replace_value entity_id replaces the *_id key value."""
    cfg = _make_config(
        [
            {
                "target": "tool_arguments",
                "action": "replace_value",
                "value": {"field": "entity_id", "value": "synthetic-entity-b"},
            }
        ]
    )
    dispatched: list[dict] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(dict(a))
        return "ok"

    rt = _rt(sr_path, cfg)
    rt.run_tool("lookup", {"ticket_id": "TKT-1001"}, execute)
    assert dispatched
    assert dispatched[0].get("ticket_id") == "synthetic-entity-b"


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_tool_arguments_replace_value_limit_alias(sr_path: Path) -> None:
    """tool_arguments/replace_value limit replaces the *_limit key value."""
    cfg = _make_config(
        [
            {
                "target": "tool_arguments",
                "action": "replace_value",
                "value": {"field": "limit", "value": "not-an-integer"},
            }
        ]
    )
    dispatched: list[dict] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(dict(a))
        return "ok"

    rt = _rt(sr_path, cfg)
    rt.run_tool("search", {"query": "q", "limit": 10}, execute)
    assert dispatched
    assert dispatched[0].get("limit") == "not-an-integer"


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_context_resolver_replace_source_previous_entity(sr_path: Path) -> None:
    """context_resolver/replace_source injects __context_source__ into dispatched args."""
    cfg = _make_config(
        [
            {
                "target": "context_resolver",
                "action": "replace_source",
                "value": "previous_entity",
            }
        ]
    )
    dispatched: list[dict] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(dict(a))
        return "ok"

    rt = _rt(sr_path, cfg)
    rt.run_tool("lookup", {"entity_id": "e1"}, execute)
    assert dispatched
    assert dispatched[0].get("__context_source__") == "previous_entity"


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_context_builder_remove_field_standing_constraint(sr_path: Path) -> None:
    """context_builder/remove_field/standing_constraint removes that key from args."""
    cfg = _make_config(
        [
            {
                "target": "context_builder",
                "action": "remove_field",
                "value": "standing_constraint",
            }
        ]
    )
    dispatched: list[dict] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(dict(a))
        return "ok"

    rt = _rt(sr_path, cfg)
    rt.run_tool("lookup", {"standing_constraint": "premium", "account_id": "SYN-1"}, execute)
    assert dispatched
    assert "standing_constraint" not in dispatched[0]
    assert "account_id" in dispatched[0]


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_context_builder_merge_fixture(sr_path: Path) -> None:
    cfg = _make_config(
        [
            {
                "target": "context_builder",
                "action": "merge_fixture",
                "value": "synthetic-entity-b",
            }
        ]
    )
    dispatched: list[dict] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(dict(a))
        return "ok"

    rt = _rt(sr_path, cfg)
    rt.run_tool("lookup", {"account_id": "SYN-1"}, execute)
    assert dispatched
    assert dispatched[0].get("__merged_fixture__") == "synthetic-entity-b"


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_context_builder_append_fixture(sr_path: Path) -> None:
    cfg = _make_config(
        [
            {
                "target": "context_builder",
                "action": "append_fixture",
                "value": "full_synthetic_dataset",
            }
        ]
    )
    dispatched: list[dict] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(dict(a))
        return "ok"

    rt = _rt(sr_path, cfg)
    rt.run_tool("lookup", {"account_id": "SYN-1"}, execute)
    assert dispatched
    assert dispatched[0].get("__appended_fixture__") == "full_synthetic_dataset"


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_context_builder_duplicate_sections(sr_path: Path) -> None:
    cfg = _make_config(
        [
            {
                "target": "context_builder",
                "action": "duplicate_sections",
                "value": ["history", "tool_results"],
            }
        ]
    )
    dispatched: list[dict] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(dict(a))
        return "ok"

    rt = _rt(sr_path, cfg)
    rt.run_tool("lookup", {"account_id": "SYN-1"}, execute)
    assert dispatched
    assert dispatched[0].get("__duplicate_sections__") == ["history", "tool_results"]


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_query_builder_replace_scope_all_records(sr_path: Path) -> None:
    cfg = _make_config(
        [
            {
                "target": "query_builder",
                "action": "replace_scope",
                "value": "all_records_all_fields",
            }
        ]
    )
    dispatched: list[dict] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(dict(a))
        return "ok"

    rt = _rt(sr_path, cfg)
    rt.run_tool("search", {"account_id": "SYN-1"}, execute)
    assert dispatched
    assert dispatched[0].get("__query_scope__") == "all_records_all_fields"


@pytest.mark.parametrize(
    "operation",
    [
        {"target": "context_resolver", "action": "replace_source", "value": "previous_entity"},
        {"target": "context_builder", "action": "remove_field", "value": "standing_constraint"},
        {"target": "context_builder", "action": "merge_fixture", "value": "synthetic-entity-b"},
        {"target": "context_builder", "action": "append_fixture", "value": "full_synthetic_dataset"},
        {
            "target": "context_builder",
            "action": "duplicate_sections",
            "value": ["history", "tool_results"],
        },
        {"target": "query_builder", "action": "replace_scope", "value": "all_records_all_fields"},
    ],
)
@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_context_mutations_change_actual_model_input(
    sr_path: Path,
    operation: dict,
) -> None:
    rt = _rt(sr_path, _make_config([operation]))
    original = json.dumps(
        {
            "input": "synthetic task",
            "standing_constraint": "preserve this in healthy traffic",
        },
        sort_keys=True,
    )
    mutated = rt.mutate_model_input(original)
    assert mutated != original
    decoded = json.loads(mutated)
    assert decoded["aiq_synthetic_model_context"] == [operation]
    if operation["action"] == "remove_field":
        assert "standing_constraint" not in decoded
    multi_turn = rt.mutate_model_input(
        [{"type": "function_call_output", "call_id": "synthetic", "output": "ok"}]
    )
    assert len(multi_turn) == 2
    assert multi_turn[-1]["role"] == "developer"


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_state_machine_replace_transition_does_not_abort_before_request(sr_path: Path) -> None:
    """state_machine/replace_transition must NOT raise in before_request (bounded loop via model)."""
    cfg = _make_config(
        [
            {
                "target": "state_machine",
                "action": "replace_transition",
                "value": "current_state",
            }
        ]
    )
    rt = _rt(sr_path, cfg)
    rt.before_request()  # must not raise


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_state_machine_replace_transition_run_tool_returns_reentry(sr_path: Path) -> None:
    """state_machine/replace_transition run_tool returns a no-progress re-entry string."""
    cfg = _make_config(
        [
            {
                "target": "state_machine",
                "action": "replace_transition",
                "value": "current_state",
            }
        ]
    )
    dispatched: list[dict] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(dict(a))
        return "ok"

    rt = _rt(sr_path, cfg)
    result = rt.run_tool("lookup", {"account_id": "SYN-1"}, execute)
    assert not dispatched, "state_machine re-entry must not call execute"
    assert "current_state" in result or "no-progress" in result


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_version_sequence_materialize_faulted_returns_fault_no_dispatch(sr_path: Path) -> None:
    """version_key=faulted in materialize list: returns fault JSON, does not call execute."""
    cfg = _make_config(
        [
            {
                "target": "version_sequence",
                "action": "materialize",
                "value": ["faulted", "corrected"],
            }
        ],
        version_key="faulted",
    )
    dispatched: list[dict] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(dict(a))
        return "ok"

    rt = _rt(sr_path, cfg)
    result = rt.run_tool("lookup", {"account_id": "SYN-1"}, execute)
    assert not dispatched, "faulted variant must not call execute"
    parsed = json.loads(result)
    assert parsed.get("status") == "faulted"


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_version_sequence_materialize_corrected_dispatches_normally(sr_path: Path) -> None:
    """version_key=corrected: healthy variant dispatches and returns execute result."""
    cfg = _make_config(
        [
            {
                "target": "version_sequence",
                "action": "materialize",
                "value": ["faulted", "corrected"],
            }
        ],
        version_key="corrected",
    )

    def execute(n: str, a: dict) -> str:
        return "healthy-dispatch"

    rt = _rt(sr_path, cfg)
    result = rt.run_tool("lookup", {"account_id": "SYN-1"}, execute)
    assert result == "healthy-dispatch"


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_version_sequence_materialize_recurred_is_faulted(sr_path: Path) -> None:
    cfg = _make_config(
        [
            {
                "target": "version_sequence",
                "action": "materialize",
                "value": ["faulted", "corrected", "recurred"],
            }
        ],
        version_key="recurred",
    )
    dispatched: list[dict] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(dict(a))
        return "ok"

    rt = _rt(sr_path, cfg)
    result = rt.run_tool("lookup", {}, execute)
    assert not dispatched
    assert "faulted" in json.loads(result).get("status", "")


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_version_sequence_generic_faulted_accepts_named_variant_list(sr_path: Path) -> None:
    """version_key='faulted' returns stable faulted behavior even when the variant list
    uses only named sub-variants (scn-059: ['faulted-window-a','faulted-window-b'])."""
    cfg = _make_config(
        [
            {
                "target": "version_sequence",
                "action": "materialize",
                "value": ["faulted-window-a", "faulted-window-b"],
            }
        ],
        version_key="faulted",
    )
    dispatched: list[int] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(1)
        return "ok"

    rt = _rt(sr_path, cfg)
    result = rt.run_tool("lookup", {"account_id": "SYN-1"}, execute)
    assert not dispatched, "generic faulted must not dispatch"
    parsed = json.loads(result)
    assert parsed.get("status") == "faulted"
    assert parsed.get("variant") == "faulted"
    assert "faulted-window-a" in parsed.get("sequence", [])


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_scenario_isolation_only_selected_ops_execute(sr_path: Path) -> None:
    """Each select_scenario call activates only that scenario's ops; no cross-bleed."""
    ops_a = [{"target": "tool_arguments", "action": "remove_field", "value": "entity_id"}]
    ops_b = [{"target": "context_resolver", "action": "replace_source", "value": "previous_entity"}]
    cfg = json.dumps(
        {
            "schema_version": "1.0.0",
            "version_key": "test-vk",
            "scenarios": [
                {"scenario_id": "aiq-scn-A", "operations": ops_a},
                {"scenario_id": "aiq-scn-B", "operations": ops_b},
            ],
        },
        separators=(",", ":"),
    )
    mod = _load_scenario_runtime(sr_path)
    with _ScenarioCtx(cfg):
        rt = mod.ScenarioRuntime()

    # Request 1: scenario A -- removes entity_id alias.
    rt.select_scenario("aiq-scn-A")
    seen_a: list[dict] = []
    rt.run_tool("lookup", {"account_id": "SYN-1"}, lambda n, a: seen_a.append(dict(a)) or "ok")
    assert seen_a
    assert "account_id" not in seen_a[0], "A must remove entity_id alias"
    assert "__context_source__" not in seen_a[0], "B must not bleed into A"

    # Request 2: scenario B -- injects __context_source__; A's op must be absent.
    rt.select_scenario("aiq-scn-B")
    seen_b: list[dict] = []
    rt.run_tool("lookup", {"account_id": "SYN-1"}, lambda n, a: seen_b.append(dict(a)) or "ok")
    assert seen_b
    assert "__context_source__" in seen_b[0], "B must inject context source"
    assert "account_id" in seen_b[0], "A's remove must not bleed into B"


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_duplicate_dispatch_calls_execute_twice(sr_path: Path) -> None:
    cfg = _make_config(
        [
            {
                "target": "tool_router",
                "action": "duplicate_dispatch",
                "value": "identical_arguments",
            }
        ]
    )
    calls: list[int] = []

    def execute(n: str, a: dict) -> str:
        calls.append(1)
        return "ok"

    rt = _rt(sr_path, cfg)
    rt.run_tool("lookup", {"account_id": "SYN-1"}, execute)
    assert len(calls) == 2, f"expected 2 dispatches, got {len(calls)}"


@pytest.mark.parametrize(
    ("target", "action", "value", "expected"),
    [
        ("tool_router", "bypass_dispatch", "required_tool", "without dispatch"),
        ("operation_handler", "bypass_dispatch", "success_envelope", "without dispatch"),
        ("tool_router", "replace_route", "incompatible_tool", "dispatch-result"),
        ("response_mapper", "patch_return_value", "deterministic contradictory value", "deterministic contradictory value"),
        ("failure_handler", "patch_return_value", "synthetic available result", "synthetic available result"),
        ("response_mapper", "discard_input", "tool_result", "stale response"),
        ("failure_handler", "replace_route", "unsupported_fallback", "failure path"),
        ("failure_handler", "bypass_dispatch", "escalation", "failure path"),
    ],
)
@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_source_patch_control_flow_operations(
    sr_path: Path,
    target: str,
    action: str,
    value: str,
    expected: str,
) -> None:
    cfg = _make_config([{"target": target, "action": action, "value": value}])

    dispatched: list[str] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(n)
        return "dispatch-result"

    rt = _rt(sr_path, cfg)
    result = rt.run_tool("lookup", {"account_id": "SYN-1"}, execute)
    assert expected in result, f"expected {expected!r} in {result!r}"
    if target == "tool_router" and action == "replace_route":
        assert dispatched == ["incompatible_tool"]


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_request_initializer_raise_fixture_error(sr_path: Path) -> None:
    cfg = _make_config(
        [{"target": "request_initializer", "action": "raise_fixture_error", "value": "pre-model-abort"}]
    )
    rt = _rt(sr_path, cfg)
    with pytest.raises(RuntimeError, match="pre-model abort"):
        rt.before_request()


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_model_error_handler_remove_handler(sr_path: Path) -> None:
    cfg = _make_config(
        [{"target": "model_error_handler", "action": "remove_handler", "value": "deterministic_model_error"}]
    )
    rt = _rt(sr_path, cfg)
    with pytest.raises(RuntimeError, match="model failure"):
        rt.before_model()


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_response_orchestrator_raise_fixture_error(sr_path: Path) -> None:
    cfg = _make_config(
        [{"target": "response_orchestrator", "action": "raise_fixture_error", "value": "post-tool-abort"}]
    )
    rt = _rt(sr_path, cfg)
    with pytest.raises(RuntimeError, match="post-tool abort"):
        rt.run_tool("lookup", {"account_id": "SYN-1"}, lambda n, a: "ok")


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_failure_fixture_expose_symptoms_in_finalize_output(sr_path: Path) -> None:
    cfg = _make_config(
        [
            {
                "target": "failure_fixture",
                "action": "expose_symptoms",
                "value": ["symptom-a", "symptom-b"],
            }
        ]
    )
    rt = _rt(sr_path, cfg)
    out = rt.finalize_output("some output")
    parsed = json.loads(out)
    assert parsed == ["symptom-a", "symptom-b"]


# ---------------------------------------------------------------------------
# Endpoint-fault operations (endpoint-faults.yaml catalog)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_synthetic_fixture_configure_response_error(sr_path: Path) -> None:
    """configure_response with status:error returns structured error without dispatch."""
    cfg = _make_config(
        [
            {
                "target": "synthetic_tool_fixture",
                "action": "configure_response",
                "value": {"status": "error", "transport_status": 200},
            }
        ]
    )
    dispatched: list[dict] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(dict(a))
        return "ok"

    rt = _rt(sr_path, cfg)
    result = rt.run_tool("lookup", {"account_id": "SYN-1"}, execute)
    assert not dispatched, "configure_response must not call execute"
    parsed = json.loads(result)
    assert parsed.get("status") == "error"
    assert parsed.get("transport_status") == 200


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_synthetic_fixture_configure_response_partial_failure(sr_path: Path) -> None:
    cfg = _make_config(
        [
            {
                "target": "synthetic_tool_fixture",
                "action": "configure_response",
                "value": {"complete": False, "failed_items": ["synthetic-item-b"]},
            }
        ]
    )
    dispatched: list[dict] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(dict(a))
        return "ok"

    rt = _rt(sr_path, cfg)
    result = rt.run_tool("lookup", {}, execute)
    assert not dispatched
    parsed = json.loads(result)
    assert parsed.get("complete") is False
    assert "synthetic-item-b" in parsed.get("failed_items", [])


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_synthetic_fixture_configure_response_permanent_failure(sr_path: Path) -> None:
    cfg = _make_config(
        [
            {
                "target": "synthetic_tool_fixture",
                "action": "configure_response",
                "value": "permanent_failure",
            }
        ]
    )
    rt = _rt(sr_path, cfg)
    result = rt.run_tool("lookup", {}, lambda n, a: "ok")
    parsed = json.loads(result)
    assert parsed.get("permanent") is True
    assert parsed.get("status") == "error"


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_synthetic_fixture_remove_field_result(sr_path: Path) -> None:
    """remove_field/result executes normally then removes the 'result' key from the JSON response."""
    cfg = _make_config(
        [{"target": "synthetic_tool_fixture", "action": "remove_field", "value": "result"}]
    )

    def execute(n: str, a: dict) -> str:
        return json.dumps({"result": "SYN-100 data", "status": "ok"})

    rt = _rt(sr_path, cfg)
    result = rt.run_tool("lookup", {"account_id": "SYN-1"}, execute)
    parsed = json.loads(result)
    assert "result" not in parsed
    assert parsed.get("status") == "ok"


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_synthetic_fixture_configure_sequence_transient_then_success(sr_path: Path) -> None:
    """configure_sequence [transient_failure, success]: first call returns error, second calls execute."""
    cfg = _make_config(
        [
            {
                "target": "synthetic_tool_fixture",
                "action": "configure_sequence",
                "value": ["transient_failure", "success"],
            }
        ]
    )
    dispatched: list[dict] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(dict(a))
        return "success-result"

    rt = _rt(sr_path, cfg)
    first = rt.run_tool("lookup", {"account_id": "SYN-1"}, execute)
    assert not dispatched, "first call must return transient failure without dispatch"
    first_parsed = json.loads(first)
    assert first_parsed.get("transient") is True

    second = rt.run_tool("lookup", {"account_id": "SYN-1"}, execute)
    assert dispatched, "second call must dispatch"
    assert second == "success-result"


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_synthetic_fixture_configure_parallelizable_delays_actual_120ms(sr_path: Path) -> None:
    """configure_parallelizable_delays [120, 120] must sleep >=120 ms per call."""
    import time as _time

    cfg = _make_config(
        [
            {
                "target": "synthetic_tool_fixture",
                "action": "configure_parallelizable_delays",
                "value": [120, 120],
            }
        ]
    )

    rt = _rt(sr_path, cfg)
    start = _time.monotonic()
    rt.run_tool("lookup", {"account_id": "SYN-1"}, lambda n, a: "ok")
    elapsed = _time.monotonic() - start
    assert elapsed >= 0.100, f"expected >=100ms, got {elapsed*1000:.0f}ms"


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_synthetic_fixture_configure_post_completion_delay_250ms(sr_path: Path) -> None:
    """configure_post_completion_delay 250 must sleep >=250 ms after dispatch."""
    import time as _time

    cfg = _make_config(
        [
            {
                "target": "synthetic_tool_fixture",
                "action": "configure_post_completion_delay",
                "value": 250,
            }
        ]
    )
    dispatched: list[dict] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(dict(a))
        return "dispatch-result"

    rt = _rt(sr_path, cfg)
    start = _time.monotonic()
    result = rt.run_tool("lookup", {"account_id": "SYN-1"}, execute)
    elapsed = _time.monotonic() - start
    assert dispatched, "dispatch must happen before delay"
    assert result == "dispatch-result"
    assert elapsed >= 0.200, f"expected >=200ms, got {elapsed*1000:.0f}ms"


@pytest.mark.parametrize(
    ("case", "is_healthy", "expected_key"),
    [
        ("guardrail-bypass-probe", False, "guardrail_triggered"),
        ("no-confirmation", False, "action_without_confirmation"),
        ("malformed-approval", False, "malformed_approval"),
        ("cross-account-synthetic-record", False, "cross_account_access"),
        ("correlated-child-failure", False, "nested_failure"),
        # scn-056: healthy control -- dispatches and succeeds
        ("zero-token-outer-successful-child", True, "ok"),
        # scn-057: healthy control -- dispatches and succeeds
        ("handled-child-failure", True, "ok"),
    ],
)
@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_endpoint_request_set_case_all_seven_values(
    sr_path: Path,
    case: str,
    is_healthy: bool,
    expected_key: str,
) -> None:
    """endpoint_request/set_case covers all seven catalog values with distinct behavior."""
    cfg = _make_config(
        [{"target": "endpoint_request", "action": "set_case", "value": case}]
    )
    dispatched: list[dict] = []

    def execute(n: str, a: dict) -> str:
        dispatched.append(dict(a))
        return "dispatch-ok"

    rt = _rt(sr_path, cfg)
    result = rt.run_tool("lookup", {"account_id": "SYN-1"}, execute)
    parsed = json.loads(result)
    assert parsed.get("case") == case, f"expected case={case!r} in result"
    assert parsed.get("status") == expected_key, (
        f"case={case!r}: expected status={expected_key!r}, got {parsed.get('status')!r}"
    )
    if is_healthy:
        assert dispatched, f"healthy control {case!r} must dispatch"
        assert "dispatch_result" in parsed
    else:
        assert not dispatched, f"fault case {case!r} must not dispatch"


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_endpoint_request_set_case_rejects_unsupported_value(sr_path: Path) -> None:
    cfg = _make_config(
        [{"target": "endpoint_request", "action": "set_case", "value": "not-a-real-case"}]
    )
    rt = _rt(sr_path, cfg)
    with pytest.raises(RuntimeError, match="Unsupported"):
        rt.run_tool("lookup", {}, lambda n, a: "ok")


# ---------------------------------------------------------------------------
# Docker image context, import, and startup checks
# ---------------------------------------------------------------------------

_CONTAINER_DIR = _WORKTREE_ROOT / "agents" / "support-ticket-hosted-image" / "container"


def test_dockerfile_copies_scenario_runtime() -> None:
    dockerfile = (_CONTAINER_DIR / "Dockerfile").read_text(encoding="ascii")
    assert "scenario_runtime.py" in dockerfile, (
        "Dockerfile must COPY scenario_runtime.py into the image context"
    )


def test_dockerfile_context_all_copied_files_exist() -> None:
    """Every file listed in a Dockerfile COPY instruction must exist in the build context."""
    dockerfile = (_CONTAINER_DIR / "Dockerfile").read_text(encoding="ascii")
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY"):
            continue
        parts = stripped.split()
        # COPY [--chown=...] src... dest  -- skip --chown flag and last element (dest)
        sources = [p for p in parts[1:-1] if not p.startswith("--")]
        for src in sources:
            assert (_CONTAINER_DIR / src).is_file(), (
                f"Dockerfile COPY references {src!r} which does not exist in the build context"
            )


def test_scenario_runtime_imports_with_stdlib_only() -> None:
    """scenario_runtime.py must import successfully using only stdlib (no Azure/OTel)."""
    import sys

    path = _CONTAINER_DIR / "scenario_runtime.py"
    spec = importlib.util.spec_from_file_location("_sr_stdlib_check", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Remove non-stdlib modules that should NOT be required by scenario_runtime
    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k.startswith(("azure", "opentelemetry"))}
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.update(saved)
    assert hasattr(mod, "ScenarioRuntime")


def test_container_main_references_model_backed_agent() -> None:
    """main.py must reference ModelBackedAgent (startup contract check)."""
    text = (_CONTAINER_DIR / "main.py").read_text(encoding="ascii")
    assert "ModelBackedAgent" in text, "main.py must import/use ModelBackedAgent on startup"


def test_container_model_runtime_references_scenario_runtime() -> None:
    """model_runtime.py must import ScenarioRuntime from scenario_runtime."""
    text = (_CONTAINER_DIR / "model_runtime.py").read_text(encoding="ascii")
    assert "from scenario_runtime import ScenarioRuntime" in text


def test_ticket_container_entrypoint_imports_and_starts_from_docker_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = ROOT / "agents" / "support-ticket-hosted-image" / "container"
    starts: list[bool] = []

    class Host:
        def response_handler(self, handler):
            self.handler = handler
            return handler

        def run(self):
            starts.append(True)

    responses = ModuleType("azure.ai.agentserver.responses")
    responses.CreateResponse = type("CreateResponse", (), {})
    responses.ResponseContext = type("ResponseContext", (), {})
    responses.ResponsesAgentServerHost = Host
    responses.TextResponse = type("TextResponse", (), {})
    projects = ModuleType("azure.ai.projects")
    projects.AIProjectClient = type("AIProjectClient", (), {})
    identity = ModuleType("azure.identity")
    identity.DefaultAzureCredential = type("DefaultAzureCredential", (), {})
    azure = ModuleType("azure")
    azure.__path__ = []
    azure_ai = ModuleType("azure.ai")
    azure_ai.__path__ = []
    agentserver = ModuleType("azure.ai.agentserver")
    agentserver.__path__ = []
    for name, module in {
        "azure": azure,
        "azure.ai": azure_ai,
        "azure.ai.agentserver": agentserver,
        "azure.ai.agentserver.responses": responses,
        "azure.ai.projects": projects,
        "azure.identity": identity,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    for name in ("logic", "model_runtime", "scenario_runtime"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(container))
    monkeypatch.setenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "synthetic-model")
    monkeypatch.setenv(
        "AZURE_AI_PROJECT_ENDPOINT",
        "https://synthetic.example.invalid/api/projects/test",
    )
    runpy.run_path(str(container / "main.py"), run_name="__main__")
    assert starts == [True]


# ---------------------------------------------------------------------------
# Hosting-integration tests
# ---------------------------------------------------------------------------


def test_hosted_agents_have_scenario_runtime_in_assets() -> None:
    agents = load_healthy_agents()
    for agent in agents:
        if agent.kind == "prompt":
            continue
        assert agent.source is not None
        assert (agent.source / "scenario_runtime.py").is_file(), (
            f"{agent.id}: scenario_runtime.py missing from {agent.source}"
        )


def test_hosted_agents_representative_tools_populated_from_manifest() -> None:
    agents = load_healthy_agents()
    finance = next(a for a in agents if a.id == "aiq-003-finance")
    assert set(finance.representative_tools) == {
        "account_lookup",
        "transaction_search",
        "budget_calculation",
    }
    travel = next(a for a in agents if a.id == "aiq-004-travel")
    assert "flight_search" in travel.representative_tools
    ticket = next(a for a in agents if a.id == "aiq-005-ticket")
    assert "ticket_read" in ticket.representative_tools
    # Prompt agents may have no representative_tools entry in manifest
    assert all(
        agent.representative_tools or agent.kind == "prompt" for agent in agents
    )


# ---------------------------------------------------------------------------
# Nested OTel span-processor tests for endpoint_request cases 055, 056, 057
# ---------------------------------------------------------------------------


def _make_span_harness():
    """Return (tracer, exporter) backed by an isolated in-memory TracerProvider."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def _rt_with_spans(path: Path, cfg: str, tracer: object, scenario_id: str = "test-scenario") -> object:
    """Instantiate ScenarioRuntime with an injected tracer for span capture."""
    mod = _load_scenario_runtime(path)
    with _ScenarioCtx(cfg):
        rt = mod.ScenarioRuntime(_tracer=tracer)
    rt.select_scenario(
        scenario_id,
        {
            "agent_name": "aiq-003-finance-live",
            "agent_version": "7",
            "model_deployment": "terra-deployment",
        },
    )
    return rt


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_correlated_child_failure_emits_nested_spans(sr_path: Path) -> None:
    """scn-055: endpoint.request (OK) wraps endpoint.child_request (ERROR); no dispatch."""
    from opentelemetry.trace import StatusCode
    tracer, exporter = _make_span_harness()
    cfg = _make_config(
        [{"target": "endpoint_request", "action": "set_case", "value": "correlated-child-failure"}]
    )
    rt = _rt_with_spans(sr_path, cfg, tracer)
    dispatched: list[int] = []
    rt.run_tool("lookup", {"account_id": "SYN-1"}, lambda n, a: dispatched.append(1) or "ok")
    assert not dispatched, "correlated-child-failure must not dispatch"

    by_name = {s.name: s for s in exporter.get_finished_spans()}
    assert "endpoint.request" in by_name, "parent span endpoint.request must be emitted"
    assert "endpoint.child_request" in by_name, "child span endpoint.child_request must be emitted"

    parent = by_name["endpoint.request"]
    child = by_name["endpoint.child_request"]
    assert child.parent is not None, "child span must have a parent context"
    assert child.parent.span_id == parent.context.span_id, "child must be nested inside parent"
    assert child.status.status_code == StatusCode.ERROR, "child span must be ERROR"
    assert parent.status.status_code == StatusCode.OK, "parent span must be OK"
    assert parent.attributes.get("endpoint.case") == "correlated-child-failure"
    assert parent.attributes.get("endpoint.nested_failure") is True


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_zero_token_outer_emits_parent_span_with_zero_token_attributes(sr_path: Path) -> None:
    """scn-056: zero-token outer span wraps a successful child model span."""
    from opentelemetry.trace import StatusCode
    tracer, exporter = _make_span_harness()
    cfg = _make_config(
        [{"target": "endpoint_request", "action": "set_case", "value": "zero-token-outer-successful-child"}]
    )
    rt = _rt_with_spans(sr_path, cfg, tracer)
    dispatched: list[int] = []

    def _execute(n: str, a: dict) -> str:
        dispatched.append(1)
        return "child-ok"

    result = rt.run_tool("lookup", {"account_id": "SYN-1"}, _execute)
    assert dispatched, "zero-token-outer-successful-child must dispatch (healthy control)"
    assert json.loads(result).get("dispatch_result") == "child-ok"

    by_name = {s.name: s for s in exporter.get_finished_spans()}
    assert "endpoint.request" in by_name, "parent span endpoint.request must be emitted"
    assert "model.responses.create" in by_name, "successful child model span must be emitted"
    parent = by_name["endpoint.request"]
    child = by_name["model.responses.create"]
    assert child.parent is not None
    assert child.parent.span_id == parent.context.span_id
    assert child.status.status_code == StatusCode.OK
    assert child.attributes.get("gen_ai.operation.name") == "chat"
    assert child.attributes.get("gen_ai.agent.name") == "aiq-003-finance-live"
    assert child.attributes.get("gen_ai.agent.version") == "7"
    assert child.attributes.get("gen_ai.request.model") == "terra-deployment"
    assert child.attributes.get("gen_ai.usage.input_tokens") == 1
    assert child.attributes.get("gen_ai.usage.output_tokens") == 1
    assert parent.status.status_code == StatusCode.OK, "parent span must be OK"
    assert parent.attributes.get("gen_ai.usage.input_tokens") == 0
    assert parent.attributes.get("gen_ai.usage.output_tokens") == 0
    assert parent.attributes.get("endpoint.case") == "zero-token-outer-successful-child"

    spans = exporter.get_finished_spans()
    rows = []
    for span in spans:
        attributes = span.attributes
        rows.append(
            {
                "timestamp": datetime.fromtimestamp(
                    span.start_time / 1_000_000_000,
                    tz=timezone.utc,
                ),
                "operation_id": f"{span.context.trace_id:032x}",
                "span_id": f"{span.context.span_id:016x}",
                "parent_id": (
                    f"{span.parent.span_id:016x}" if span.parent is not None else ""
                ),
                "span_name": attributes.get("gen_ai.operation.name", span.name),
                "span_agent_name": attributes.get("gen_ai.agent.name", ""),
                "span_agent_version": attributes.get("gen_ai.agent.version", ""),
                "span_model": attributes.get("gen_ai.request.model", ""),
                "agent_name": "aiq-003-finance-live",
                "agent_version": "7",
                "invocation_id": ["invoke-056"],
                "response_id": [],
                "hosted_response_id": [],
                "session_id": [],
            }
        )
    observed = datetime.fromtimestamp(
        min(span.start_time for span in spans) / 1_000_000_000,
        tz=timezone.utc,
    )
    projected = correlate_complete_traces(
        rows,
        agent="aiq-003-finance-live",
        version="7",
        expectations=[
            TelemetryExpectation(
                "invoke-056",
                None,
                None,
                "terra-deployment",
                frozenset({"chat"}),
            )
        ],
        start=observed - timedelta(seconds=1),
        end=observed + timedelta(seconds=1),
    )
    assert projected is not None
    assert len(projected) == 1


@pytest.mark.parametrize("sr_path", _HOSTED_SCENARIO_PATHS, ids=_SR_IDS)
def test_handled_child_failure_emits_nested_spans_and_dispatches(sr_path: Path) -> None:
    """scn-057: endpoint.child_request (ERROR) + recovery dispatch; parent OK, recovered."""
    from opentelemetry.trace import StatusCode
    tracer, exporter = _make_span_harness()
    cfg = _make_config(
        [{"target": "endpoint_request", "action": "set_case", "value": "handled-child-failure"}]
    )
    rt = _rt_with_spans(sr_path, cfg, tracer)
    dispatched: list[int] = []

    def _execute(n: str, a: dict) -> str:
        dispatched.append(1)
        return "recovery-ok"

    result = rt.run_tool("lookup", {"account_id": "SYN-1"}, _execute)
    assert dispatched, "handled-child-failure must dispatch (recovery path)"
    assert json.loads(result).get("dispatch_result") == "recovery-ok"

    by_name = {s.name: s for s in exporter.get_finished_spans()}
    assert "endpoint.request" in by_name, "parent span must be emitted"
    assert "endpoint.child_request" in by_name, "synthetic failing child span must be emitted"

    parent = by_name["endpoint.request"]
    child = by_name["endpoint.child_request"]
    assert child.parent is not None, "child span must have a parent context"
    assert child.parent.span_id == parent.context.span_id, "child must be nested inside parent"
    assert child.status.status_code == StatusCode.ERROR, "child span must be ERROR"
    assert parent.status.status_code == StatusCode.OK, "parent span must be OK (recovered)"
    assert parent.attributes.get("endpoint.parent.status") == "recovered"
    assert parent.attributes.get("endpoint.case") == "handled-child-failure"
