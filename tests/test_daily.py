from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_insights_quality.daily as daily
from agent_insights_quality.artifact_io import content_hash
from agent_insights_quality.cli import main
from agent_insights_quality.contracts import ContractError
from agent_insights_quality.judging import project_evidence
from agent_insights_quality.planning import generate_daily_plan
from agent_insights_quality.runtime.config import RuntimeConfig
from agent_insights_quality.runtime.errors import RuntimeFailure
from agent_insights_quality.runtime.orchestrator import PlanInput, PlannedWindow, RunState


SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
SHA = "sha256:" + ("a" * 64)


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "AIQ_AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
        "AIQ_AZURE_RESOURCE_GROUP": "quality-rg",
        "AIQ_FOUNDRY_ACCOUNT": "quality-account",
        "AIQ_APPLICATION_INSIGHTS_RESOURCE_ID": (
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/quality-rg/"
            "providers/Microsoft.Insights/components/quality-appi"
        ),
        "AIQ_CONTAINER_REGISTRY_NAME": "qualityregistry",
        "AIQ_TERRA_AGENT_DEPLOYMENT": "terra-agents",
        "AIQ_TERRA_INSIGHTS_DEPLOYMENT": "terra-insights",
        "AIQ_TERRA_MODEL_VERSION": "2026-08-01",
        "AIQ_ADO_ORGANIZATION_URL": "https://ado.example.invalid",
        "AIQ_ADO_PROJECT": "Quality",
        "AIQ_ADO_TEMPLATE_ID": "template",
        "AIQ_ADO_OWNER_ID": "owner",
        "AIQ_ARTIFACT_BACKEND": "local",
        "AIQ_ARTIFACT_LOCATION": str(tmp_path / "artifacts"),
        "AIQ_AUTOMATION_OWNER": "ninghu",
        "AIQ_MONITOR_OWNERSHIP_RECEIPT": str(tmp_path / "monitors.json"),
    }


class DeploymentCli:
    def __init__(
        self,
        *,
        plan_path: Path | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.commands: list[list[str]] = []
        self.plan_path = plan_path
        self.events = events

    def run(self, arguments, **_kwargs):
        self.commands.append(list(arguments))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def json(self, arguments, **_kwargs):
        arguments = list(arguments)
        self.commands.append(arguments)
        if arguments[:3] == ["account", "list", "--all"]:
            return [{"id": SUBSCRIPTION, "name": "Qualification"}]
        if arguments[:2] == ["account", "show"]:
            return {
                "id": SUBSCRIPTION,
                "tenantId": "22222222-2222-2222-2222-222222222222",
                "user": {"type": "user"},
            }
        if arguments[:2] == ["cloud", "show"]:
            return {"name": "AzureCloud"}
        if arguments[:3] == ["ad", "signed-in-user", "show"]:
            return {"id": "33333333-3333-3333-3333-333333333333"}
        if arguments[:3] == ["deployment", "group", "create"]:
            if self.plan_path is not None:
                assert self.plan_path.is_file()
            if self.events is not None:
                self.events.append("bicep")
            return {"properties": {"provisioningState": "Succeeded"}}
        raise AssertionError(arguments)


def _bundle(payload: dict, work, scenario_id: str) -> dict:
    assignment = next(
        item for item in payload["assignments"] if item["scenario_id"] == scenario_id
    )
    expected = assignment["expected"]
    trace_reference = content_hash({"trace": scenario_id, "phase": work.phase})
    insights = []
    if expected["category"] != "none":
        insights.append(
            {
                "id": f"insight-{scenario_id}",
                "title": "Synthetic finding",
                "description": "A bounded synthetic finding.",
                "category": expected["category"],
                "severity": expected["severity"],
                "trace_count": 1,
                "trace_ids": [trace_reference],
                "proposed_fix": "Apply the reviewed synthetic correction.",
                "fix_kind": "prose",
                "tool_references": [],
                "signature": content_hash({"signature": scenario_id}),
                "evidence_fingerprint": content_hash({"evidence": scenario_id}),
            }
        )
    raw = {
        "schema_version": "1.0.0",
        "bundle_id": (
            "00000000-0000-4000-8000-"
            + hashlib_suffix(scenario_id)
        ),
        "plan_id": payload["plan_id"],
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
            "window_start": "2026-08-21T07:00:00Z",
            "window_end": "2026-08-21T07:01:00Z",
            "analysis_window_start": "2026-08-21T07:00:00Z",
            "analysis_window_end": "2026-08-21T07:01:00Z",
            "engine_build": payload["engine"]["build"],
            "generator_model": payload["engine"]["generator_model"],
        },
        "version_sequence": {
            "phase": work.phase,
            "run_id": assignment["run_id"],
            "version_digest": work.version_reference,
        },
        "ground_truth": {
            "root_cause": "Synthetic reviewed root cause.",
            "category": expected["category"],
            "severity": expected["severity"],
            "finding_count": expected["finding_count"],
            "fix_boundary": "Synthetic reviewed fix boundary.",
        },
        "mutation": {
            "healthy_digest": SHA,
            "faulted_digest": work.version_reference,
            "sanitized_delta": "Synthetic reviewed mutation.",
        },
        "trace_evidence": [
            {
                "trace_id": trace_reference,
                "span_ids": [content_hash({"span": scenario_id})],
                "summary": "Synthetic trace summary.",
                "artifact_reference": content_hash({"artifact": scenario_id}),
                "project_reference": payload["project"]["resource_reference"],
                "agent_id": assignment["agent_id"],
                "version_digest": work.version_reference,
                "observed_at": "2026-08-21T07:00:30Z",
            }
        ],
        "prior_trace_ids": [],
        "insights": insights,
        "run_noise_insights": [],
        "previous_insight": None,
    }
    return project_evidence(raw)


def hashlib_suffix(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()[:12]


class Hooks:
    def __init__(
        self,
        payload: dict,
        shared: dict[str, dict] | None = None,
        *,
        fail_deploy: bool = False,
        events: list[str] | None = None,
    ) -> None:
        self.payload = payload
        self.results = shared if shared is not None else {}
        self.fail_deploy = fail_deploy
        self.events = events
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def _result(self, operation: str, key: str) -> dict:
        with self._lock:
            self.calls.append(operation)
            value = {"result_reference": content_hash({"operation": operation, "key": key})}
            self.results[key] = value
            return value

    def preflight(self, _plan, *, dry_run):
        if self.events is not None:
            self.events.append("preflight")
        return self._result("preflight", str(dry_run))

    def ensure_project(self, _plan, *, idempotency_key):
        return self._result("project", idempotency_key)

    def deploy(self, _work, *, idempotency_key):
        if self.fail_deploy:
            raise RuntimeFailure("deployment_failed", "Synthetic deployment failed.")
        return self._result("deploy", idempotency_key)

    def invoke(self, work, _deployment, *, idempotency_key):
        minute = work.wave * 10 + work.sequence_index * 2
        start = datetime(2026, 8, 21, tzinfo=UTC) + timedelta(minutes=minute)
        value = self._result("invoke", idempotency_key) | {
            "window_binding": {
                "planned_start": (
                    work.window.start_identity
                    if isinstance(work.window, PlannedWindow)
                    else work.window.start.isoformat()
                ),
                "planned_end": (
                    work.window.end_identity
                    if isinstance(work.window, PlannedWindow)
                    else work.window.end.isoformat()
                ),
                "realized_start": start.isoformat(),
                "realized_end": (start + timedelta(minutes=1)).isoformat(),
            }
        }
        self.results[idempotency_key] = value
        return value

    def wait_ingestion(self, _work, _invocation, *, idempotency_key):
        return self._result("ingestion", idempotency_key)

    def run_insights(self, _work, _telemetry, *, idempotency_key):
        return self._result("insights", idempotency_key)

    def assemble_evidence(self, work, _insight_run, *, idempotency_key):
        references = []
        for assignment in work.assignments:
            bundle = _bundle(self.payload, work, assignment["scenario_id"])
            rendered = (
                json.dumps(bundle, indent=2, sort_keys=True).encode("ascii") + b"\n"
            )
            references.append("sha256:" + hashlib.sha256(rendered).hexdigest())
        value = {
            "evidence_count": len(references),
            "evidence_references": references,
        }
        with self._lock:
            self.calls.append("evidence")
            self.results[idempotency_key] = value
        return value

    def recover(self, key, _checkpoint):
        self.calls.append("recover")
        return self.results[key]

    def load_evidence_bundle(self, work, scenario_id, _reference):
        return _bundle(self.payload, work, scenario_id)

    def cancel(self, _work):
        self.calls.append("cancel")

    def finalize_failure(self, _failure, _state):
        return {"artifact_reference": content_hash({"failure": "synthetic"})}


def test_readiness_false_has_no_operational_entrypoint_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("operational entrypoint must not run")

    monkeypatch.setattr(daily, "run_daily", forbidden)
    assert (
        main(
            [
                "run-daily",
                "--report-date",
                "2026-08-21",
                "--output-root",
                str(tmp_path),
            ]
        )
        == 1
    )
    assert called is False
    request = tmp_path / "daily" / "2026" / "08" / "21" / "email-send-request.json"
    assert json.loads(request.read_text(encoding="ascii"))["state"] == "unsent"


def test_finalized_readiness_day_requires_rerun_before_plan_or_azure(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "reports"
    day = output_root / "daily" / "2026" / "08" / "21"
    day.mkdir(parents=True)
    (day / "readiness-failure.json").write_text("{}\n", encoding="ascii")

    with pytest.raises(RuntimeFailure, match="explicit rerun suffix"):
        daily.run_daily(
            date(2026, 8, 21),
            output_root=output_root,
            state_root=tmp_path / "private",
            environment={},
            cli_factory=lambda: (_ for _ in ()).throw(
                AssertionError("terminal readiness record must not touch Azure")
            ),
        )

    assert not (day / "plan.json").exists()


@pytest.mark.parametrize("filename", ["report.json", "report.md"])
def test_finalized_current_report_requires_rerun_before_plan_or_azure(
    tmp_path: Path,
    filename: str,
) -> None:
    output_root = tmp_path / "reports"
    day = output_root / "daily" / "2026" / "08" / "21"
    day.mkdir(parents=True)
    (day / filename).write_text("{}\n", encoding="ascii")

    with pytest.raises(RuntimeFailure, match="explicit rerun suffix"):
        daily.run_daily(
            date(2026, 8, 21),
            output_root=output_root,
            state_root=tmp_path / "private",
            environment={},
            cli_factory=lambda: (_ for _ in ()).throw(
                AssertionError("finalized report must not touch Azure")
            ),
        )

    assert not (day / "plan.json").exists()


def test_interrupted_plan_write_repairs_only_missing_artifact(tmp_path: Path) -> None:
    output_root = tmp_path / "reports"
    payload, target = daily.ensure_daily_plan(date(2026, 8, 21), output_root)
    original = (target / "plan.json").read_bytes()
    (target / "plan.md").unlink()

    resumed, resumed_target = daily.ensure_daily_plan(
        date(2026, 8, 21),
        output_root,
    )

    assert resumed == payload
    assert resumed_target == target
    assert (target / "plan.json").read_bytes() == original
    assert (target / "plan.md").is_file()


def test_bicep_parameters_are_exact_and_never_include_secret_material(
    tmp_path: Path,
) -> None:
    payload = generate_daily_plan(date(2026, 8, 21), rerun=3)
    config = RuntimeConfig.from_env(_environment(tmp_path))
    cli = DeploymentCli()

    receipt = daily.deploy_qualification_project(payload, config, cli)

    command = next(
        command
        for command in cli.commands
        if command[:3] == ["deployment", "group", "create"]
    )
    parameter_index = command.index("--parameters")
    parameters = dict(
        item.split("=", 1) for item in command[parameter_index + 1 :]
    )
    assert parameters == {
        "accountName": "quality-account",
        "projectName": "aiq-20260821-r03",
        "applicationInsightsName": "quality-appi",
        "registryName": "qualityregistry",
        "reportDate": "2026-08-21",
        "expiresOn": "2026-08-28",
        "automationOwner": "ninghu",
        "catalogVersion": payload["catalog_hash"],
        "connectionNameSuffix": "aiq-20260821-r03",
    }
    serialized = json.dumps(command).casefold()
    assert "connectionstring" not in serialized
    assert "instrumentationkey" not in serialized
    assert receipt["status"] == "succeeded"


def test_mixed_case_arm_resource_id_deploys_and_validates_resume(
    tmp_path: Path,
) -> None:
    payload = generate_daily_plan(date(2026, 8, 21), rerun=4)
    environment = _environment(tmp_path)
    environment["AIQ_APPLICATION_INSIGHTS_RESOURCE_ID"] = (
        f"/Subscriptions/{SUBSCRIPTION}/ResourceGroups/quality-rg/"
        "Providers/Microsoft.Insights/Components/Quality-Appi"
    )
    config = RuntimeConfig.from_env(environment)
    receipt_path = tmp_path / "deployment.json"
    first_cli = DeploymentCli()

    first = daily.ensure_qualification_deployment(
        payload,
        config,
        first_cli,
        receipt_path,
        propagation_wait_seconds=0,
        sleeper=lambda _seconds: None,
    )
    command = next(
        value
        for value in first_cli.commands
        if value[:3] == ["deployment", "group", "create"]
    )
    assert "applicationInsightsName=Quality-Appi" in command

    second_cli = DeploymentCli()
    resumed = daily.ensure_qualification_deployment(
        payload,
        config,
        second_cli,
        receipt_path,
        propagation_wait_seconds=900,
        sleeper=lambda _seconds: pytest.fail("completed wait must not repeat"),
    )
    assert resumed == first
    assert second_cli.commands == []


@pytest.mark.parametrize("wait", [-1, 1801, True])
def test_propagation_wait_is_bounded_and_validated(
    tmp_path: Path,
    wait,
) -> None:
    payload = generate_daily_plan(date(2026, 8, 21))
    config = RuntimeConfig.from_env(_environment(tmp_path))
    with pytest.raises(RuntimeFailure, match="between 0 and 1800"):
        daily.ensure_qualification_deployment(
            payload,
            config,
            DeploymentCli(),
            tmp_path / "deployment.json",
            propagation_wait_seconds=wait,
            sleeper=lambda _seconds: None,
        )


def test_pending_propagation_receipt_resumes_without_redeploy(
    tmp_path: Path,
) -> None:
    payload = generate_daily_plan(date(2026, 8, 21))
    config = RuntimeConfig.from_env(_environment(tmp_path))
    receipt_path = tmp_path / "deployment.json"
    first_cli = DeploymentCli()

    def interrupt(_seconds: float) -> None:
        raise RuntimeFailure("interrupted", "Synthetic interruption.")

    with pytest.raises(RuntimeFailure, match="Synthetic interruption"):
        daily.ensure_qualification_deployment(
            payload,
            config,
            first_cli,
            receipt_path,
            sleeper=interrupt,
        )
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["status"] == "deployed"

    waited: list[float] = []
    second_cli = DeploymentCli()
    receipt = daily.ensure_qualification_deployment(
        payload,
        config,
        second_cli,
        receipt_path,
        sleeper=waited.append,
    )
    assert waited == [900]
    assert receipt["status"] == "succeeded"
    assert not any(
        command[:3] == ["deployment", "group", "create"]
        for command in second_cli.commands
    )


def test_daily_success_and_completed_resume_are_idempotent(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "reports"
    state_root = tmp_path / "private"
    payload = generate_daily_plan(date(2026, 8, 21))
    shared: dict[str, dict] = {}
    hooks: list[Hooks] = []
    cli_instances: list[DeploymentCli] = []
    events: list[str] = []
    plan_path = output_root / "daily" / "2026" / "08" / "21" / "plan.json"

    def hooks_factory(_config):
        instance = Hooks(payload, shared, events=events)
        hooks.append(instance)
        return instance

    def cli_factory():
        instance = DeploymentCli(plan_path=plan_path, events=events)
        cli_instances.append(instance)
        return instance

    status_path = daily.run_daily(
        date(2026, 8, 21),
        output_root=output_root,
        state_root=state_root,
        environment=_environment(tmp_path),
        cli_factory=cli_factory,
        hooks_factory=hooks_factory,
        propagation_sleeper=lambda seconds: events.append(f"wait:{seconds}"),
    )
    status = json.loads(status_path.read_text(encoding="ascii"))
    daily.validate_daily_status(status, payload)
    assert status["primary_package_count"] == len(payload["assignments"])
    assert status["workflow"]["ado"] == {
        "mode": "candidate-only",
        "auto_apply": False,
        "write_requests_allowed": False,
    }
    assert SUBSCRIPTION not in json.dumps(status)
    changed = deepcopy(status)
    changed["evidence"][0]["agent_id"] = payload["assignments"][1]["agent_id"]
    changed["status_hash"] = content_hash(
        {key: value for key, value in changed.items() if key != "status_hash"}
    )
    with pytest.raises(ContractError, match="evidence identity"):
        daily.validate_daily_status(changed, payload)
    assert plan_path.is_file()
    assert events.index("bicep") < events.index("wait:900") < events.index("preflight")
    first_remote_calls = [
        call
        for call in hooks[0].calls
        if call in {"project", "deploy", "invoke", "ingestion", "insights", "evidence"}
    ]
    assert first_remote_calls

    assert (
        daily.run_daily(
            date(2026, 8, 21),
            output_root=output_root,
            state_root=state_root,
            environment={},
            cli_factory=lambda: (_ for _ in ()).throw(
                AssertionError("completed handoff must not touch Azure")
            ),
            hooks_factory=lambda _config: (_ for _ in ()).throw(
                AssertionError("completed handoff must not reload the adapter")
            ),
        )
        == status_path
    )

    status_path.unlink()
    resumed = daily.run_daily(
        date(2026, 8, 21),
        output_root=output_root,
        state_root=state_root,
        environment=_environment(tmp_path),
        cli_factory=cli_factory,
        hooks_factory=hooks_factory,
        propagation_sleeper=lambda _seconds: pytest.fail(
            "completed deployment must not repeat the propagation wait"
        ),
    )
    assert resumed == status_path
    assert cli_instances[-1].commands == []
    assert not any(
        call in {"project", "deploy", "invoke", "ingestion", "insights", "evidence"}
        for call in hooks[-1].calls
    )
    assert "preflight" in hooks[-1].calls
    assert "recover" in hooks[-1].calls


def test_daily_status_rejects_incomplete_evidence_references(tmp_path: Path) -> None:
    payload = generate_daily_plan(date(2026, 8, 21))
    plan = PlanInput.from_daily_plan(payload)
    hooks = Hooks(payload)
    checkpoints = {}
    project_key = f"{plan.plan_id}:project"
    hooks.ensure_project(plan, idempotency_key=project_key)
    checkpoints[project_key] = content_hash(hooks.results[project_key])
    for versions in plan.agents.values():
        for work in versions:
            key = f"{work.key}:evidence"
            hooks.assemble_evidence(work, {}, idempotency_key=key)
            checkpoints[key] = content_hash(hooks.results[key])
    final = next(iter(plan.agents.values()))[-1]
    hooks.results[f"{final.key}:evidence"]["evidence_references"].pop()
    state = RunState(
        plan.plan_id,
        plan.reference,
        status="succeeded",
        phase="complete",
        checkpoints=checkpoints,
    )
    with pytest.raises(RuntimeFailure, match="incomplete evidence references"):
        daily.build_daily_status(
            payload,
            plan,
            state,
            hooks,
            tmp_path / "packages",
        )


def test_operational_failure_finalizes_once_and_requires_rerun(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "reports"
    state_root = tmp_path / "private"
    payload = generate_daily_plan(date(2026, 8, 21), rerun=2)
    deploy_cli = DeploymentCli()
    hooks = Hooks(payload, fail_deploy=True)

    with pytest.raises(RuntimeFailure, match="INCONCLUSIVE"):
        daily.run_daily(
            date(2026, 8, 21),
            output_root=output_root,
            state_root=state_root,
            rerun=2,
            max_parallel_agents=1,
            environment=_environment(tmp_path),
            cli_factory=lambda: deploy_cli,
            hooks_factory=lambda _config: hooks,
            propagation_sleeper=lambda _seconds: None,
        )

    target = (
        output_root
        / "daily"
        / "2026"
        / "08"
        / "21"
        / "aiq-20260821-r02"
    )
    report = json.loads((target / "report.json").read_text(encoding="ascii"))
    request_path = (
        state_root
        / "daily"
        / "aiq-20260821-r02"
        / "email-send-request.json"
    )
    request = json.loads(request_path.read_text(encoding="ascii"))
    request_bytes = request_path.read_bytes()
    assert report["status"] == "INCONCLUSIVE"
    assert request["state"] == "unsent"
    assert (target / "failure-email.html").is_file()
    deployment_count = sum(
        command[:3] == ["deployment", "group", "create"]
        for command in deploy_cli.commands
    )
    (target / "report.md").unlink()
    (target / "failure-email.html").unlink()

    with pytest.raises(RuntimeFailure, match="explicit rerun suffix"):
        daily.run_daily(
            date(2026, 8, 21),
            output_root=output_root,
            state_root=state_root,
            rerun=2,
            environment={},
            cli_factory=lambda: (_ for _ in ()).throw(
                AssertionError("finalized failure must not touch Azure")
            ),
            hooks_factory=lambda _config: (_ for _ in ()).throw(
                AssertionError("finalized failure must not reload the adapter")
            ),
        )
    assert (target / "report.md").is_file()
    assert (target / "failure-email.html").is_file()
    assert request_path.read_bytes() == request_bytes
    assert (
        sum(
            command[:3] == ["deployment", "group", "create"]
            for command in deploy_cli.commands
        )
        == deployment_count
    )


def test_unexpected_operational_error_is_sanitized_and_finalized(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "reports"
    state_root = tmp_path / "private"

    def unexpected(_config):
        raise ValueError("private implementation detail")

    with pytest.raises(RuntimeFailure, match="INCONCLUSIVE"):
        daily.run_daily(
            date(2026, 8, 21),
            output_root=output_root,
            state_root=state_root,
            environment=_environment(tmp_path),
            cli_factory=DeploymentCli,
            hooks_factory=unexpected,
            propagation_sleeper=lambda _seconds: None,
        )

    target = output_root / "daily" / "2026" / "08" / "21"
    report = json.loads((target / "report.json").read_text(encoding="ascii"))
    request = json.loads(
        (
            state_root
            / "daily"
            / "aiq-20260821"
            / "email-send-request.json"
        ).read_text(encoding="ascii")
    )
    assert report["status"] == "INCONCLUSIVE"
    assert report["failure"]["failed_phase"] == "runtime_preflight"
    assert "private implementation detail" not in json.dumps(report)
    assert request["state"] == "unsent"
