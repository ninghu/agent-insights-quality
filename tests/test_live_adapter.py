from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Event, Thread
from zipfile import ZipFile

import pytest

import agent_insights_quality.live_adapter as live
from agent_insights_quality.agent_runtime import (
    DeploymentReceipt,
    InvocationEndpointError,
    InvocationFailureReceipt,
    InvocationReceipt,
)
from agent_insights_quality.cli import main
from agent_insights_quality.insights.client import AgentInsightsClient, InsightCheckpoint
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
_REGISTRY = RecipeRegistry.load()
EXPECTED_FAILURE_SCENARIOS = frozenset(
    scenario_id
    for scenario_id, scenario in _REGISTRY.scenarios.items()
    if scenario["mutation"]["recipe_id"] is not None
    and any(
        (operation["target"], operation["action"])
        in live._EXPECTED_ENDPOINT_FAILURES
        for operation in _REGISTRY.mutations[
            str(scenario["mutation"]["recipe_id"])
        ]["operations"]
    )
)


def _work_for_recipe(recipe_id: str) -> tuple[object, object]:
    registry = RecipeRegistry.load()
    recipe = registry.mutations[recipe_id]
    scenario = next(
        value
        for value in registry.scenarios.values()
        if value["mutation"]["recipe_id"] == recipe_id
    )
    kind = str(recipe["kind"])
    agent_id = "aiq-001-weather" if kind != "source_patch" else "aiq-003-finance"
    agent_type = "prompt" if kind != "source_patch" else "hosted_code"
    phase = str(scenario["version_semantics"]["phases"][0])
    version_key = str(scenario["version_semantics"]["version_keys"][0])
    traffic = registry.traffic[str(scenario["traffic"]["recipe_id"])]
    work = live.VersionWork(
        agent_id=agent_id,
        agent_name=f"{agent_id}-recipe-coverage",
        version_reference="sha256:" + ("f" * 64),
        window=PlannedWindow(
            f"window://run-01-{agent_id}/{phase}/start-inclusive",
            f"window://run-01-{agent_id}/{phase}/end-exclusive",
        ),
        assignments=(
            {
                "scenario_id": scenario["id"],
                "scenario_version": scenario["version"],
                "agent_type": agent_type,
                "traffic_recipe_id": traffic["id"],
                "traffic_requests": traffic["request_count"],
                "traffic_seed": 7,
            },
        ),
        agent_type=agent_type,
        wave=1,
        phase=phase,
        version_key=version_key,
    )
    return work, recipe


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


@pytest.mark.parametrize(
    ("expected", "actual", "verdict", "reason"),
    [
        (1, 1, "AT_BAR", "exact"),
        (1, 2, "NOT_AT_BAR", "extra_noise"),
        (2, 1, "NOT_AT_BAR", "missing_findings"),
    ],
)
def test_finding_count_assessment_requires_exact_count(
    expected: int,
    actual: int,
    verdict: str,
    reason: str,
) -> None:
    assert live._finding_count_assessment(expected, actual) == {
        "expected": expected,
        "actual": actual,
        "verdict": verdict,
        "reason": reason,
    }


@pytest.mark.parametrize(
    "recipe_id",
    sorted(RecipeRegistry.load().mutations),
)
def test_every_reviewed_recipe_materializes_its_exact_operation(recipe_id: str) -> None:
    work, recipe = _work_for_recipe(recipe_id)
    materialized = materialize_version(
        work,
        project_endpoint="https://sample.services.ai.azure.com/api/projects/quality",
        model_deployment="terra-agents",
        ticket_image=IMAGE,
    )
    assert list(materialized.operations) == recipe["operations"]
    if recipe["kind"] == "source_patch":
        encoded = materialized.definition["environment_variables"][
            "AIQ_SCENARIO_CONFIGURATION"
        ]
        assert json.loads(encoded)["scenarios"] == [
            {
                "scenario_id": work.assignments[0]["scenario_id"],
                "operations": recipe["operations"],
            }
        ]
    else:
        assert materialized.instruction_delta
        assert all(
            json.dumps(operation["value"], sort_keys=True)
            in materialized.instruction_delta
            for operation in recipe["operations"]
        )
    with pytest.raises(RuntimeFailure, match="not supported"):
        live._validate_operation(
            "source_patch",
            {"target": "policy", "action": "configure_post", "value": True},
        )
    with pytest.raises(RuntimeFailure, match="exact reviewed value"):
        live._validate_operation(
            "traffic_only",
            {
                "target": "synthetic_tool_fixture",
                "action": "configure_post_completion_delay",
                "value": 251,
            },
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
            assert [
                operation
                for item in json.loads(encoded)["scenarios"]
                for operation in item["operations"]
            ] == list(first.operations)


def test_reused_scenario_059_version_has_identical_artifact_configuration() -> None:
    _, plan = _plan()
    works = [
        work
        for versions in plan.agents.values()
        for work in versions
        if any(
            assignment["scenario_id"] == "aiq-scn-059-cross-window-dedup"
            for assignment in work.assignments
        )
    ]
    first, second = works
    assert first.version_reference == second.version_reference
    assert first.version_key == second.version_key
    materialized = [
        materialize_version(
            work,
            project_endpoint="https://sample.services.ai.azure.com/api/projects/quality",
            model_deployment="terra-agents",
            ticket_image=IMAGE,
        )
        for work in works
    ]
    assert materialized[0].definition == materialized[1].definition
    assert materialized[0].instruction_delta == materialized[1].instruction_delta


def test_healthy_artifact_digests_cover_resolved_runtime_assets() -> None:
    for agent in live.load_healthy_agents():
        definition = agent.definition_for_deployment(
            model_deployment_name="terra-agents",
            project_endpoint=(
                "https://sample.services.ai.azure.com/api/projects/quality"
                if agent.kind != "prompt"
                else None
            ),
        )
        digest = live._healthy_artifact_digest(
            agent,
            definition,
            ticket_image=IMAGE,
        )
        assert digest.startswith("sha256:")
        assert agent.representative_tools
        if agent.kind == "hosted_code":
            archive, _ = live.deterministic_zip(agent.source)
            with ZipFile(BytesIO(archive)) as package:
                assert "scenario_runtime.py" in package.namelist()


@pytest.mark.parametrize(
    ("recipe_id", "expected_case"),
    [
        ("mut-guardrail-bypass-v1", "guardrail-bypass-probe"),
        ("mut-action-without-confirmation-v1", "no-confirmation"),
        ("mut-malformed-approval-v1", "malformed-approval"),
        ("mut-cross-account-pii-v1", "cross-account-synthetic-record"),
        ("mut-parent-child-correlation-v1", "correlated-child-failure"),
        ("mut-outer-zero-token-control-v1", "zero-token-outer-successful-child"),
        ("mut-handled-child-failure-control-v1", "handled-child-failure"),
    ],
)
def test_endpoint_case_operations_are_present_in_every_request(
    tmp_path: Path,
    recipe_id: str,
    expected_case: str,
) -> None:
    work, _ = _work_for_recipe(recipe_id)
    hooks, _, _, _, _ = _prepared_hooks(tmp_path, [])
    agent = next(item for item in live.load_healthy_agents() if item.id == work.agent_id)
    fixtures = hooks._fixtures(work, agent)
    assert fixtures
    assert {
        json.loads(fixture.input)["case"] for fixture in fixtures
    } == {expected_case}


@pytest.mark.parametrize(
    "recipe_id",
    [
        recipe_id
        for recipe_id, recipe in sorted(RecipeRegistry.load().mutations.items())
        if recipe["kind"] == "traffic_only"
    ],
)
def test_every_traffic_recipe_materializes_observable_prompt_operations(
    tmp_path: Path,
    recipe_id: str,
) -> None:
    work, recipe = _work_for_recipe(recipe_id)
    hooks, _, _, _, _ = _prepared_hooks(tmp_path, [])
    agent = next(item for item in live.load_healthy_agents() if item.id == work.agent_id)
    fixtures = hooks._fixtures(work, agent)
    assert fixtures
    assert all(fixture.scenario_operations for fixture in fixtures)
    operation = recipe["operations"][0]
    action = operation["action"]
    value = operation["value"]
    first = fixtures[0].scenario_operations[0]
    if operation["target"] == "endpoint_request":
        assert first.endpoint_case == value
    elif action == "configure_response":
        assert first.result["fixture"] == "configure_response"
        if value == "permanent_failure":
            assert first.result["permanent"] is True
            assert first.result["status"] == "error"
        else:
            assert all(first.result[key] == item for key, item in value.items())
    elif action == "remove_field":
        assert value not in first.result
    elif action == "configure_sequence":
        assert first.result["status"] == "error"
        assert fixtures[1].scenario_operations[0].result != first.result
    elif action == "configure_parallelizable_delays":
        assert first.delay_seconds == value[0] / 1000
    elif action == "configure_post_completion_delay":
        assert first.delay_seconds == value / 1000
    else:
        raise AssertionError(f"unasserted traffic action: {action}")


class _Deployments:
    def __init__(self) -> None:
        self.routes: list[str] = []
        self.cleaned: list[str] = []
        self.version = 0
        self.recovered: dict[
            tuple[str, str, str], DeploymentReceipt
        ] = {}

    def recover_version(
        self,
        *,
        agent_name,
        agent_type,
        run_id,
        artifact_digest,
        source_digest=None,
        image_digest=None,
    ):
        return (
            self.recovered.get((agent_name, run_id, artifact_digest)),
            False,
        )

    def _receipt(self, route, agent_name, definition, run_id):
        self.routes.append(route)
        self.version += 1
        receipt = DeploymentReceipt(
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
        self.recovered[(agent_name, run_id, receipt.artifact_digest)] = receipt
        return receipt

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
        self.abort_on_call: int | None = None

    def _invoke(self, receipt, fixture):
        self.inputs.append(fixture.input)
        index = len(self.inputs)
        if self.abort_on_call == index:
            raise live.RuntimeContractError("unexpected transport failure")
        if fixture.id.split(":", 1)[0] in EXPECTED_FAILURE_SCENARIOS:
            raise InvocationEndpointError(
                "expected synthetic failure",
                InvocationFailureReceipt(
                    receipt.agent_name,
                    receipt.agent_version,
                    500,
                    invocation_id=f"invocation-{index}",
                    request_id=f"request-{index}",
                    session_id=f"session-{index}",
                ),
            )
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
        self.checkpoint_count = 0

    def probe(self):
        return None

    def get_or_create_monitor(self, **_kwargs):
        return {"id": "monitor"}, False

    def capture_insight_checkpoint(self, _monitor, **_kwargs):
        self.checkpoint_count += 1
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
        lookback_hours,
        agent_name,
        agent_version,
        operation_ids,
        cancelled,
    ):
        assert checkpoint.revisions == {}
        assert lookback_hours == 1
        assert agent_name
        assert agent_version
        assert operation_ids
        assert not cancelled()
        return (
            {
                "id": run_id,
                "monitor_id": monitor,
                "status": "succeeded",
                "start_time": (expected_end - timedelta(hours=1)).isoformat(),
                "end_time": expected_end.isoformat(),
            },
            [],
        )

    validate_run_window = staticmethod(AgentInsightsClient.validate_run_window)

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


def test_private_receipts_recover_deploy_and_invoke_across_hook_instances(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 8, 21, 12, tzinfo=UTC)
    first, plan, first_deployments, first_invocations, _ = _prepared_hooks(
        tmp_path,
        [start, start + timedelta(seconds=5)],
    )
    work = next(iter(plan.agents.values()))[0]
    deployed = first.deploy(work, idempotency_key=work.key + ":deploy")
    invoked = first.invoke(work, deployed, idempotency_key=work.key + ":invoke")
    assert first_deployments.routes
    assert first_invocations.inputs

    resumed, _, resumed_deployments, resumed_invocations, _ = _prepared_hooks(
        tmp_path,
        [],
    )
    assert resumed.deploy(work, idempotency_key=work.key + ":deploy") == deployed
    assert (
        resumed.invoke(work, deployed, idempotency_key=work.key + ":invoke")
        == invoked
    )
    assert resumed_deployments.routes == []
    assert resumed_invocations.inputs == []
    assert resumed._windows[work.key].public_dict() == invoked["window_binding"]


def test_expected_endpoint_failures_persist_each_fixture_and_resume_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 8, 21, 12, tzinfo=UTC)
    first, plan, _, first_invocations, _ = _prepared_hooks(
        tmp_path,
        [start],
    )
    work = next(
        item
        for versions in plan.agents.values()
        for item in versions
        if any(
            first._expects_endpoint_failure(
                item,
                str(assignment["scenario_id"]),
            )
            for assignment in item.assignments
        )
    )
    deployed = first.deploy(work, idempotency_key=work.key + ":deploy")
    first_invocations.abort_on_call = 2
    with pytest.raises(RuntimeFailure, match="unexpected transport failure"):
        first.invoke(work, deployed, idempotency_key=work.key + ":invoke")
    assert len(first_invocations.inputs) == 2

    resumed, _, _, resumed_invocations, resumed_insights = _prepared_hooks(
        tmp_path,
        [start + timedelta(seconds=1), start + timedelta(seconds=2)],
    )
    recovered_deployment = resumed.deploy(
        work,
        idempotency_key=work.key + ":deploy",
    )
    versions = resumed._plan.agents[work.agent_id]
    position = versions.index(work)
    if position:
        prior = versions[position - 1]
        resumed._windows[prior.key] = WindowBinding(
            prior.window.start_identity,
            prior.window.end_identity,
            start - timedelta(seconds=2),
            start,
        )
    result = resumed.invoke(
        work,
        recovered_deployment,
        idempotency_key=work.key + ":invoke",
    )
    fixture_count = sum(
        int(assignment["traffic_requests"]) for assignment in work.assignments
    )
    expected_failure_count = sum(
        int(assignment["traffic_requests"])
        for assignment in work.assignments
        if assignment["scenario_id"] in EXPECTED_FAILURE_SCENARIOS
    )
    assert result["expected_failure_count"] == expected_failure_count
    assert result["completed_count"] == fixture_count - expected_failure_count
    assert len(resumed_invocations.inputs) == fixture_count - 1
    assert resumed_insights.checkpoint_count == 0

    captured: dict[str, object] = {}

    def correlate(*_args, **kwargs):
        expectations = kwargs["expectations"]
        captured["expectations"] = expectations
        return [
            TraceCorrelation(
                f"{index + 1:032x}",
                1,
                1,
                (f"{index + 1:016x}",),
                start + timedelta(seconds=1),
            )
            for index, _ in enumerate(expectations)
        ]

    monkeypatch.setattr(live, "wait_for_correlated_traces", correlate)
    telemetry = resumed.wait_ingestion(
        work,
        result,
        idempotency_key=work.key + ":ingestion",
    )
    expectations = captured["expectations"]
    assert telemetry["operation_count"] == fixture_count
    assert len(expectations) == fixture_count
    assert all(item.identifiers() for item in expectations)
    assert (
        sum(
            item.required_operations == frozenset({"invoke_agent"})
            for item in expectations
        )
        == expected_failure_count
    )


def test_insight_run_is_persisted_before_polling_and_cancelled_after_restart(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 8, 21, 12, tzinfo=UTC)
    hooks, plan, _, _, _ = _prepared_hooks(tmp_path, [])
    work = next(iter(plan.agents.values()))[0]
    hooks.deploy(work, idempotency_key=work.key + ":deploy")
    hooks._windows[work.key] = WindowBinding(
        work.window.start_identity,
        work.window.end_identity,
        start,
        start + timedelta(seconds=10),
    )
    hooks._checkpoints[work.key] = InsightCheckpoint(start, {}, {})
    hooks._telemetry[work.key] = (
        TraceCorrelation(
            "a" * 32,
            1,
            1,
            ("b" * 16,),
            start + timedelta(seconds=1),
        ),
    )
    polling_started = Event()
    allow_completion = Event()

    class BlockingInsights(_Insights):
        def collect_run(self, *args, **kwargs):
            polling_started.set()
            assert allow_completion.wait(5)
            return super().collect_run(*args, **kwargs)

    hooks._insights = BlockingInsights()
    failures: list[BaseException] = []

    def execute() -> None:
        try:
            hooks.run_insights(
                work,
                {"ignored": True},
                idempotency_key=work.key + ":insights",
            )
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=execute)
    thread.start()
    assert polling_started.wait(5)

    resumed, _, resumed_deployments, _, resumed_insights = _prepared_hooks(
        tmp_path,
        [],
    )
    resumed.cancel(work)
    assert resumed_insights.cancelled == [("monitor", "insights-run")]
    assert resumed_deployments.cleaned

    allow_completion.set()
    thread.join(5)
    assert not thread.is_alive()
    assert failures == []


def test_recurred_version_uses_latest_matching_faulted_insight_across_correction(
    tmp_path: Path,
) -> None:
    hooks, plan, _, _, _ = _prepared_hooks(tmp_path, [])
    scenario_id = "aiq-scn-060-fixed-issue-recurrence"
    works = sorted(
        [
            work
            for versions in plan.agents.values()
            for work in versions
            if any(
                assignment["scenario_id"] == scenario_id
                for assignment in work.assignments
            )
        ],
        key=lambda work: work.sequence_index,
    )
    assert [work.phase for work in works] == ["faulted", "corrected", "recurred"]
    root_cause = hooks._registry.scenarios[scenario_id]["expected"]["root_cause"]
    hooks._provenance[(works[0].key, scenario_id)] = {
        "insight_id": "original-finding",
        "fingerprint": "sha256:" + ("c" * 64),
        "artifact_digest": "sha256:" + ("d" * 64),
        "trace_ids": ["a" * 32],
        "root_cause": root_cause,
    }
    previous = hooks._prior_insight(works[2], scenario_id)
    assert previous is not None
    assert previous["version_digest"] == "sha256:" + ("d" * 64)


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
    scenario_id = str(pair[1].assignments[0]["scenario_id"])
    previous_deployment = hooks._deployments[
        (pair[0].agent_name, pair[0].version_reference)
    ]
    hooks._provenance[(pair[0].key, scenario_id)] = {
        "insight_id": "prior-insight",
        "fingerprint": "sha256:" + ("d" * 64),
        "artifact_digest": previous_deployment.artifact_digest,
        "trace_ids": [operation],
        "root_cause": hooks._registry.scenarios[scenario_id]["expected"][
            "root_cause"
        ],
    }
    evidence = hooks.assemble_evidence(
        pair[1],
        insight_run,
        idempotency_key=pair[1].key + ":evidence",
    )
    ensure_public_safe(telemetry)
    ensure_public_safe(insight_run)
    ensure_public_safe(evidence)
    assert evidence["evidence_count"] == len(pair[1].assignments)
    assert (
        hooks._prior_insight(pair[1], scenario_id)["version_digest"]
        == previous_deployment.artifact_digest
    )
    first = hooks._plan.agents[pair[1].agent_id][0]
    assert hooks._prior_insight(first, scenario_id) is None
    bundle = json.loads(
        (tmp_path / "artifacts" / f"{plan.plan_id}/evidence/"
         f"{pair[1].assignments[0]['scenario_id']}-{pair[1].phase}.json").read_text(
            encoding="ascii"
        )
    )
    assert bundle["run"]["window_start"] == hooks._windows[
        pair[1].key
    ].realized_start.isoformat()
    assert bundle["run"]["analysis_window_start"] == hooks._insight_windows[
        pair[1].key
    ][0].isoformat()
    assert len(bundle["trace_evidence"]) == int(
        pair[1].assignments[0]["traffic_requests"]
    )
    assert bundle["finding_count"] == {
        "expected": pair[1].assignments[0]["expected"]["finding_count"],
        "actual": 0,
        "verdict": "NOT_AT_BAR",
        "reason": "missing_findings",
    }
    expected_tools = next(
        agent.representative_tools
        for agent in live.load_healthy_agents()
        if agent.id == pair[1].agent_id
    )
    assert bundle["agent"]["available_tools"] == sorted(expected_tools)
    assert bundle["trace_evidence"][0]["trace_id"].startswith("sha256:")
    assert all(
        span.startswith("sha256:") for span in bundle["trace_evidence"][0]["span_ids"]
    )

    hooks.cancel(pair[1])
    assert deployments.cleaned
    assert insights.cancelled == [("monitor", "insights-run")]
