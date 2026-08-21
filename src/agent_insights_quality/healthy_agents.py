from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from agent_insights_quality.contracts import EXPECTED_AGENTS, ROOT
from agent_insights_quality.runtime import (
    HealthyFixture,
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
    ) -> dict[str, Any]:
        resolved = json.loads(json.dumps(self.definition))
        if self.kind != "prompt":
            if model_deployment_name is not None:
                raise RuntimeContractError(
                    "A model deployment name is valid only for prompt definitions."
                )
            return resolved
        if not model_deployment_name or not model_deployment_name.strip():
            raise RuntimeContractError(
                "Prompt definitions require a runtime model deployment name."
            )
        if resolved.get("model") != "${AIQ_MODEL_DEPLOYMENT_NAME}":
            raise RuntimeContractError(f"Prompt model placeholder changed: {self.id}")
        resolved["model"] = model_deployment_name.strip()
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
        if kind != "prompt" and any(
            fixture.expected_tool_calls or fixture.tool_outputs for fixture in fixtures
        ):
            raise RuntimeContractError(
                f"Hosted healthy fixtures cannot claim client-executed tools: {agent_id}"
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
