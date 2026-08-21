from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import agent_insights_quality.live_adapter as live
from agent_insights_quality.agent_runtime import DeploymentReceipt, InvocationReceipt
from agent_insights_quality.cli import main
from agent_insights_quality.insights.client import InsightCheckpoint
from agent_insights_quality.insights.telemetry import TraceCorrelation
from agent_insights_quality.live_adapter import (
    LiveRuntimeHooks,
    RecipeRegistry,
    WindowBinding,
    materialize_execution_plan,
    materialize_version,
)
from agent_insights_quality.planning import generate_daily_plan, serialize_plan
from agent_insights_quality.runtime.artifacts import LocalArtifactStore
from agent_insights_quality.runtime.azure import ProjectResources
from agent_insights_quality.runtime.config import RuntimeConfig
from agent_insights_quality.runtime.errors import RuntimeFailure
from agent_insights_quality.runtime.orchestrator import PlanInput, PlannedWindow
from agent_insights_quality.runtime.receipts import ensure_public_safe


SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
IMAGE = "ghcr.io/ninghu/agent-insights-quality-ticket@sha256:" + ("a" * 64)


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig.from_env(
        {
            "AIQ_AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
            "AIQ_AZURE_RESOURCE_GROUP": "quality-rg",
            "AIQ_FOUNDRY_ACCOUNT": "quality-account",
            "AIQ_FOUNDRY_PROJECT": "aiq-20260821",
            "AIQ_FOUNDRY_PROJECT_ENDPOINT": (
                "https://sample.services.ai.azure.com/api/projects/quality"
            ),
            "AIQ_APPLICATION_INSIGHTS_RESOURCE_ID": (
                f"/subscriptions/{SUBSCRIPTION}/resourceGroups/quality-rg/"
                "providers/Microsoft.Insights/components/quality"
            ),
            "AIQ_TERRA_AGENT_DEPLOYMENT": "terra-agents",
            "AIQ_TERRA_INSIGHTS_DEPLOYMENT": "terra-insights",
            "AIQ_TERRA_MODEL_VERSION": "2026-08-01",
            "AIQ_TICKET_IMAGE_URI": IMAGE,
            "AIQ_ADO_ORGANIZATION_URL": "https://ado.example.invalid",
            "AIQ_ADO_PROJECT": "Quality",
            "AIQ_ADO_TEMPLATE_ID": "template",
            "AIQ_ADO_OWNER_ID": "owner",
            "AIQ_ARTIFACT_BACKEND": "local",
            "AIQ_ARTIFACT_LOCATION": str(tmp_path / "artifacts"),
            "AIQ_AUTOMATION_OWNER": "ninghu",
            "AIQ_MONITOR_OWNERSHIP_RECEIPT": str(tmp_path / "monitors.json"),
            "AIQ_RUNTIME_ADAPTER": "agent_insights_quality.live_adapter",
        }
    )


def _plan() -> tuple[dict, PlanInput]:
    payload = generate_daily_plan(datetime(2026, 8, 21, tzinfo=UTC).date())
    return payload, PlanInput.from_daily_plan(payload)


def test_symbolic_daily_plan_materializes_without_changing_plan_digest(tmp_path: Path) -> None:
    payload, plan = _plan()
    assert plan.reference == payload["plan_digest"]
    assert all(
        isinstance(work.window, PlannedWindow)
        for versions in plan.agents.values()
        for work in versions
    )
    execution = materialize_execution_plan(plan)
    assert execution["plan_reference"] == payload["plan_digest"]
    assert len(execution["work_items"]) == sum(len(items) for items in plan.agents.values())

    plan_path = tmp_path / "plan.json"
    output = tmp_path / "execution.json"
    plan_path.write_bytes(serialize_plan(payload))
    assert main(
        ["materialize-execution-plan", "--plan", str(plan_path), "--output", str(output)]
    ) == 0
    assert json.loads(output.read_text(encoding="ascii"))["plan_reference"] == payload["plan_digest"]


def test_every_reviewed_recipe_shape_is_supported_and_unknown_shape_is_rejected() -> None:
    registry = RecipeRegistry.load()
    assert len(registry.mutations) == 59
    assert len(registry.traffic) == 63
    assert sum(len(item["operations"]) for item in registry.mutations.values()) == 59
    with pytest.raises(RuntimeFailure, match="not supported"):
        live._validate_operation(
            "source_patch",
            {"target": "python_source", "action": "execute", "value": "catalog string"},
        )
    with pytest.raises(RuntimeFailure, match="not supported"):
        live._validate_operation(
            "source_patch",
            {"target": "policy", "action": "configure_post", "value": True},
        )


def test_mutation_materialization_is_deterministic_for_all_five_agents() -> None:
    _, plan = _plan()
    registry = RecipeRegistry.load()
    for versions in plan.agents.values():
        work = next((item for item in versions if item.phase not in {"healthy", "corrected"}), versions[0])
        first = materialize_version(
            work,
            project_endpoint="https://sample.services.ai.azure.com/api/projects/quality",
            model_deployment="terra-agents",
            ticket_image=IMAGE,
            registry=registry,
        )
        second = materialize_version(
            work,
            project_endpoint="https://sample.services.ai.azure.com/api/projects/quality",
            model_deployment="terra-agents",
            ticket_image=IMAGE,
            registry=registry,
        )
        assert first.definition == second.definition
        assert first.mutation_reference == second.mutation_reference
        if work.agent_type == "prompt":
            assert "AIQ_SCENARIO_CONFIGURATION" not in first.definition
        else:
            encoded = first.definition["environment_variables"]["AIQ_SCENARIO_CONFIGURATION"]
            assert json.loads(encoded)["operations"] == list(first.operations)


class _Deployments:
    def __init__(self) -> None:
        self.routes: list[str] = []
        self.cleaned: list[str] = []
        self.version = 0

    def _receipt(self, route, agent_name, definition, run_id):
        self.routes.append(route)
        self.version += 1
        return DeploymentReceipt(
            agent_name,
            str(self.version),
            route,
            live.canonical_json_digest(definition),
            run_id,
            "active",
            source_digest=("sha256:" + ("b" * 64)) if route == "hosted_code" else None,
            image_digest=("sha256:" + ("a" * 64))
            if route == "hosted_custom_container"
            else None,
        )

    def deploy_prompt(self, *, agent_name, definition, run_id, create_agent):
        return self._receipt("prompt", agent_name, definition, run_id)

    def deploy_hosted_source(self, *, agent_name, definition, source, run_id, create_agent):
        assert source.is_dir()
        return self._receipt("hosted_code", agent_name, definition, run_id)

    def deploy_hosted_container(self, *, agent_name, definition, image, run_id, create_agent):
        assert image == IMAGE
        return self._receipt("hosted_custom_container", agent_name, definition, run_id)

    def cleanup_version(self, receipt):
        self.cleaned.append(receipt.agent_name + ":" + receipt.agent_version)


class _Invocations:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def _invoke(self, receipt, fixture):
        self.inputs.append(fixture.input)
        index = len(self.inputs)
        return InvocationReceipt(
            fixture.id,
            receipt.agent_name,
            receipt.agent_version,
            f"response-{index}",
            f"invocation-{index}",
            f"request-{index}",
            None if receipt.agent_type == "prompt" else f"session-{index}",
            "synthetic output",
            (),
        )

    invoke_prompt = _invoke
    invoke_hosted = _invoke


class _Insights:
    def __init__(self) -> None:
        self.cancelled: list[tuple[str, str]] = []

    def probe(self):
        return None

    def get_or_create_monitor(self, **_kwargs):
        return {"id": "monitor"}, False

    def capture_insight_checkpoint(self, _monitor):
        return InsightCheckpoint(datetime(2026, 8, 21, 12, tzinfo=UTC), {}, {})

    def find_monitor(self, _agent):
        return {"id": "monitor"}

    def create_run(self, _monitor, *, lookback_hours):
        assert lookback_hours == 1
        return {"id": "insights-run"}

    def collect_run(
        self,
        monitor,
        run_id,
        *,
        checkpoint,
        expected_start,
        expected_end,
    ):
        return (
            {
                "id": run_id,
                "monitor_id": monitor,
                "status": "succeeded",
                "start_time": expected_start.isoformat(),
                "end_time": expected_end.isoformat(),
            },
            [],
        )

    def cancel_run(self, monitor, run_id):
        self.cancelled.append((monitor, run_id))


def _prepared_hooks(tmp_path: Path, moments: list[datetime]):
    _, plan = _plan()
    deployments = _Deployments()
    invocations = _Invocations()
    insights = _Insights()
    hooks = LiveRuntimeHooks(
        _config(tmp_path),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        deployment_factory=lambda _endpoint, _token: deployments,
        invocation_factory=lambda _endpoint, _token: invocations,
        insights_factory=lambda _endpoint, _credential, _ownership: insights,
        telemetry_query=object(),
        now=lambda: moments.pop(0),
    )
    hooks._plan = plan
    hooks._project = ProjectResources(
        "private-project",
        plan.project_name,
        "account",
        "group",
        "https://sample.services.ai.azure.com/api/projects/quality",
        "private-app-insights",
        "principal",
        True,
        {},
    )
    hooks._deployment_client = deployments
    hooks._invocation_client = invocations
    hooks._insights = insights
    return hooks, plan, deployments, invocations, insights


def test_all_five_agents_route_endpoint_only_and_hooks_are_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 8, 21, 12, tzinfo=UTC)
    moments = [start + timedelta(seconds=index) for index in range(20)]
    hooks, plan, deployments, invocations, insights = _prepared_hooks(tmp_path, moments)
    selected = [versions[0] for versions in plan.agents.values()]
    for work in selected:
        public_deployment = hooks.deploy(work, idempotency_key=work.key + ":deploy")
        first = hooks.invoke(
            work,
            public_deployment,
            idempotency_key=work.key + ":invoke",
        )
        second = hooks.invoke(
            work,
            public_deployment,
            idempotency_key=work.key + ":invoke",
        )
        assert first == second
        ensure_public_safe(first)
    assert deployments.routes.count("prompt") == 2
    assert deployments.routes.count("hosted_code") == 2
    assert deployments.routes.count("hosted_custom_container") == 1
    assert invocations.inputs
    assert all('"correlation":' in value and '"input":' in value for value in invocations.inputs)


def test_realized_windows_are_exact_non_overlapping_and_feed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, plan = _plan()
    pair = next(
        (versions[index], versions[index + 1])
        for versions in plan.agents.values()
        for index in range(len(versions) - 1)
        if versions[index].run_id == versions[index + 1].run_id
    )
    start = datetime(2026, 8, 21, 12, tzinfo=UTC)
    hooks, _, deployments, _, insights = _prepared_hooks(
        tmp_path,
        [
            start,
            start + timedelta(seconds=10),
            start + timedelta(seconds=10),
            start + timedelta(seconds=20),
        ],
    )
    versions = hooks._plan.agents[pair[0].agent_id]
    pair_position = versions.index(pair[0])
    if pair_position:
        prior = versions[pair_position - 1]
        hooks._windows[prior.key] = WindowBinding(
            prior.window.start_identity,
            prior.window.end_identity,
            start - timedelta(seconds=10),
            start,
        )
    for work in pair:
        deployed = hooks.deploy(work, idempotency_key=work.key + ":deploy")
        invoked = hooks.invoke(work, deployed, idempotency_key=work.key + ":invoke")
        binding = hooks._windows[work.key]
        assert invoked["window_binding"] == binding.public_dict()
    assert hooks._windows[pair[0].key].realized_end <= hooks._windows[pair[1].key].realized_start

    overlapping = WindowBinding(
        pair[1].window.start_identity,
        pair[1].window.end_identity,
        start + timedelta(seconds=9),
        start + timedelta(seconds=11),
    )
    with pytest.raises(RuntimeFailure, match="overlapping"):
        hooks._assert_window_order(pair[1], overlapping)

    operation = "a" * 32
    spans = ("b" * 16, "c" * 16)
    monkeypatch.setattr(
        live,
        "wait_for_correlated_traces",
        lambda *_args, **_kwargs: [
            TraceCorrelation(operation, 2, 1, spans, start + timedelta(seconds=1))
            for _ in hooks._invocations[pair[1].key]
        ],
    )
    telemetry = hooks.wait_ingestion(
        pair[1],
        {"ignored": True},
        idempotency_key=pair[1].key + ":ingestion",
    )
    insight_run = hooks.run_insights(
        pair[1],
        telemetry,
        idempotency_key=pair[1].key + ":insights",
    )
    hooks._checkpoints[pair[1].key] = InsightCheckpoint(
        start,
        {},
        {"previous": {"revision": "prior-planned-version"}},
    )
    evidence = hooks.assemble_evidence(
        pair[1],
        insight_run,
        idempotency_key=pair[1].key + ":evidence",
    )
    ensure_public_safe(telemetry)
    ensure_public_safe(insight_run)
    ensure_public_safe(evidence)
    assert evidence["evidence_count"] == len(pair[1].assignments)
    assert hooks._prior_insight(pair[1])["version_digest"] == pair[0].version_reference
    first = hooks._plan.agents[pair[1].agent_id][0]
    hooks._checkpoints[first.key] = InsightCheckpoint(
        start,
        {},
        {"pre-existing": {"revision": "prior-service-revision"}},
    )
    assert hooks._prior_insight(first) is None
    assert json.loads(
        (tmp_path / "artifacts" / f"{plan.plan_id}/evidence/"
         f"{pair[1].assignments[0]['scenario_id']}-{pair[1].phase}.json").read_text(
            encoding="ascii"
        )
    )["run"]["window_start"] == hooks._windows[pair[1].key].realized_start.isoformat()

    hooks.cancel(pair[1])
    assert deployments.cleaned
    assert insights.cancelled == [("monitor", "insights-run")]
