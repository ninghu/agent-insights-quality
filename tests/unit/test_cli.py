import builtins
from contextlib import asynccontextmanager
from datetime import date
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_insights_quality import catalogs, cli, runner
from agent_insights_quality.email import read_email
from agent_insights_quality.errors import QualityError
from agent_insights_quality.state import RuntimeStore
import test_runner as fake


@pytest.fixture
def app(tmp_path, monkeypatch):
    h = fake.Harness(tmp_path, issues=4)
    fake.fake_storage(monkeypatch)
    from agent_insights_quality import report_context
    h.catalog = fake.replace(h.catalog, targets=tuple(fake.replace(target, expectation={
        "title": "Synthetic reviewed defect", "root_cause": "Synthetic input contradiction",
        "expected_fix": "Honor the synthetic reviewed input",
    }) for target in h.catalog.targets))
    private_config = h.store.root / "config"
    private_config.mkdir(parents=True)
    (private_config / "email-recipient.json").write_text(json.dumps({
        "schema_version": "1.0.0", "purpose": "daily_test", "recipient": "synthetic@example.invalid",
    }))
    monkeypatch.setattr(catalogs, "load_catalog", lambda _: h.catalog)
    monkeypatch.setattr(report_context, "load_catalog", lambda _: h.catalog)
    monkeypatch.setattr(cli, "_committed_inputs", lambda _: None)
    monkeypatch.setattr(runner, "source_revision", lambda _: "a" * 40)
    original = runner.Runner
    def construct(*args, **kwargs):
        return original(*args, **kwargs, now=h.clock.now, monotonic=h.clock.monotonic,
                        sleep=h.clock.sleep, attempts=fake.attempts,
                        deployment_source=lambda _: "deployment-one",
                        changes_since=lambda _: runner.SourceChanges(()))
    monkeypatch.setattr(runner, "Runner", construct)
    calls = []
    @asynccontextmanager
    async def ports(catalog, runtime, run_id, assessment):
        calls.append((run_id, assessment))
        yield h.cloud, h.sol, h.registry
    def invoke(*arguments, day=fake.DAY):
        return cli.main(arguments, root=h.catalog.root,
                        runtime_factory=lambda profile: RuntimeStore(profile, root=h.store.root),
                        ports=ports, today=day)
    h.cli, h.port_calls = invoke, calls
    return h


def last_json(capsys):
    capture = capsys.readouterr()
    return json.loads(capture.out.splitlines()[-1]), capture.err


def test_help_and_module_entry_point_do_not_import_azure_or_hosted_sdk(tmp_path):
    code = r'''
import builtins, runpy, sys
old = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith(("azure.", "agent_framework", "docker")):
        raise AssertionError("SDK imported by help")
    return old(name, *args, **kwargs)
builtins.__import__ = guarded
sys.argv = ["aiq-quality", "--help"]
runpy.run_module("agent_insights_quality", run_name="__main__")
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert all(name in result.stdout for name in (
        "validate", "generate-docs", "run-staging", "run-daily", "status", "email-claim", "email-result",
    ))


def test_cli_test_pipeline_and_claim_actual_outcome_no_raw_stdout(app, capsys):
    assert app.cli("run-daily", "--test-run", "--rerun", "1") == 0
    value, error = last_json(capsys)
    assert not error
    assert value["status"] == "Full" and value["counts"]["correct_issues"] == 4
    assert Path(value["result_path"]).is_file()
    delivery = value["delivery_id"]
    assert "synthetic@example.invalid" not in json.dumps(value)
    assert not app.store.outbox("events").directory.exists()
    assert app.cli("email-claim", "--delivery-id", delivery, "--claim-id", "app-claim") == 0
    claim, _ = last_json(capsys)
    packet = json.loads(Path(claim["request_path"]).read_text())
    assert packet["recipient"] == "synthetic@example.invalid"
    assert packet["test_run"] and packet["rerun"] == 1
    assert "100.0" in packet["html"] and "TEST " in packet["subject"]
    assert "<html" not in json.dumps(claim)
    assert app.cli("email-claim", "--delivery-id", delivery, "--claim-id", "app-claim") == 2
    assert "email_reconciliation_required" in capsys.readouterr().err
    result_file = app.store.root / "actual-send.json"
    result_file.write_text(json.dumps({"synthetic_provider": "accepted-only"}))
    assert app.cli(
        "email-result", "--delivery-id", delivery, "--claim-id", "app-claim",
        "--outcome", "accepted", "--result-file", str(result_file),
    ) == 0
    result, _ = last_json(capsys)
    assert result["status"] == "accepted" and not result["inbox_delivery_confirmed"]
    assert app.cli("run-daily", "--test-run", "--rerun", "1") == 0
    repeated, _ = last_json(capsys)
    assert repeated["email_status"] == "accepted"
    assert app.cli("status") == 0
    status, _ = last_json(capsys)
    assert status["runs"][0]["status"] == "Full"
    assert "synthetic_provider" not in json.dumps(status)


@pytest.mark.parametrize("arguments", [
    ("run-daily", "--test-run"),
    ("run-daily", "--test-run", "--rerun", "0"),
    ("run-daily", "--test-run", "--rerun", "-1"),
    ("run-daily", "--rerun", "1"),
])
def test_private_identity_validation_happens_before_ports(app, arguments):
    assert app.cli(*arguments) == 2
    assert not app.port_calls


def test_private_weekend_preserves_actual_date_and_current_candidate(app, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_official_source", lambda _: pytest.fail("Private trial switched to main"))
    weekend = date(2026, 9, 5)
    assert app.cli("run-daily", "--test-run", "--rerun", "2", day=weekend) == 0
    value, _ = last_json(capsys)
    record = read_email(app.store.outbox("email"), value["delivery_id"])
    assert record.request.report_date == weekend.isoformat()
    assert app.store.run(value["run_id"]).read_completed("run")["source_revision"] == "a" * 40


def test_fixed_private_recipient_schema_precedes_cloud(app):
    path = app.store.root / "config" / "email-recipient.json"
    path.write_text(json.dumps({
        "schema_version": "1.0", "purpose": "daily_test", "recipient": "synthetic@example.invalid",
    }))
    assert app.cli("run-daily", "--test-run", "--rerun", "1") == 2
    assert not app.port_calls


def test_settings_and_sol_configuration_reach_runtime_without_sdk(app):
    path = app.catalog.root / "config"
    path.mkdir(parents=True)
    (path / "runtime.json").write_text(json.dumps({"deployment_workers": 1, "hydration_seconds": 0}))
    (app.store.root / "config" / "assessment.json").write_text(json.dumps({
        "deployment_name": "synthetic-sol", "model": "gpt-5.6-sol",
        "model_version": "2026-07-09", "credential": "azure_cli",
    }))
    assert app.cli("run-daily", "--test-run", "--rerun", "1") == 0
    assert app.port_calls[0][1].deployment_name == "synthetic-sol"
    records = app.store.run(app.port_calls[0][0])
    assert records.read("source")["settings"]["deployment_workers"] == 1
    assert records.read_completed("assessment-settings")["deployment_name"] == "synthetic-sol"


def test_no_runtime_root_recipient_or_source_override_flags():
    for command in ("run-staging", "run-daily", "status", "email-claim"):
        with pytest.raises(SystemExit):
            cli.parser().parse_args([command, "--runtime-root", "synthetic"])
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["run-daily", "--recipient", "synthetic@example.invalid"])


def test_official_main_ref_is_required_but_cli_never_fetches_or_relaunches(tmp_path, monkeypatch):
    calls = []
    def git(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(returncode=0, stdout="main-sha" if "HEAD" in arguments else "different-sha")
    monkeypatch.setattr(cli.subprocess, "run", git)
    with pytest.raises(QualityError, match="official_source_not_fetched_main"):
        cli._official_source(tmp_path)
    assert all(command[1] == "rev-parse" for command in calls)
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="same-sha"))
    cli._official_source(tmp_path)


def test_uncommitted_execution_inputs_cannot_be_mislabeled_as_old_deployments(tmp_path, monkeypatch):
    calls = []
    def git(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(returncode=0, stdout=" M agents/synthetic/v0/definition.json")
    monkeypatch.setattr(cli.subprocess, "run", git)
    with pytest.raises(QualityError, match="source_inputs_not_committed"):
        cli._committed_inputs(tmp_path)
    assert calls[0][1] == "status" and "--untracked-files=all" in calls[0]


def test_production_factory_scoped_metadata_and_current_constructor_contracts(tmp_path, monkeypatch, capsys):
    import asyncio
    from agent_insights_quality import bootstrap, providers, registry
    h = fake.Harness(tmp_path)
    metadata, received, closed = [], {}, []
    async def discover(profile):
        assert profile == "daily"
        return h.cloud.environment
    def azure(arguments):
        metadata.append(arguments)
        return {"properties": {"ConnectionString": "InstrumentationKey=synthetic-private"}}
    def cloud(environment, **kwargs):
        received.update(environment=environment, **kwargs)
        return h.cloud
    def sol(environment, *, deployment):
        received["sol_deployment"] = deployment
        return h.sol
    class Blob:
        def __init__(self, environment):
            assert environment is h.cloud.environment
        async def read(self):
            return None
        async def close(self):
            closed.append(True)
    monkeypatch.setattr(bootstrap, "discover_environment", discover)
    monkeypatch.setattr(bootstrap, "azure_json", azure)
    monkeypatch.setattr(providers, "AzureRuntime", cloud)
    monkeypatch.setattr(providers, "AzureSol", sol)
    monkeypatch.setattr(registry, "AzureRegistryBlob", Blob)
    fake.fake_storage(monkeypatch)
    from agent_insights_quality.settings import AssessmentSettings
    async def construct():
        with h.store.ownership():
            async with cli.production_ports(h.catalog, h.store, "factory", AssessmentSettings()) as ports:
                assert ports[:2] == (h.cloud, h.sol)
                received["images"].persist({
                    "artifact_key": "synthetic-artifact", "state": "pending", "run_id": "synthetic-build",
                })
    asyncio.run(construct())
    assert len(metadata) == 1 and h.cloud.environment.application_insights_resource_id in metadata[0]
    assert received["sol_deployment"] == "sol-assessment"
    assert received["hosted_environment"] == {
        "FOUNDRY_PROJECT_ENDPOINT": h.cloud.environment.project_endpoint,
        "APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=synthetic-private",
        "ENABLE_SENSITIVE_DATA": "true",
    }
    records = h.store.run("factory")
    assert received["images"].workspace == records.directory / "build"
    assert records.read("images")["records"]["synthetic-artifact"]["run_id"] == "synthetic-build"
    assert closed == [True]
    assert "InstrumentationKey" not in capsys.readouterr().out


def test_generate_docs_calls_catalog_views_and_validation_stays_offline(app, monkeypatch, capsys):
    from agent_insights_quality import catalog_docs
    guarded = builtins.__import__
    def no_sdk(name, *args, **kwargs):
        if name.startswith(("azure.", "agent_framework", "docker")):
            pytest.fail("Offline command imported a provider SDK")
        return guarded(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_sdk)
    called = []
    generated = []
    def generate(catalog):
        generated.append(catalog)
        return ("AGENT_CATALOG.md", "ISSUE_CATALOG.md")
    monkeypatch.setattr(catalog_docs, "generate_catalog_views", generate)
    monkeypatch.setattr(catalogs, "validate_catalog", lambda catalog: called.append(catalog))
    assert app.cli("generate-docs") == 0
    assert json.loads(capsys.readouterr().out)["generated_paths"] == ["AGENT_CATALOG.md", "ISSUE_CATALOG.md"]
    assert generated == [app.catalog]
    assert not app.catalog.root.exists()
    assert app.cli("validate") == 0
    assert called == [app.catalog]
    assert not app.port_calls


def test_email_provider_result_must_be_inside_private_root(app, tmp_path):
    path = tmp_path / "not-private.json"
    path.write_text('{"synthetic":"accepted"}')
    assert app.cli(
        "email-result", "--delivery-id", "test", "--claim-id", "test",
        "--outcome", "accepted", "--result-file", str(path),
    ) == 2


def test_staging_resume_keeps_original_selection_and_never_prepares_email(app, monkeypatch):
    app.cloud.environment = fake.replace(app.cloud.environment, profile="staging")
    monkeypatch.setattr(runner, "choose_staging", lambda catalog, store, full=False: tuple(
        runner.Selection(target, "traffic", ("full",)) for target in catalog.targets
    ))
    assert app.cli("run-staging", "--full") == 0
    count = len(app.cloud.invocations)
    monkeypatch.setattr(runner, "choose_staging", lambda *args, **kwargs: pytest.fail("Selection recomputed on resume"))
    assert app.cli("run-staging", "--full") == 0
    assert len(app.cloud.invocations) == count
    assert not app.cloud.starts
    assert not RuntimeStore("staging", root=app.store.root).outbox("email").directory.exists()


def test_unchanged_staging_does_not_even_construct_live_ports(app, monkeypatch):
    monkeypatch.setattr(runner, "choose_staging", lambda *args, **kwargs: ())
    assert app.cli("run-staging") == 0
    assert not app.port_calls


def test_partial_and_failure_email_routing_comes_from_actual_result(app, monkeypatch, capsys):
    original = app.sol.complete_json
    failing = {"issue-001"}
    async def incomplete(**kwargs):
        if kwargs["payload"]["target"]["unit_id"]["logical_version"] in failing:
            raise QualityError("synthetic_assessment_failure")
        return await original(**kwargs)
    app.sol.complete_json = incomplete
    assert app.cli("run-daily", "--test-run", "--rerun", "1") == 0
    first, _ = last_json(capsys)
    assert first["status"] == "Partial" and first["coverage"]["excluded_units"] == 1
    failing.update({"issue-002", "issue-003"})
    monkeypatch.setattr(runner, "source_revision", lambda _: "b" * 40)
    # A fresh trial without retained history exposes all three incomplete units.
    app.store.outbox("trials")._path("progress", fake.DAY.isoformat()).unlink()
    assert app.cli("run-daily", "--test-run", "--rerun", "2") == 2
    second, _ = last_json(capsys)
    assert second["status"] == "Failed" and second["score"] is None
    email = read_email(app.store.outbox("email"), second["delivery_id"])
    assert email.request.mode == "test" and "failure" in email.request.subject


def test_prepared_email_resume_skips_ports_and_retains_frozen_metadata_context_and_warnings(app, monkeypatch, capsys):
    assert app.cli("run-daily", "--test-run", "--rerun", "1") == 0
    value, _ = last_json(capsys)
    record = read_email(app.store.outbox("email"), value["delivery_id"])
    assert "Sweden Central" in record.request.html and "a" * 40 in record.request.html
    assert "2026-09-04" in record.request.html and "Synthetic reviewed defect" in record.request.html
    calls = len(app.port_calls), len(app.sol.calls), len(app.cloud.invocations)
    (app.store.root / "config" / "email-recipient.json").write_text("{broken")
    (app.store.root / "config" / "assessment.json").write_text("{broken")
    monkeypatch.setattr(runner, "source_revision", lambda _: "b" * 40)
    assert app.cli("run-daily", "--test-run", "--rerun", "1") == 0
    last_json(capsys)
    assert (len(app.port_calls), len(app.sol.calls), len(app.cloud.invocations)) == calls
    assert read_email(app.store.outbox("email"), value["delivery_id"]) == record


def test_bootstrap_failure_is_safe_logged_before_traffic_and_unexpected_error_reraises(app, monkeypatch, capsys):
    @asynccontextmanager
    async def failed(*args):
        raise QualityError("synthetic_bootstrap_unavailable")
        yield
    code = cli.main(
        ["run-daily", "--test-run", "--rerun", "1"], root=app.catalog.root,
        runtime_factory=lambda profile: app.store, ports=failed, today=fake.DAY,
    )
    assert code == 2 and "synthetic_bootstrap_unavailable" in capsys.readouterr().err
    assert not app.cloud.invocations
    statuses = [
        app.store.run(path.name).read("command-status")
        for path in (app.store.directory / "runs").glob("startup-*")
    ]
    assert any(item["code"] == "synthetic_bootstrap_unavailable" for item in statuses)
    @asynccontextmanager
    async def bug(*args):
        raise TypeError("synthetic private programmer detail")
        yield
    with pytest.raises(TypeError, match="programmer detail"):
        cli.main(["run-daily", "--test-run", "--rerun", "2"], root=app.catalog.root,
                 runtime_factory=lambda profile: app.store, ports=bug, today=fake.DAY)
    assert "programmer detail" not in capsys.readouterr().out


def test_entrypoint_redacts_programming_details_but_returns_failure(monkeypatch, capsys):
    def bug():
        raise ValueError("synthetic private exception payload")
    monkeypatch.setattr(cli, "main", bug)
    assert cli.entrypoint() == 1
    capture = capsys.readouterr()
    assert json.loads(capture.err) == {"status": "failed", "code": "unexpected_failure"}
    assert "exception payload" not in capture.out + capture.err


def test_cli_official_flushes_logger_events_during_traffic_and_keeps_work_items_private(app, monkeypatch, capsys):
    import asyncio
    from agent_insights_quality import events
    from agent_insights_quality.integration import RunIntegration
    import test_integration as integrations
    import test_publication as publishing

    integrations.config(app.store)
    client = publishing.FakeClient()
    gate = {}
    writes = []
    monkeypatch.setattr(cli, "_official_source", lambda _: None)
    monkeypatch.setattr(runner, "RunLogger", events.RunLogger)
    async def fetch(*args):
        return integrations.snapshot()
    def auxiliary(*args, **kwargs):
        loop = asyncio.get_running_loop()
        gate.update(tick=integrations.Tick(), written=asyncio.Event())
        client.before_manage = lambda _: loop.call_soon_threadsafe(gate["written"].set)
        def write(root, document, *, test_run):
            writes.append(document)
            return (
                "reports/daily/2026/09/04/report.json", "reports/daily/2026/09/04/report.md",
                "reports/latest.json", "reports/latest.md",
            )
        return RunIntegration(*args, **kwargs, fetch_context=fetch, tick=gate["tick"],
                              adx_factory=lambda *_: client, write_report=write)
    original = app.cloud.invoke
    async def invoke(*args, **kwargs):
        if not app.cloud.invocations:
            gate["tick"].request.set()
            await gate["written"].wait()
            assert not app.sol.calls and not app.cloud.starts
        return await original(*args, **kwargs)
    app.cloud.invoke = invoke
    @asynccontextmanager
    async def ports(*args):
        assert app.store.run("daily-2026-09-04").read_completed("work-item-context")
        yield app.cloud, app.sol, app.registry
    assert cli.main(
        ["run-daily"], root=app.catalog.root, runtime_factory=lambda _: app.store,
        ports=ports, integrations=auxiliary, today=fake.DAY,
    ) == 0
    value, _ = last_json(capsys)
    assert value["status"] == "Full" and value["github_request_path"]
    email = read_email(app.store.outbox("email"), value["delivery_id"])
    assert "Synthetic private quality item" in email.request.html
    assert "Synthetic private quality item" not in json.dumps(app.sol.calls)
    assert "Synthetic private quality item" not in json.dumps(writes)
    assert "Synthetic private quality item" not in json.dumps(client.rows)
