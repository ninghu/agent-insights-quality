from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from agent_insights_quality.contracts import EXPECTED_AGENTS, ROOT
from agent_insights_quality.runtime import (
    HealthyFixture,
    LiveTelemetryEvidence,
    RuntimeContractError,
    load_fixtures,
)


AgentKind = Literal["prompt", "hosted_code", "hosted_custom_container"]


@dataclass(frozen=True, slots=True)
class HealthyAgent:
    id: str
    kind: AgentKind
    root: Path
    definition: dict[str, Any]
    fixtures: tuple[HealthyFixture, ...]

    @property
    def source(self) -> Path | None:
        path = self.root / ("container" if self.kind == "hosted_custom_container" else "source")
        return path if path.is_dir() else None

    def definition_for_deployment(
        self,
        *,
        model_deployment_name: str | None = None,
        project_endpoint: str | None = None,
    ) -> dict[str, Any]:
        resolved = json.loads(json.dumps(self.definition))
        if not model_deployment_name or not model_deployment_name.strip():
            raise RuntimeContractError(
                "Healthy definitions require a runtime model deployment name."
            )
        if self.kind == "prompt":
            if resolved.get("model") != "${AIQ_MODEL_DEPLOYMENT_NAME}":
                raise RuntimeContractError(f"Prompt model placeholder changed: {self.id}")
            resolved["model"] = model_deployment_name.strip()
        else:
            environment = resolved.get("environment_variables")
            if (
                not isinstance(environment, dict)
                or environment.get("AZURE_AI_MODEL_DEPLOYMENT_NAME")
                != "${AIQ_MODEL_DEPLOYMENT_NAME}"
                or environment.get("AZURE_AI_PROJECT_ENDPOINT")
                != "${AIQ_PROJECT_ENDPOINT}"
            ):
                raise RuntimeContractError(f"Hosted runtime placeholders changed: {self.id}")
            if not project_endpoint or not project_endpoint.strip():
                raise RuntimeContractError(
                    "Hosted definitions require a runtime Foundry project endpoint."
                )
            environment["AZURE_AI_MODEL_DEPLOYMENT_NAME"] = model_deployment_name.strip()
            environment["AZURE_AI_PROJECT_ENDPOINT"] = project_endpoint.strip()
        return resolved


def load_healthy_agents() -> tuple[HealthyAgent, ...]:
    agents = []
    for root in sorted((ROOT / "agents").iterdir()):
        if not root.is_dir():
            continue
        manifest_path = root / "manifest.yaml"
        definition_path = root / "definition.json"
        fixture_path = root / "healthy-traffic.json"
        if not manifest_path.is_file():
            continue
        from agent_insights_quality.contracts import load_data

        manifest = load_data(manifest_path)
        agent_id = str(manifest["id"])
        kind = str(manifest["agent_type"])
        if agent_id not in EXPECTED_AGENTS or EXPECTED_AGENTS[agent_id] != kind:
            raise RuntimeContractError(f"Unexpected healthy agent contract: {agent_id}")
        if manifest["status"] != "active":
            raise RuntimeContractError(f"Healthy agent is not active: {agent_id}")
        try:
            definition = json.loads(definition_path.read_text(encoding="ascii"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeContractError(
                f"Healthy agent definition is invalid: {definition_path}"
            ) from error
        if not isinstance(definition, dict):
            raise RuntimeContractError(f"Healthy agent definition is not an object: {agent_id}")
        if kind == "prompt" and definition.get("kind") != "prompt":
            raise RuntimeContractError(f"Prompt definition kind mismatch: {agent_id}")
        if kind != "prompt" and definition.get("kind") != "hosted":
            raise RuntimeContractError(f"Hosted definition kind mismatch: {agent_id}")
        fixtures = load_fixtures(fixture_path)
        if kind != "prompt":
            source = root / ("container" if kind == "hosted_custom_container" else "source")
            required_files = {
                "logic.py",
                "main.py",
                "model_runtime.py",
                "requirements.txt",
            }
            if not all((source / filename).is_file() for filename in required_files):
                raise RuntimeContractError(
                    f"Hosted model/tool implementation is incomplete: {agent_id}"
                )
            main_text = (source / "main.py").read_text(encoding="ascii")
            runtime_text = (source / "model_runtime.py").read_text(encoding="ascii")
            requirements = (source / "requirements.txt").read_text(encoding="ascii")
            if (
                "ModelBackedAgent" not in main_text
                or "model.responses.create" not in runtime_text
                or "gen_ai.tool.name" not in runtime_text
                or "azure-ai-projects" not in requirements
                or "opentelemetry-api" not in requirements
                or any(not fixture.expected_tool_calls for fixture in fixtures)
            ):
                raise RuntimeContractError(
                    f"Hosted model/tool tracing contract is incomplete: {agent_id}"
                )
        agents.append(
            HealthyAgent(
                id=agent_id,
                kind=cast(AgentKind, kind),
                root=root,
                definition=definition,
                fixtures=fixtures,
            )
        )
    if {agent.id for agent in agents} != set(EXPECTED_AGENTS):
        raise RuntimeContractError("Healthy implementation registry is incomplete.")
    return tuple(agents)


def require_live_telemetry_qualification(
    evidence: Sequence[LiveTelemetryEvidence],
) -> None:
    agents = load_healthy_agents()
    by_agent: dict[str, list[LiveTelemetryEvidence]] = {}
    for item in evidence:
        by_agent.setdefault(item.agent_id, []).append(item)
    if set(by_agent) != {agent.id for agent in agents}:
        raise RuntimeContractError(
            "Live telemetry qualification must cover every healthy agent."
        )

    operation_ids: set[str] = set()
    for agent in agents:
        expected = {fixture.id: fixture for fixture in agent.fixtures}
        actual_items = by_agent[agent.id]
        actual = {item.fixture_id: item for item in actual_items}
        if len(actual) != len(actual_items) or set(actual) != set(expected):
            raise RuntimeContractError(
                f"Live telemetry fixture coverage is incomplete for {agent.id}."
            )
        versions = {item.agent_version for item in actual_items}
        if len(versions) != 1 or not next(iter(versions)):
            raise RuntimeContractError(
                f"Live telemetry spans multiple versions for {agent.id}."
            )
        for fixture_id, item in actual.items():
            if (
                not item.agent_name.startswith(agent.id)
                or not item.response_id
                or not item.operation_id
                or not {"agent", "model"}.issubset(item.span_kinds)
            ):
                raise RuntimeContractError(
                    f"Live telemetry identity or core spans are incomplete for {fixture_id}."
                )
            if item.operation_id in operation_ids:
                raise RuntimeContractError(
                    "Live telemetry operation IDs must be unique per healthy request."
                )
            operation_ids.add(item.operation_id)
            fixture = expected[fixture_id]
            if tuple(item.tool_names) != fixture.expected_tool_calls:
                raise RuntimeContractError(
                    f"Live telemetry tool sequence differs for {fixture_id}."
                )
            if agent.kind != "prompt":
                if "tool" not in item.span_kinds:
                    raise RuntimeContractError(
                        f"Hosted live telemetry lacks a tool span for {fixture_id}."
                    )
                expected_arguments = tuple(
                    fixture.tool_outputs[name]["arguments"]
                    for name in fixture.expected_tool_calls
                )
                expected_results = tuple(
                    fixture.tool_outputs[name]["result"]
                    for name in fixture.expected_tool_calls
                )
                if (
                    item.tool_arguments != expected_arguments
                    or item.tool_results != expected_results
                ):
                    raise RuntimeContractError(
                        f"Live telemetry tool inputs or outputs differ for {fixture_id}."
                    )
