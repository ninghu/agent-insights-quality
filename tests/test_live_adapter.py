from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
from typing import Any, Callable
from zipfile import ZipFile

import pytest

import agent_insights_quality.live_adapter as live
from agent_insights_quality.artifact_io import content_hash
from agent_insights_quality.agent_runtime import (
    DeploymentCleanupError,
    DeploymentHttpError,
    DeploymentReceipt,
    DeploymentPollError,
    InvocationEndpointError,
    InvocationFailureReceipt,
    InvocationReceipt,
)
from agent_insights_quality.cli import main
from agent_insights_quality.contracts import ContractError
from agent_insights_quality.daily import (
    build_daily_status,
    validate_daily_status_packages,
)
from agent_insights_quality.insights.client import AgentInsightsClient, InsightCheckpoint
from agent_insights_quality.insights.telemetry import TraceCorrelation
from agent_insights_quality.judging import project_evidence, validate_evidence_bundle
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
from agent_insights_quality.runtime.orchestrator import (
    PlanInput,
    PlannedWindow,
    ProductionOrchestrator,
    RunState,
    VersionWork,
)
from agent_insights_quality.runtime.receipts import ensure_public_safe, opaque_reference
from agent_insights_quality.scoring import score_run


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


def _capture_failure(
    failures: list[BaseException],
    operation: Callable[[], Any],
) -> None:
    try:
        operation()
    except BaseException as error:
        failures.append(error)


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
    payload = generate_daily_plan(
        datetime(2026, 8, 21, tzinfo=UTC).date(),
        full_catalog=True,
    )
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


def test_failure_artifacts_are_append_only_across_resume_attempts(
    tmp_path: Path,
) -> None:
    _, plan = _plan()
    hooks = LiveRuntimeHooks(
        _config(tmp_path),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )
    hooks._plan = plan

    first = hooks.finalize_failure(
        RuntimeFailure(
            "first_failure",
            "First synthetic failure.",
            {
                "cancellation_failures": ["deployment_cleanup_sessions_active"],
                "unexpected_exceptions": [
                    {
                        "exception_class": "ValueError",
                        "exception_reference": "sha256:" + ("b" * 64),
                        "work_reference": "sha256:" + ("c" * 64),
                    }
                ],
                "command": [
                    "az",
                    "resource",
                    "show",
                    "--ids",
                    "/subscript"
                    "ions/11111111-1111-1111-1111-111111111111/"
                    "resourceGroups/private-rg",
                ],
            },
        ),
        {"attempt": 1, "status": "inconclusive", "phase": "deploy"},
    )
    second = hooks.finalize_failure(
        RuntimeFailure("second_failure", "Second synthetic failure."),
        {"attempt": 2, "status": "inconclusive", "phase": "invoke"},
    )

    failures = [
        path
        for path in sorted(
            (tmp_path / "artifacts" / plan.plan_id / "runtime" / "failures").rglob(
                "failure-*.json"
            )
        )
        if not path.name.endswith(".metadata.json")
    ]
    assert len(failures) == 2
    assert first["artifact_reference"] != second["artifact_reference"]
    first_payload = json.loads(failures[0].read_text(encoding="ascii"))
    assert first_payload["attempt"] == 1
    assert first_payload["failure"]["details"]["cancellation_failures"] == [
        "deployment_cleanup_sessions_active"
    ]
    assert first_payload["private_diagnostics"]["unexpected_exceptions"] == [
        {
            "exception_class": "ValueError",
            "exception_reference": "sha256:" + ("b" * 64),
            "work_reference": "sha256:" + ("c" * 64),
        }
    ]
    assert "/subscript" + "ions/" not in json.dumps(first_payload)
    assert json.loads(failures[1].read_text(encoding="ascii"))["attempt"] == 2


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


def _contract_live_insight(
    insight_id: str,
    trace_ids: list[str],
    category: str,
) -> dict:
    return {
        "id": insight_id,
        "agent_name": "synthetic-agent",
        "agent_version": "1",
        "revision": "1",
        "title": "Synthetic finding",
        "description": "A bounded synthetic finding.",
        "category": category,
        "severity": "high",
        "created_at": "2026-08-21T12:05:00Z",
        "updated_at": "2026-08-21T12:06:00Z",
        "details": {
            "highlighted_traces": [
                {
                    "trace_id": trace_id,
                    "timestamp": "2026-08-21T12:01:00Z",
                }
                for trace_id in trace_ids
            ],
            "linked_traces": [],
            "recommended_actions": {
                "proposed_fix": {
                    "text": "Apply the bounded fix.",
                    "kind": "code_change",
                    "changes": [],
                }
            },
        },
    }


def test_run_insight_accounting_assigns_every_unique_card_once(
    tmp_path: Path,
) -> None:
    hooks, plan, _, _, _ = _prepared_hooks(tmp_path, [])
    work = next(
        work
        for versions in plan.agents.values()
        for work in versions
        if len(work.assignments) >= 2
    )
    first_id = str(work.assignments[0]["scenario_id"])
    second_id = str(work.assignments[1]["scenario_id"])
    first_trace = "a" * 32
    second_trace = "b" * 32
    category = str(hooks._registry.scenarios[first_id]["expected"]["category"])
    second_category = str(
        hooks._registry.scenarios[second_id]["expected"]["category"]
    )
    wrong_category = "latency" if second_category != "latency" else "output_quality"
    insights = [
        _contract_live_insight("assigned", [first_trace], category),
        _contract_live_insight(
            "umbrella",
            [first_trace, second_trace],
            category,
        ),
        _contract_live_insight("extra", [second_trace], wrong_category),
    ]
    correlations = {
        first_id: (TraceCorrelation(first_trace, 1, 1),),
        second_id: (TraceCorrelation(second_trace, 1, 1),),
    }
    allocation = hooks._allocate_run_insights(work, insights, correlations)
    assert allocation.total == len(insights)
    assert [item["id"] for item in allocation.by_scenario[first_id]] == ["assigned"]
    assert [item["id"] for item in allocation.umbrella_noise] == ["umbrella"]
    assert [item["id"] for item in allocation.extra_noise] == ["extra"]


def test_run_insight_accounting_accepts_only_proven_same_scenario_prior_traces(
    tmp_path: Path,
) -> None:
    hooks, plan, _, _, _ = _prepared_hooks(tmp_path, [])
    scenario_id = "aiq-scn-060-fixed-issue-recurrence"
    work = next(
        item
        for versions in plan.agents.values()
        for item in versions
        if item.phase == "recurred"
        and any(
            assignment["scenario_id"] == scenario_id
            for assignment in item.assignments
        )
    )
    current_trace = "a" * 32
    prior_trace = "b" * 32
    unknown_trace = "c" * 32
    correlations = {
        scenario_id: (TraceCorrelation(current_trace, 1, 1),),
    }
    prior = {scenario_id: {prior_trace}}
    category = str(hooks._registry.scenarios[scenario_id]["expected"]["category"])

    allocation = hooks._allocate_run_insights(
        work,
        [
            _contract_live_insight(
                "stale",
                [current_trace, prior_trace],
                category,
            )
        ],
        correlations,
        prior,
    )
    assert [item["id"] for item in allocation.by_scenario[scenario_id]] == [
        "stale"
    ]
    prior_only = hooks._allocate_run_insights(
        work,
        [
            _contract_live_insight(
                "prior-only",
                [prior_trace],
                category,
            )
        ],
        correlations,
        prior,
    )
    assert [item["id"] for item in prior_only.by_scenario[scenario_id]] == [
        "prior-only"
    ]

    with pytest.raises(RuntimeFailure) as caught:
        hooks._allocate_run_insights(
            work,
            [
                _contract_live_insight(
                    "unknown",
                    [current_trace, unknown_trace],
                    category,
                )
            ],
            correlations,
            prior,
        )
    assert caught.value.code == "insight_scope_unproven"


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
    assert live._canonical(materialized[0].definition) == live._canonical(
        materialized[1].definition
    )
    assert live._materialized_artifact_identity(
        materialized[0]
    ) == live._materialized_artifact_identity(materialized[1])
    if materialized[0].agent.source is not None:
        first_archive, first_digest = live.deterministic_zip(
            materialized[0].agent.source
        )
        second_archive, second_digest = live.deterministic_zip(
            materialized[1].agent.source
        )
        assert first_archive == second_archive
        assert first_digest == second_digest


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
        assert fixtures[0].scenario_operations[1].result != first.result
    elif action == "configure_parallelizable_delays":
        assert first.delay_seconds == value[0] / 1000
    elif action == "configure_post_completion_delay":
        assert first.delay_seconds == value / 1000
    else:
        raise AssertionError(f"unasserted traffic action: {action}")


@pytest.mark.parametrize(
    "traffic_recipe_id",
    sorted(RecipeRegistry.load().traffic),
)
def test_all_63_traffic_recipes_produce_executable_endpoint_requests(
    tmp_path: Path,
    traffic_recipe_id: str,
) -> None:
    hooks, plan, _, _, _ = _prepared_hooks(tmp_path, [])
    work = next(
        work
        for versions in plan.agents.values()
        for work in versions
        if any(
            assignment["traffic_recipe_id"] == traffic_recipe_id
            for assignment in work.assignments
        )
        and work.phase != "corrected"
    )
    agent = next(item for item in live.load_healthy_agents() if item.id == work.agent_id)
    scenario_ids = {
        str(assignment["scenario_id"])
        for assignment in work.assignments
        if assignment["traffic_recipe_id"] == traffic_recipe_id
    }
    fixtures = [
        fixture
        for fixture in hooks._fixtures(work, agent)
        if fixture.id.split(":", 1)[0] in scenario_ids
    ]
    recipe = hooks._registry.traffic[traffic_recipe_id]
    assert len(fixtures) == int(recipe["request_count"])
    correlations = set()
    for fixture in fixtures:
        request = json.loads(fixture.input)
        assert request["scenario_id"] in scenario_ids
        assert request["input"] in {item.input for item in agent.fixtures}
        assert traffic_recipe_id in request["synthetic_recipe"]
        assert request["correlation"] not in correlations
        correlations.add(request["correlation"])


def test_generated_traffic_preserves_each_agents_reviewed_domain_inputs_and_tools(
    tmp_path: Path,
) -> None:
    hooks, plan, _, _, _ = _prepared_hooks(tmp_path, [])
    agents = {agent.id: agent for agent in live.load_healthy_agents()}
    for agent_id, versions in plan.agents.items():
        work = versions[0]
        agent = agents[agent_id]
        fixtures = hooks._fixtures(work, agent)
        assert fixtures
        for fixture in fixtures:
            request_index = int(fixture.id.rsplit(":", 1)[1])
            base = agent.fixtures[request_index % len(agent.fixtures)]
            request = json.loads(fixture.input)
            assert request["input"] == base.input
            assert fixture.expected_tool_calls == base.expected_tool_calls
            assert fixture.tool_outputs == base.tool_outputs


def test_generated_healthy_prompt_traffic_enforces_output_and_tool_contracts(
    tmp_path: Path,
) -> None:
    hooks, plan, _, _, _ = _prepared_hooks(tmp_path, [])
    agents = {agent.id: agent for agent in live.load_healthy_agents()}
    saw_fault = False
    for agent_id, versions in plan.agents.items():
        agent = agents[agent_id]
        if agent.kind != "prompt":
            continue
        for work in versions:
            fixtures = hooks._fixtures(work, agent)
            expected_by_scenario = {
                assignment["scenario_id"]: assignment["expected"]["finding_count"]
                for assignment in work.assignments
            }
            for fixture in fixtures:
                scenario_id = fixture.id.split(":", 1)[0]
                if expected_by_scenario[scenario_id] == 0:
                    assert fixture.validate_output is True
                    assert fixture.validate_tools is True
                else:
                    saw_fault = True
                    assert fixture.validate_output is False
                    assert fixture.validate_tools is False
    assert saw_fault


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
        cancelled=None,
    ):
        assert cancelled is None or not cancelled()
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

    def deploy_prompt(
        self,
        *,
        agent_name,
        definition,
        run_id,
        create_agent,
        cancelled=None,
    ):
        assert cancelled is None or not cancelled()
        return self._receipt("prompt", agent_name, definition, run_id)

    def deploy_hosted_source(
        self,
        *,
        agent_name,
        definition,
        source,
        run_id,
        create_agent,
        cancelled=None,
    ):
        assert cancelled is None or not cancelled()
        assert source.is_dir()
        return self._receipt("hosted_code", agent_name, definition, run_id)

    def deploy_hosted_container(
        self,
        *,
        agent_name,
        definition,
        image,
        run_id,
        create_agent,
        cancelled=None,
    ):
        assert cancelled is None or not cancelled()
        assert image == IMAGE
        return self._receipt("hosted_custom_container", agent_name, definition, run_id)

    def cleanup_version(self, receipt):
        self.cleaned.append(receipt.agent_name + ":" + receipt.agent_version)


class _Invocations:
    def __init__(self) -> None:
        self.inputs: list[str] = []
        self.abort_on_call: int | None = None

    def _invoke(self, receipt, fixture, *, cancelled=None):
        assert cancelled is None or not cancelled()
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
        assert lookback_hours == 3
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
        assert lookback_hours == 3
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


def _single_version_assignment(
    payload: dict[str, Any],
    plan: PlanInput,
) -> tuple[dict[str, Any], VersionWork]:
    assignment = next(
        item for item in payload["assignments"] if len(item["version_sequence"]) == 1
    )
    final = assignment["version_sequence"][-1]
    work = next(
        work
        for versions in plan.agents.values()
        for work in versions
        if work.phase == final["phase"]
        and work.version_reference == final["digest"]
        and any(
            item["scenario_id"] == assignment["scenario_id"]
            for item in work.assignments
        )
    )
    return assignment, work


def _synthetic_evidence_bundle(
    hooks: LiveRuntimeHooks,
    plan: PlanInput,
    work: VersionWork,
    assignment: dict[str, Any],
    project_reference: str,
    *,
    second_project_reference: str | None = None,
) -> dict[str, Any]:
    scenario_id = str(assignment["scenario_id"])
    expected = hooks._registry.scenarios[scenario_id]["expected"]
    trace_references = [project_reference]
    if second_project_reference is not None:
        trace_references.append(second_project_reference)
    traces = [
        {
            "trace_id": opaque_reference(f"trace:{scenario_id}:{index}"),
            "span_ids": [opaque_reference(f"span:{scenario_id}:{index}")],
            "summary": "Synthetic correlated trace.",
            "artifact_reference": opaque_reference(
                f"artifact:{scenario_id}:{index}"
            ),
            "project_reference": reference,
            "agent_id": assignment["agent_id"],
            "version_digest": work.version_reference,
            "observed_at": "2026-08-21T12:00:30Z",
        }
        for index, reference in enumerate(trace_references)
    ]
    return project_evidence(
        {
            "schema_version": "1.0.0",
            "bundle_id": str(
                live.uuid.uuid5(
                    live.uuid.NAMESPACE_URL,
                    f"test:{scenario_id}:{work.phase}",
                )
            ),
            "plan_id": plan.plan_id,
            "scenario": {
                "id": scenario_id,
                "version": assignment["scenario_version"],
            },
            "agent": {
                "id": assignment["agent_id"],
                "name": assignment["agent_name"],
                "type": assignment["agent_type"],
                "version_digest": work.version_reference,
                "available_tools": [],
            },
            "run": {
                "run_id": assignment["run_id"],
                "window_start": "2026-08-21T12:00:00Z",
                "window_end": "2026-08-21T12:01:00Z",
                "analysis_window_start": "2026-08-21T12:00:00Z",
                "analysis_window_end": "2026-08-21T12:01:00Z",
                "engine_build": plan.engine_build,
                "generator_model": plan.generator_model,
            },
            "version_sequence": {
                "phase": work.phase,
                "run_id": assignment["run_id"],
                "version_digest": work.version_reference,
            },
            "ground_truth": {
                "root_cause": expected["root_cause"],
                "category": expected["category"],
                "severity": expected["severity"],
                "finding_count": assignment["expected"]["finding_count"],
                "fix_boundary": expected["fix"]["boundary"],
            },
            "mutation": {
                "healthy_digest": "sha256:" + ("a" * 64),
                "faulted_digest": work.version_reference,
                "sanitized_delta": "Synthetic reviewed mutation.",
            },
            "trace_evidence": traces,
            "prior_trace_ids": [],
            "insights": [],
            "previous_insight": None,
        }
    )


def _persist_evidence_bundle(
    tmp_path: Path,
    plan: PlanInput,
    work: VersionWork,
    assignment: dict[str, Any],
    bundle: dict[str, Any],
) -> tuple[str, Path]:
    content = json.dumps(bundle, indent=2, sort_keys=True).encode("ascii") + b"\n"
    path = (
        tmp_path
        / "artifacts"
        / plan.plan_id
        / "evidence"
        / f"{assignment['scenario_id']}-{work.phase}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    record = LocalArtifactStore(tmp_path / "artifacts").put(
        f"{plan.plan_id}/evidence/{assignment['scenario_id']}-{work.phase}.json",
        content,
        opaque_reference("test-owner"),
    )
    return record.reference, path


def test_load_evidence_bundle_normalizes_exact_legacy_project_in_memory(
    tmp_path: Path,
) -> None:
    payload, plan = _plan()
    hooks, _, _, _, _ = _prepared_hooks(tmp_path, [])
    assignment, work = _single_version_assignment(payload, plan)
    legacy_reference = opaque_reference(hooks._project.project_id)
    symbolic_reference = opaque_reference(f"runtime:project:{plan.plan_id}")
    raw = _synthetic_evidence_bundle(
        hooks,
        plan,
        work,
        assignment,
        legacy_reference,
    )
    reference, path = _persist_evidence_bundle(
        tmp_path,
        plan,
        work,
        assignment,
        raw,
    )
    original_bytes = path.read_bytes()

    normalized = hooks.load_evidence_bundle(
        work,
        str(assignment["scenario_id"]),
        reference,
    )

    assert {item["project_reference"] for item in normalized["trace_evidence"]} == {
        symbolic_reference
    }
    assert normalized["bundle_hash"] != raw["bundle_hash"]
    assert normalized["bundle_hash"] == content_hash(
        {
            key: value
            for key, value in normalized.items()
            if key != "bundle_hash"
        }
    )
    validate_evidence_bundle(normalized)
    assert path.read_bytes() == original_bytes


def test_load_evidence_bundle_accepts_symbolic_project_without_mutation(
    tmp_path: Path,
) -> None:
    payload, plan = _plan()
    hooks, _, _, _, _ = _prepared_hooks(tmp_path, [])
    assignment, work = _single_version_assignment(payload, plan)
    symbolic_reference = opaque_reference(f"runtime:project:{plan.plan_id}")
    raw = _synthetic_evidence_bundle(
        hooks,
        plan,
        work,
        assignment,
        symbolic_reference,
    )
    reference, path = _persist_evidence_bundle(
        tmp_path,
        plan,
        work,
        assignment,
        raw,
    )
    original_bytes = path.read_bytes()

    loaded = hooks.load_evidence_bundle(
        work,
        str(assignment["scenario_id"]),
        reference,
    )

    assert loaded == raw
    assert loaded["bundle_hash"] == raw["bundle_hash"]
    assert path.read_bytes() == original_bytes


def test_load_evidence_bundle_validates_original_hash_before_normalization(
    tmp_path: Path,
) -> None:
    payload, plan = _plan()
    hooks, _, _, _, _ = _prepared_hooks(tmp_path, [])
    assignment, work = _single_version_assignment(payload, plan)
    raw = _synthetic_evidence_bundle(
        hooks,
        plan,
        work,
        assignment,
        opaque_reference(hooks._project.project_id),
    )
    raw["bundle_hash"] = "sha256:" + ("f" * 64)
    reference, _ = _persist_evidence_bundle(
        tmp_path,
        plan,
        work,
        assignment,
        raw,
    )
    with pytest.raises(ContractError, match="bundle_hash"):
        hooks.load_evidence_bundle(
            work,
            str(assignment["scenario_id"]),
            reference,
        )


def test_load_evidence_bundle_validates_normalized_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload, plan = _plan()
    hooks, _, _, _, _ = _prepared_hooks(tmp_path, [])
    assignment, work = _single_version_assignment(payload, plan)
    raw = _synthetic_evidence_bundle(
        hooks,
        plan,
        work,
        assignment,
        opaque_reference(hooks._project.project_id),
    )
    reference, _ = _persist_evidence_bundle(
        tmp_path,
        plan,
        work,
        assignment,
        raw,
    )
    monkeypatch.setattr(live, "_digest", lambda _value: "sha256:" + ("f" * 64))

    with pytest.raises(ContractError, match="bundle_hash"):
        hooks.load_evidence_bundle(
            work,
            str(assignment["scenario_id"]),
            reference,
        )


@pytest.mark.parametrize("case", ["mixed", "foreign", "unbound"])
def test_load_evidence_bundle_rejects_untrusted_legacy_project_provenance(
    tmp_path: Path,
    case: str,
) -> None:
    payload, plan = _plan()
    hooks, _, _, _, _ = _prepared_hooks(tmp_path, [])
    assignment, work = _single_version_assignment(payload, plan)
    legacy_reference = opaque_reference(hooks._project.project_id)
    project_reference = (
        opaque_reference("foreign-project")
        if case == "foreign"
        else legacy_reference
    )
    second_reference = (
        opaque_reference(f"runtime:project:{plan.plan_id}")
        if case == "mixed"
        else None
    )
    raw = _synthetic_evidence_bundle(
        hooks,
        plan,
        work,
        assignment,
        project_reference,
        second_project_reference=second_reference,
    )
    reference, _ = _persist_evidence_bundle(
        tmp_path,
        plan,
        work,
        assignment,
        raw,
    )
    if case == "unbound":
        hooks._project = None

    with pytest.raises(RuntimeFailure) as caught:
        hooks.load_evidence_bundle(
            work,
            str(assignment["scenario_id"]),
            reference,
        )

    assert caught.value.code in {
        "evidence_reference_incomplete",
        "runtime_preflight_required",
    }


def test_normalized_legacy_evidence_supports_daily_status_and_scoring(
    tmp_path: Path,
) -> None:
    payload, plan = _plan()
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
    )
    hooks.preflight(plan, dry_run=True)
    project = ProjectResources(
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
    assignment, work = _single_version_assignment(payload, plan)
    raw = _synthetic_evidence_bundle(
        hooks,
        plan,
        work,
        assignment,
        opaque_reference(project.project_id),
    )
    reference, path = _persist_evidence_bundle(
        tmp_path,
        plan,
        work,
        assignment,
        raw,
    )
    original_bytes = path.read_bytes()

    subset_payload = deepcopy(payload)
    subset_payload["assignments"] = [deepcopy(assignment)]
    subset_plan = PlanInput.from_daily_plan(subset_payload)
    subset_work = next(iter(subset_plan.agents.values()))[-1]
    evidence_result = {
        "evidence_count": 1,
        "evidence_references": [reference],
    }
    project_key = f"{subset_plan.plan_id}:project"
    project_result = {
        "project_reference": opaque_reference(project.project_id),
        "project_name_reference": opaque_reference(project.project_name),
        "managed": project.managed,
    }
    evidence_key = f"{subset_work.key}:evidence"
    hooks._persist_private_receipt(
        project_key,
        project_result,
        {"kind": "project", "project": hooks._project_payload(project)},
    )
    hooks._persist_private_receipt(
        evidence_key,
        evidence_result,
        {"kind": "evidence", "work_key": subset_work.key, "provenance": {}},
    )
    assert hooks._project is None

    state = RunState(
        subset_plan.plan_id,
        subset_plan.reference,
        status="succeeded",
        phase="complete",
        checkpoints={
            project_key: content_hash(project_result),
            evidence_key: content_hash(evidence_result),
        },
    )
    status = build_daily_status(
        subset_payload,
        subset_plan,
        state,
        hooks,
        tmp_path / "packages",
    )
    validate_daily_status_packages(status, tmp_path / "packages")
    normalized = hooks.load_evidence_bundle(
        work,
        str(assignment["scenario_id"]),
        reference,
    )
    score = score_run(payload, [normalized], [])
    package = json.loads(
        (
            tmp_path
            / "packages"
            / f"{assignment['scenario_id']}-primary-package.json"
        ).read_text(encoding="ascii")
    )

    assert hooks._project == project
    assert status["evidence"][0]["artifact_reference"] != reference
    assert status["evidence"][0]["bundle_hash"] == package["evidence"]["bundle_hash"]
    assert package["evidence"]["bundle_hash"] != normalized["bundle_hash"]
    assert "provenance_failure" not in score["violations"]
    assert path.read_bytes() == original_bytes
    assert "sha256:" + hashlib.sha256(original_bytes).hexdigest() == reference


def test_live_deploy_preserves_transient_poll_timeout_for_orchestrator_retry(
    tmp_path: Path,
) -> None:
    hooks, plan, _, _, _ = _prepared_hooks(tmp_path, [])
    work = next(
        work
        for versions in plan.agents.values()
        for work in versions
        if work.agent_type == "prompt"
    )

    class TimedOutDeployments(_Deployments):
        def deploy_prompt(
            self,
            *,
            agent_name,
            definition,
            run_id,
            create_agent,
            cancelled=None,
        ):
            raise DeploymentPollError(
                "Synthetic poll timeout.",
                DeploymentReceipt(
                    agent_name,
                    "1",
                    "prompt",
                    live.canonical_json_digest(definition),
                    run_id,
                    "poll_error",
                ),
                code="agent_deployment_timeout",
                transient=True,
            )

    deployments = TimedOutDeployments()
    hooks._deployment_client = deployments

    with pytest.raises(RuntimeFailure) as caught:
        hooks.deploy(work, idempotency_key=f"{work.key}:deploy")
    assert caught.value.code == "agent_deployment_timeout"
    assert caught.value.transient is True
    hooks.cancel(work)
    assert deployments.cleaned


def test_create_timeout_retry_recovers_exact_version_before_recreate(
    tmp_path: Path,
) -> None:
    hooks, plan, _, _, _ = _prepared_hooks(tmp_path, [])
    work = next(
        item
        for versions in plan.agents.values()
        for item in versions
        if item.agent_type == "prompt"
    )

    class CreateTimeoutThenRecover(_Deployments):
        def __init__(self):
            super().__init__()
            self.recover_calls = 0
            self.create_calls = 0
            self.created_receipt: DeploymentReceipt | None = None

        def recover_version(self, **kwargs):
            self.recover_calls += 1
            if self.created_receipt is not None:
                return self.created_receipt, True
            return None, False

        def deploy_prompt(self, **kwargs):
            self.create_calls += 1
            self.created_receipt = self._receipt(
                "prompt",
                kwargs["agent_name"],
                kwargs["definition"],
                kwargs["run_id"],
            )
            raise DeploymentHttpError(408)

    deployments = CreateTimeoutThenRecover()
    hooks._deployment_client = deployments
    orchestrator = ProductionOrchestrator(
        hooks,
        tmp_path / "state.json",
        sleep=lambda _seconds: None,
    )

    result = orchestrator._retry(
        lambda: hooks.deploy(work, idempotency_key=f"{work.key}:deploy")
    )

    assert result["status"] == "active"
    assert deployments.create_calls == 1
    assert deployments.recover_calls == 2


def test_persistent_create_timeout_exhausts_bounded_recovery_retries(
    tmp_path: Path,
) -> None:
    hooks, plan, _, _, _ = _prepared_hooks(tmp_path, [])
    work = next(
        item
        for versions in plan.agents.values()
        for item in versions
        if item.agent_type == "prompt"
    )

    class PersistentTimeout(_Deployments):
        def __init__(self):
            super().__init__()
            self.recover_calls = 0
            self.create_calls = 0

        def recover_version(self, **_kwargs):
            self.recover_calls += 1
            return None, False

        def deploy_prompt(self, **_kwargs):
            self.create_calls += 1
            raise DeploymentHttpError(408, retry_after_seconds=90)

    deployments = PersistentTimeout()
    hooks._deployment_client = deployments
    orchestrator = ProductionOrchestrator(
        hooks,
        tmp_path / "state.json",
        retry_attempts=3,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(RuntimeFailure) as caught:
        orchestrator._retry(
            lambda: hooks.deploy(work, idempotency_key=f"{work.key}:deploy")
        )

    assert caught.value.code == "agent_deployment_request_timeout"
    assert caught.value.transient is True
    assert caught.value.details == {
        "http_status": 408,
        "retry_after_seconds": 90,
    }
    assert deployments.recover_calls == 3
    assert deployments.create_calls == 3


def test_cancelled_poll_error_cleans_provisional_receipt_without_holding_hook_lock(
    tmp_path: Path,
) -> None:
    hooks, plan, _, _, _ = _prepared_hooks(tmp_path, [])
    work = next(
        work
        for versions in plan.agents.values()
        for work in versions
        if work.agent_type == "prompt"
    )

    class CancelledPoll(_Deployments):
        def deploy_prompt(
            self,
            *,
            agent_name,
            definition,
            run_id,
            create_agent,
            cancelled=None,
        ):
            receipt = DeploymentReceipt(
                agent_name,
                "late-version",
                "prompt",
                live.canonical_json_digest(definition),
                run_id,
                "failed",
            )
            hooks._cancel_events.setdefault(work.key, Event()).set()
            raise DeploymentPollError("Synthetic CodeError.", receipt)

        def cleanup_version(self, receipt):
            acquired = Event()

            def acquire_hook_lock():
                with hooks._lock:
                    acquired.set()

            thread = Thread(target=acquire_hook_lock)
            thread.start()
            thread.join(timeout=1)
            assert acquired.is_set()
            return super().cleanup_version(receipt)

    deployments = CancelledPoll()
    hooks._deployment_client = deployments

    with pytest.raises(RuntimeFailure) as caught:
        hooks.deploy(work, idempotency_key=f"{work.key}:deploy")

    assert caught.value.code == "run_cancelled"
    assert deployments.cleaned == [f"{work.agent_name}:late-version"]


def test_cancelled_poll_error_retains_cleanup_failure_code(tmp_path: Path) -> None:
    hooks, plan, _, _, _ = _prepared_hooks(tmp_path, [])
    work = next(
        work
        for versions in plan.agents.values()
        for work in versions
        if work.agent_type == "prompt"
    )

    class CancelledPollCleanupFailure(_Deployments):
        def deploy_prompt(
            self,
            *,
            agent_name,
            definition,
            run_id,
            create_agent,
            cancelled=None,
        ):
            receipt = DeploymentReceipt(
                agent_name,
                "late-version",
                "prompt",
                live.canonical_json_digest(definition),
                run_id,
                "failed",
            )
            hooks._cancel_events.setdefault(work.key, Event()).set()
            raise DeploymentPollError("Synthetic CodeError.", receipt)

        def cleanup_version(self, _receipt):
            raise DeploymentCleanupError("Synthetic cleanup conflict.")

    hooks._deployment_client = CancelledPollCleanupFailure()

    with pytest.raises(RuntimeFailure) as caught:
        hooks.deploy(work, idempotency_key=f"{work.key}:deploy")

    assert caught.value.code == "cancel_partial_failure"
    assert caught.value.details["failure_codes"] == [
        "deployment_cleanup_sessions_active"
    ]


def test_concurrent_late_cleanup_callers_share_the_same_failure(
    tmp_path: Path,
) -> None:
    hooks, plan, _, _, _ = _prepared_hooks(tmp_path, [])
    work = next(iter(plan.agents.values()))[0]
    receipt = DeploymentReceipt(
        work.agent_name,
        "late-version",
        work.agent_type,
        "sha256:" + ("d" * 64),
        work.run_id,
        "failed",
    )
    started = Event()
    release = Event()

    class SlowCleanupFailure:
        calls = 0

        def cleanup_version(self, _receipt):
            self.calls += 1
            started.set()
            assert release.wait(timeout=2)
            raise DeploymentCleanupError("Synthetic cleanup conflict.")

    deployments = SlowCleanupFailure()
    failures: list[BaseException] = []
    identity = (work.agent_name, work.version_reference)
    owner = Thread(
        target=lambda: _capture_failure(
            failures,
            lambda: hooks._cleanup_cancelled_deployment(
                deployments,
                identity,
                receipt,
            ),
        )
    )
    waiter = Thread(
        target=lambda: _capture_failure(
            failures,
            lambda: hooks._cleanup_cancelled_deployment(
                deployments,
                identity,
                receipt,
            ),
        )
    )
    owner.start()
    assert started.wait(timeout=2)
    waiter.start()
    assert waiter.is_alive()
    release.set()
    owner.join(timeout=2)
    waiter.join(timeout=2)

    assert deployments.calls == 1
    assert len(failures) == 2
    assert all(
        isinstance(error, RuntimeFailure)
        and error.code == "cancel_partial_failure"
        and error.details["failure_codes"]
        == ["deployment_cleanup_sessions_active"]
        for error in failures
    )


def test_live_deploy_cleans_exact_version_when_cancellation_arrives_late(
    tmp_path: Path,
) -> None:
    hooks, plan, deployments, _, _ = _prepared_hooks(tmp_path, [])
    work = next(
        work
        for versions in plan.agents.values()
        for work in versions
        if work.agent_type == "prompt"
    )

    class CancelAfterCreate(_Deployments):
        def deploy_prompt(self, **kwargs):
            receipt = super().deploy_prompt(**kwargs)
            hooks._cancel_events.setdefault(work.key, Event()).set()
            return receipt

    late = CancelAfterCreate()
    hooks._deployment_client = late
    with pytest.raises(RuntimeFailure) as caught:
        hooks.deploy(work, idempotency_key=f"{work.key}:deploy")

    assert caught.value.code == "run_cancelled"
    assert late.cleaned == [f"{work.agent_name}:1"]


def test_live_deploy_retains_late_cleanup_failure_code(tmp_path: Path) -> None:
    hooks, plan, _, _, _ = _prepared_hooks(tmp_path, [])
    work = next(
        work
        for versions in plan.agents.values()
        for work in versions
        if work.agent_type == "prompt"
    )

    class FailedLateCleanup(_Deployments):
        def deploy_prompt(self, **kwargs):
            receipt = super().deploy_prompt(**kwargs)
            hooks._cancel_events.setdefault(work.key, Event()).set()
            return receipt

        def cleanup_version(self, _receipt):
            raise DeploymentCleanupError("Synthetic cleanup conflict.")

    hooks._deployment_client = FailedLateCleanup()
    with pytest.raises(RuntimeFailure) as caught:
        hooks.deploy(work, idempotency_key=f"{work.key}:deploy")

    assert caught.value.code == "cancel_partial_failure"
    assert caught.value.details["failure_codes"] == [
        "deployment_cleanup_sessions_active"
    ]


def test_hosted_deployments_are_process_serialized_while_prompts_overlap(
    tmp_path: Path,
) -> None:
    first, plan, _, _, _ = _prepared_hooks(tmp_path / "first", [])
    second, _, _, _, _ = _prepared_hooks(tmp_path / "second", [])
    prompt_works = [
        versions[0]
        for versions in plan.agents.values()
        if versions[0].agent_type == "prompt"
    ]
    hosted_works = [
        versions[0]
        for versions in plan.agents.values()
        if versions[0].agent_type != "prompt"
    ]
    assert len(prompt_works) >= 2
    assert len(hosted_works) >= 2

    class ConcurrencyDeployments(_Deployments):
        def __init__(self):
            super().__init__()
            self.counter_lock = Lock()
            self.receipt_lock = Lock()
            self.hosted_active = 0
            self.hosted_max = 0
            self.prompt_active = 0
            self.prompt_max = 0
            self.prompt_barrier = Barrier(2)

        def _hosted_call(self, operation):
            with self.counter_lock:
                self.hosted_active += 1
                self.hosted_max = max(self.hosted_max, self.hosted_active)
            try:
                time.sleep(0.02)
                return operation()
            finally:
                with self.counter_lock:
                    self.hosted_active -= 1

        def recover_version(self, **kwargs):
            if kwargs["agent_type"] != "prompt":
                return self._hosted_call(
                    lambda: super(ConcurrencyDeployments, self).recover_version(
                        **kwargs
                    )
                )
            return super().recover_version(**kwargs)

        def _receipt(self, *args, **kwargs):
            with self.receipt_lock:
                return super()._receipt(*args, **kwargs)

        def deploy_prompt(self, **kwargs):
            with self.counter_lock:
                self.prompt_active += 1
                self.prompt_max = max(self.prompt_max, self.prompt_active)
            try:
                self.prompt_barrier.wait(timeout=2)
                return super().deploy_prompt(**kwargs)
            finally:
                with self.counter_lock:
                    self.prompt_active -= 1

        def deploy_hosted_source(self, **kwargs):
            return self._hosted_call(
                lambda: super(ConcurrencyDeployments, self).deploy_hosted_source(
                    **kwargs
                )
            )

        def deploy_hosted_container(self, **kwargs):
            return self._hosted_call(
                lambda: super(
                    ConcurrencyDeployments,
                    self,
                ).deploy_hosted_container(**kwargs)
            )

    deployments = ConcurrencyDeployments()
    first._deployment_client = deployments
    second._deployment_client = deployments

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda pair: pair[0].deploy(
                    pair[1],
                    idempotency_key=pair[1].key + ":deploy",
                ),
                ((first, hosted_works[0]), (second, hosted_works[1])),
            )
        )
    assert deployments.hosted_max == 1

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda pair: pair[0].deploy(
                    pair[1],
                    idempotency_key=pair[1].key + ":deploy",
                ),
                ((first, prompt_works[0]), (second, prompt_works[1])),
            )
        )
    assert deployments.prompt_max == 2


def test_insight_lookback_covers_delayed_resume_and_clamps() -> None:
    start = datetime(2026, 8, 21, 12, tzinfo=UTC)
    assert live._insight_lookback_hours(
        start,
        start + timedelta(minutes=10),
    ) == 3
    assert live._insight_lookback_hours(
        start,
        start + timedelta(hours=4),
    ) == 5
    assert live._insight_lookback_hours(
        start,
        start + timedelta(hours=3000),
    ) == 2160
    with pytest.raises(RuntimeFailure, match="timezone-aware"):
        live._insight_lookback_hours(start.replace(tzinfo=None), start)


def test_delayed_insight_run_lookback_still_contains_original_traffic(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 8, 21, 12, tzinfo=UTC)

    class DelayedInsights(_Insights):
        def create_run(self, _monitor, *, lookback_hours):
            assert lookback_hours == 5
            return {"id": "delayed-run"}

        def collect_run(self, monitor, run_id, **kwargs):
            assert kwargs["lookback_hours"] == 5
            return (
                {
                    "id": run_id,
                    "monitor_id": monitor,
                    "status": "succeeded",
                    "start_time": (start - timedelta(minutes=1)).isoformat(),
                    "end_time": (start + timedelta(hours=4, minutes=1)).isoformat(),
                },
                [],
            )

    hooks, plan, _, _, _ = _prepared_hooks(
        tmp_path,
        [start + timedelta(hours=4)],
    )
    work = next(iter(plan.agents.values()))[0]
    deployment = DeploymentReceipt(
        work.agent_name,
        "7",
        work.agent_type,
        "sha256:" + ("d" * 64),
        work.run_id,
        "active",
    )
    hooks._deployments[(work.agent_name, work.version_reference)] = deployment
    hooks._windows[work.key] = WindowBinding(
        work.window.start_identity,
        work.window.end_identity,
        start,
        start + timedelta(minutes=1),
    )
    hooks._checkpoints[work.key] = InsightCheckpoint(
        start - timedelta(minutes=1),
        {},
        {},
    )
    hooks._telemetry[work.key] = (
        TraceCorrelation("a" * 32, 2, 1, ("b" * 16,), start),
    )
    hooks._insights = DelayedInsights()

    result = hooks.run_insights(
        work,
        {},
        idempotency_key=work.key + ":delayed-insights",
    )
    assert result["analysis_window"]["start"] < start.isoformat()
    assert hooks._insight_lookbacks[work.key] == 5


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


def test_deploy_wraps_materialization_contract_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hooks, plan, _, _, _ = _prepared_hooks(tmp_path, [])
    work = next(iter(plan.agents.values()))[0]

    def reject_materialization(*_args, **_kwargs):
        raise live.RuntimeContractError("Synthetic materialization contract failure.")

    monkeypatch.setattr(live, "materialize_version", reject_materialization)

    with pytest.raises(RuntimeFailure) as caught:
        hooks.deploy(work, idempotency_key=work.key + ":deploy")
    assert caught.value.code == "agent_deployment_failed"


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


def test_private_invocation_receipt_roundtrip_and_legacy_response_id_fallback() -> None:
    current = InvocationReceipt(
        fixture_id="fixture",
        agent_name="aiq-001-weather-test",
        agent_version="2",
        response_id="response-final",
        invocation_id="invocation-final",
        request_id="request-final",
        session_id=None,
        output_text="done",
        called_tools=("geocode",),
        response_ids=("response-initial", "response-final"),
    )
    payload = LiveRuntimeHooks._invocation_payload(current)
    assert payload["response_ids"] == ["response-initial", "response-final"]
    assert LiveRuntimeHooks._restore_invocation(payload) == current

    legacy = dict(payload)
    del legacy["response_ids"]
    restored = LiveRuntimeHooks._restore_invocation(legacy)
    assert restored.response_id == "response-final"
    assert restored.response_ids == ("response-final",)


def test_prompt_turns_expand_to_exact_scenario_bound_telemetry_expectations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 8, 21, 12, tzinfo=UTC)
    hooks, plan, _, _, _ = _prepared_hooks(tmp_path, [])
    work = next(
        item
        for versions in plan.agents.values()
        for item in versions
        if item.agent_type == "prompt"
    )
    fixture_id = str(work.assignments[0]["scenario_id"]) + ":0"
    receipt = DeploymentReceipt(
        work.agent_name,
        "2",
        "prompt",
        work.version_reference,
        work.run_id,
        "active",
    )
    hooks._deployments[(work.agent_name, work.version_reference)] = receipt
    hooks._invocations[work.key] = (
        InvocationReceipt(
            fixture_id,
            work.agent_name,
            "2",
            "response-final",
            "invocation-final",
            "request-final",
            None,
            "done",
            ("geocode",),
            ("response-initial", "response-final"),
        ),
    )
    hooks._invocation_failures[work.key] = ()
    hooks._windows[work.key] = WindowBinding(
        work.window.start_identity,
        work.window.end_identity,
        start,
        start + timedelta(seconds=10),
    )
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
                start + timedelta(seconds=index + 1),
                index,
            )
            for index, _ in enumerate(expectations)
        ]

    monkeypatch.setattr(live, "wait_for_correlated_traces", correlate)
    result = hooks.wait_ingestion(
        work,
        {},
        idempotency_key=work.key + ":expanded-ingestion",
    )

    expectations = captured["expectations"]
    assert result["operation_count"] == 2
    assert [item.response_id for item in expectations] == [
        "response-initial",
        "response-final",
    ]
    assert [item.invocation_id for item in expectations] == [
        None,
        "invocation-final",
    ]
    assert [item.required_operations for item in expectations] == [
        frozenset({"invoke_agent"}),
        frozenset({"invoke_agent", "chat"}),
    ]
    scenario_id = fixture_id.split(":", 1)[0]
    assert len(hooks._scenario_telemetry[work.key][scenario_id]) == 2


def test_recovered_deployment_receipt_is_cached_before_invocation(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 8, 21, 12, tzinfo=UTC)
    hooks, plan, deployments, invocations, _ = _prepared_hooks(
        tmp_path,
        [start, start + timedelta(seconds=1)],
    )
    work = next(
        work
        for versions in plan.agents.values()
        for work in versions
        if work.agent_type == "prompt"
    )
    materialized = materialize_version(
        work,
        project_endpoint=hooks._project.project_endpoint,
        model_deployment=hooks._config.azure.terra_agent_deployment,
        ticket_image=IMAGE,
    )
    artifact_digest, _, _ = live._materialized_artifact_identity(materialized)
    receipt = DeploymentReceipt(
        work.agent_name,
        "recovered-version",
        "prompt",
        artifact_digest,
        work.run_id,
        "active",
    )
    deployments.recovered[(work.agent_name, work.run_id, artifact_digest)] = receipt
    public = hooks.deploy(work, idempotency_key=work.key + ":deploy")
    assert deployments.routes == []
    assert hooks._deployments[(work.agent_name, work.version_reference)] == receipt
    hooks.invoke(work, public, idempotency_key=work.key + ":invoke")
    assert invocations.inputs


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
    started = first._load_private_receipt(work.key + ":invoke:started")
    assert started is not None
    assert started["private"]["invocation_start"] == start.isoformat()

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
    assert result["window_binding"]["realized_start"] == start.isoformat()

    captured: dict[str, object] = {}

    def correlate(*_args, **kwargs):
        expectations = kwargs["expectations"]
        captured["expectations"] = expectations
        captured["start"] = kwargs["start"]
        return [
            TraceCorrelation(
                f"{index + 1:032x}",
                1,
                1,
                (f"{index + 1:016x}",),
                start + timedelta(seconds=1),
                index,
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
    assert captured["start"] == start
    assert len(expectations) == fixture_count
    assert all(item.identifiers() for item in expectations)
    assert all(
        item.model_identities()
        == frozenset(
            {
                "terra-agents",
                "gpt-5.6-terra-2026-08-01",
            }
        )
        for item in expectations
    )
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
    hooks, plan, _, _, _ = _prepared_hooks(
        tmp_path,
        [start + timedelta(seconds=11)],
    )
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
    assert resumed._insight_lookbacks[work.key] == 3

    allow_completion.set()
    thread.join(5)
    assert not thread.is_alive()
    assert failures == []


def test_insight_run_created_after_abort_is_cancelled_by_owner(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 8, 21, 12, tzinfo=UTC)
    hooks, plan, _, _, _ = _prepared_hooks(
        tmp_path,
        [start + timedelta(seconds=11)],
    )
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
    create_started = Event()
    allow_create = Event()

    class BlockingCreateInsights(_Insights):
        def create_run(self, monitor, *, lookback_hours):
            create_started.set()
            assert allow_create.wait(5)
            return super().create_run(
                monitor,
                lookback_hours=lookback_hours,
            )

    insights = BlockingCreateInsights()
    hooks._insights = insights
    failures: list[RuntimeFailure] = []

    def execute() -> None:
        try:
            hooks.run_insights(
                work,
                {"ignored": True},
                idempotency_key=work.key + ":insights",
            )
        except RuntimeFailure as error:
            failures.append(error)

    thread = Thread(target=execute)
    thread.start()
    assert create_started.wait(5)
    hooks.cancel(work)
    allow_create.set()
    thread.join(5)

    assert not thread.is_alive()
    assert failures[0].code == "run_cancelled"
    assert insights.cancelled == [("monitor", "insights-run")]


def test_insight_run_cancellation_coalesces_without_holding_adapter_lock(
    tmp_path: Path,
) -> None:
    hooks, _, _, _, _ = _prepared_hooks(tmp_path, [])
    cancel_started = Event()
    allow_cancel = Event()

    class BlockingCancelInsights(_Insights):
        def __init__(self):
            super().__init__()
            self.cancel_count = 0

        def cancel_run(self, monitor, run_id):
            self.cancel_count += 1
            cancel_started.set()
            assert allow_cancel.wait(5)
            super().cancel_run(monitor, run_id)

    insights = BlockingCancelInsights()
    identity = ("monitor", "insights-run")
    failures: list[BaseException] = []

    def cancel() -> None:
        try:
            hooks._cancel_insight_run_once(insights, identity)
        except BaseException as error:
            failures.append(error)

    first = Thread(target=cancel)
    second = Thread(target=cancel)
    first.start()
    assert cancel_started.wait(5)
    second.start()
    assert hooks._lock.acquire(timeout=1)
    hooks._lock.release()
    allow_cancel.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []
    assert insights.cancel_count == 1
    assert insights.cancelled == [identity]


def test_insight_run_cancellation_releases_claim_after_unexpected_failure(
    tmp_path: Path,
) -> None:
    hooks, _, _, _, _ = _prepared_hooks(tmp_path, [])
    first_started = Event()
    allow_failure = Event()

    class FailThenSucceedInsights(_Insights):
        def __init__(self):
            super().__init__()
            self.cancel_count = 0

        def cancel_run(self, monitor, run_id):
            self.cancel_count += 1
            if self.cancel_count == 1:
                first_started.set()
                assert allow_failure.wait(5)
                raise ValueError("synthetic unexpected cancellation failure")
            super().cancel_run(monitor, run_id)

    insights = FailThenSucceedInsights()
    identity = ("monitor", "insights-run")
    failures: list[BaseException] = []

    def cancel() -> None:
        try:
            hooks._cancel_insight_run_once(insights, identity)
        except BaseException as error:
            failures.append(error)

    first = Thread(target=cancel)
    second = Thread(target=cancel)
    first.start()
    assert first_started.wait(5)
    second.start()
    allow_failure.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert insights.cancel_count == 2
    assert insights.cancelled == [identity]
    assert hooks._cancelling_insight_runs == {}


def test_cancellation_is_not_blocked_by_inflight_endpoint_invocation(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 8, 21, 12, tzinfo=UTC)
    hooks, plan, deployments, _, _ = _prepared_hooks(
        tmp_path,
        [start, start + timedelta(seconds=1)],
    )
    work = next(iter(plan.agents.values()))[0]
    deployed = hooks.deploy(work, idempotency_key=work.key + ":deploy")
    entered = Event()
    release = Event()

    class BlockingInvocations(_Invocations):
        def _invoke(self, receipt, fixture, *, cancelled=None):
            entered.set()
            assert release.wait(5)
            if cancelled is not None and cancelled():
                raise live.RuntimeContractError("Endpoint invocation was cancelled.")
            return super()._invoke(receipt, fixture, cancelled=cancelled)

        invoke_prompt = _invoke
        invoke_hosted = _invoke

    hooks._invocation_client = BlockingInvocations()
    failures: list[BaseException] = []
    thread = Thread(
        target=lambda: _capture_failure(
            failures,
            lambda: hooks.invoke(
                work,
                deployed,
                idempotency_key=work.key + ":invoke",
            ),
        )
    )
    thread.start()
    assert entered.wait(5)
    hooks.cancel(work)
    assert deployments.cleaned
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert failures
    assert isinstance(failures[0], RuntimeFailure)


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
    assert previous["version_digest"] == works[0].version_reference
    hooks._scenario_telemetry[works[0].key] = {
        scenario_id: (TraceCorrelation("a" * 32, 1, 1),),
    }
    hooks._scenario_telemetry[works[1].key] = {
        scenario_id: (TraceCorrelation("b" * 32, 1, 1),),
    }
    assert hooks._prior_scenario_operation_ids(works[2], scenario_id) == {
        "a" * 32,
        "b" * 32,
    }
    assert hooks._prior_scenario_operation_ids(works[0], scenario_id) == set()


def test_transient_endpoint_failure_resumes_without_replaying_completed_fixtures(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 8, 21, 12, tzinfo=UTC)
    hooks, plan, _, _, _ = _prepared_hooks(
        tmp_path,
        [start, start + timedelta(seconds=2)],
    )
    work = next(
        item
        for versions in plan.agents.values()
        for item in versions
        if item.agent_type == "prompt"
        and sum(int(value["traffic_requests"]) for value in item.assignments) >= 2
        and not any(
            hooks._expects_endpoint_failure(
                item,
                str(assignment["scenario_id"]),
            )
            for assignment in item.assignments
        )
    )
    deployed = hooks.deploy(work, idempotency_key=work.key + ":deploy")

    class TransientSecondFixture(_Invocations):
        def __init__(self):
            super().__init__()
            self.failed = False

        def _invoke(self, receipt, fixture, *, cancelled=None):
            if len(self.inputs) == 1 and not self.failed:
                self.failed = True
                self.inputs.append(fixture.input)
                raise InvocationEndpointError(
                    "Agent endpoint returned HTTP 429.",
                    InvocationFailureReceipt(
                        receipt.agent_name,
                        receipt.agent_version,
                        429,
                    ),
                    code="endpoint_rate_limited",
                    transient=True,
                )
            return super()._invoke(receipt, fixture, cancelled=cancelled)

        invoke_prompt = _invoke
        invoke_hosted = _invoke

    invocations = TransientSecondFixture()
    hooks._invocation_client = invocations
    with pytest.raises(RuntimeFailure) as caught:
        hooks.invoke(work, deployed, idempotency_key=work.key + ":invoke")
    assert caught.value.code == "endpoint_rate_limited"
    assert caught.value.transient is True
    assert caught.value.details["http_status"] == 429

    first_fixture_input = invocations.inputs[0]
    result = hooks.invoke(work, deployed, idempotency_key=work.key + ":invoke")
    fixture_count = sum(
        int(assignment["traffic_requests"]) for assignment in work.assignments
    )
    assert result["completed_count"] == fixture_count
    assert invocations.inputs.count(first_fixture_input) == 1
    assert len(invocations.inputs) == fixture_count + 1


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
            start + timedelta(seconds=21),
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
        lambda *_args, **kwargs: [
            TraceCorrelation(
                operation,
                2,
                1,
                spans,
                start + timedelta(seconds=1),
                index,
            )
            for index, _ in enumerate(kwargs["expectations"])
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
    original_correlations = hooks._scenario_telemetry[pair[1].key][scenario_id]
    missing_time = original_correlations[0]
    hooks._scenario_telemetry[pair[1].key][scenario_id] = (
        TraceCorrelation(
            missing_time.operation_id,
            missing_time.span_count,
            missing_time.root_count,
            missing_time.span_ids,
            None,
        ),
        *original_correlations[1:],
    )
    with pytest.raises(RuntimeFailure) as missing:
        hooks.assemble_evidence(
            pair[1],
            insight_run,
            idempotency_key=pair[1].key + ":missing-observed-at",
        )
    assert missing.value.code == "telemetry_provenance_missing"
    hooks._scenario_telemetry[pair[1].key][scenario_id] = original_correlations
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
        == pair[0].version_reference
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
    assert bundle["agent"]["name"] == pair[1].agent_name
    assert bundle["agent"]["version_digest"] == pair[1].version_reference
    assert bundle["run"]["run_id"] == pair[1].run_id
    assert bundle["version_sequence"]["run_id"] == pair[1].run_id
    assert bundle["version_sequence"]["version_digest"] == pair[1].version_reference
    assert all(
        trace["version_digest"] == pair[1].version_reference
        for trace in bundle["trace_evidence"]
    )
    assert {
        trace["project_reference"] for trace in bundle["trace_evidence"]
    } == {opaque_reference(f"runtime:project:{plan.plan_id}")}
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
