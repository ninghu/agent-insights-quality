from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from agent_insights_quality.contracts import EXPECTED_AGENTS, ROOT
from agent_insights_quality.agent_runtime import (
    DeploymentReceipt,
    HealthyFixture,
    InvocationReceipt,
    LiveSpanEvidence,
    LiveTelemetryEvidence,
    RuntimeContractError,
    load_fixtures,
    validate_deployment_receipt,
    validate_telemetry_identifiers,
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
    *,
    run_id: str,
    window_start: datetime,
    window_end: datetime,
    deployments: Sequence[DeploymentReceipt],
    invocations: Sequence[InvocationReceipt],
    evidence: Sequence[LiveTelemetryEvidence],
) -> None:
    agents = load_healthy_agents()
    if (
        not run_id
        or window_start.tzinfo is None
        or window_end.tzinfo is None
        or window_start.utcoffset() != timedelta(0)
        or window_end.utcoffset() != timedelta(0)
        or window_start >= window_end
        or window_end - window_start > timedelta(hours=1)
    ):
        raise RuntimeContractError(
            "Live telemetry qualification requires a non-empty run ID and a bounded UTC window."
        )

    deployment_by_agent: dict[str, DeploymentReceipt] = {}
    for agent in agents:
        matches = [
            receipt
            for receipt in deployments
            if receipt.agent_name == agent.id
            or receipt.agent_name.startswith(f"{agent.id}-")
        ]
        if len(matches) != 1:
            raise RuntimeContractError(
                f"Live qualification requires one current deployment for {agent.id}."
            )
        receipt = matches[0]
        validate_deployment_receipt(receipt)
        if (
            receipt.run_id != run_id
            or receipt.status != "active"
            or receipt.agent_type != agent.kind
        ):
            raise RuntimeContractError(
                f"Live qualification deployment is stale or inactive for {agent.id}."
            )
        deployment_by_agent[agent.id] = receipt
    if len(deployments) != len(agents):
        raise RuntimeContractError(
            "Live qualification received deployments outside the five healthy agents."
        )

    expected_invocations: dict[tuple[str, str], InvocationReceipt] = {}
    response_ids: set[str] = set()
    request_ids: set[str] = set()
    invocation_ids: set[str] = set()
    hosted_session_ids: set[str] = set()
    for agent in agents:
        deployment = deployment_by_agent[agent.id]
        fixture_by_id = {fixture.id: fixture for fixture in agent.fixtures}
        fixtures = set(fixture_by_id)
        matching = [
            receipt
            for receipt in invocations
            if receipt.agent_name == deployment.agent_name
            and receipt.agent_version == deployment.agent_version
            and receipt.fixture_id in fixtures
        ]
        if len(matching) != len(fixtures) or {item.fixture_id for item in matching} != fixtures:
            raise RuntimeContractError(
                f"Live qualification invocation receipts are incomplete for {agent.id}."
            )
        for receipt in matching:
            key = (agent.id, receipt.fixture_id)
            if (
                key in expected_invocations
                or not receipt.response_id
                or receipt.response_id in response_ids
                or not receipt.request_id
                or receipt.request_id in request_ids
                or (
                    receipt.invocation_id is not None
                    and (
                        not receipt.invocation_id
                        or receipt.invocation_id in invocation_ids
                    )
                )
                or (
                    agent.kind != "prompt"
                    and (
                        not receipt.session_id
                        or receipt.session_id in hosted_session_ids
                    )
                )
                or (agent.kind == "prompt" and receipt.session_id is not None)
                or fixture_by_id[receipt.fixture_id].output_contains
                not in receipt.output_text
                or (
                    agent.kind == "prompt"
                    and receipt.called_tools
                    != fixture_by_id[receipt.fixture_id].expected_tool_calls
                )
            ):
                raise RuntimeContractError(
                    f"Live qualification invocation receipts are invalid for {agent.id}."
                )
            expected_invocations[key] = receipt
            response_ids.add(receipt.response_id)
            request_ids.add(receipt.request_id)
            if receipt.invocation_id is not None:
                invocation_ids.add(receipt.invocation_id)
            if receipt.session_id is not None:
                hosted_session_ids.add(receipt.session_id)
    if len(invocations) != len(expected_invocations):
        raise RuntimeContractError(
            "Live qualification received stale or unrelated invocation receipts."
        )

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
        deployment = deployment_by_agent[agent.id]
        for fixture_id, item in actual.items():
            validate_telemetry_identifiers(item)
            invocation = expected_invocations[(agent.id, fixture_id)]
            if (
                item.run_id != run_id
                or item.agent_name != deployment.agent_name
                or item.agent_version != deployment.agent_version
                or item.response_id != invocation.response_id
                or item.invocation_id != invocation.invocation_id
                or item.request_id != invocation.request_id
                or item.session_id != invocation.session_id
                or item.operation_id
                in {
                    identifier
                    for identifier in (
                        invocation.response_id,
                        invocation.invocation_id,
                        invocation.request_id,
                        invocation.session_id,
                    )
                    if identifier is not None
                }
                or item.observed_at.tzinfo is None
                or item.observed_at.utcoffset() != timedelta(0)
                or not window_start <= item.observed_at < window_end
                or any(
                    span.observed_at.tzinfo is None
                    or span.observed_at.utcoffset() != timedelta(0)
                    or not window_start <= span.observed_at < window_end
                    for span in item.spans
                )
            ):
                raise RuntimeContractError(
                    f"Live telemetry is stale or not correlated to the current receipt for {fixture_id}."
                )
            if item.operation_id in operation_ids:
                raise RuntimeContractError(
                    "Live telemetry operation IDs must be unique per healthy request."
                )
            operation_ids.add(item.operation_id)
            fixture = expected[fixture_id]
            _validate_span_tree(item, fixture)


def _validate_span_tree(
    item: LiveTelemetryEvidence,
    fixture: HealthyFixture,
) -> None:
    agent_spans = [span for span in item.spans if span.kind == "agent"]
    model_spans = [span for span in item.spans if span.kind == "model"]
    tool_spans = [span for span in item.spans if span.kind == "tool"]
    if (
        len(agent_spans) != 1
        or agent_spans[0].parent_span_id is not None
        or not agent_spans[0].name
        or not model_spans
        or any(not span.name for span in model_spans)
        or len(agent_spans) + len(model_spans) + len(tool_spans) != len(item.spans)
    ):
        raise RuntimeContractError(
            f"Live telemetry core span hierarchy is incomplete for {fixture.id}."
        )
    root_id = agent_spans[0].span_id
    span_by_id = {span.span_id: span for span in item.spans}
    if any(
        not _is_descendant(span, root_id, span_by_id)
        for span in (*model_spans, *tool_spans)
    ):
        raise RuntimeContractError(
            f"Live telemetry parent-child hierarchy differs for {fixture.id}."
        )

    tool_names = tuple(span.tool_name for span in tool_spans)
    if tool_names != fixture.expected_tool_calls:
        raise RuntimeContractError(
            f"Live telemetry tool sequence differs for {fixture.id}."
        )
    expected_arguments = tuple(
        _canonical_json(fixture.tool_outputs[name]["arguments"])
        for name in fixture.expected_tool_calls
    )
    expected_results = tuple(
        _canonical_json(fixture.tool_outputs[name]["result"])
        for name in fixture.expected_tool_calls
    )
    if (
        tuple(_canonical_json(span.tool_arguments) for span in tool_spans)
        != expected_arguments
        or tuple(_canonical_json(span.tool_result) for span in tool_spans)
        != expected_results
        or any(not span.name for span in tool_spans)
    ):
        raise RuntimeContractError(
            f"Live telemetry tool inputs or outputs differ for {fixture.id}."
        )


def _is_descendant(
    span: LiveSpanEvidence,
    root_id: str,
    span_by_id: dict[str, LiveSpanEvidence],
) -> bool:
    visited = {span.span_id}
    parent_id = span.parent_span_id
    while parent_id is not None:
        if parent_id == root_id:
            return True
        if parent_id in visited:
            return False
        visited.add(parent_id)
        parent = span_by_id.get(parent_id)
        if parent is None:
            return False
        parent_id = parent.parent_span_id
    return False


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise RuntimeContractError(
            "Live telemetry tool evidence must contain canonical JSON values."
        ) from error
