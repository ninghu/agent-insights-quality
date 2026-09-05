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


@pytest.mark.parametrize("full", [False, True])
def test_staging_resume_across_midnight_keeps_original_run_and_completed_traffic(app, monkeypatch, capsys, full):
    from agent_insights_quality.state import RecordStore
    app.catalog = fake.replace(app.catalog, targets=app.catalog.targets[:1])
    app.cloud.environment = fake.replace(app.cloud.environment, profile="staging")
    monkeypatch.setattr(runner, "choose_staging", lambda catalog, store, full=False: (
        runner.Selection(catalog.targets[0], "traffic", ("full",) if full else ("missing",)),
    ))
    save = RecordStore.save_progress
    broken = False
    def interrupt(records, key, value):
        nonlocal broken
        if records.directory.name == "last-tests" and not broken:
            broken = True
            raise RuntimeError("synthetic crash before last-test index")
        save(records, key, value)
    monkeypatch.setattr(RecordStore, "save_progress", interrupt)
    arguments = ("run-staging", "--full") if full else ("run-staging",)
    with pytest.raises(RuntimeError, match="last-test"):
        app.cli(*arguments)
    assert len(app.cloud.invocations) == 20 and len(app.sol.calls) == 1
    previous_run = app.port_calls[0][0]
    assert app.cli(*arguments, day=date(2026, 9, 5)) == 0
    value, _ = last_json(capsys)
    assert value["run_id"] == previous_run
    assert len(app.cloud.invocations) == 20 and len(app.sol.calls) == 1
    records = RuntimeStore("staging", root=app.store.root).run(previous_run)
    assert records.read_completed("run")["report_date"] == fake.DAY.isoformat()


def test_completed_full_staging_needs_explicit_new_run_for_fresh_traffic(app, monkeypatch, capsys):
    app.catalog = fake.replace(app.catalog, targets=app.catalog.targets[:1])
    app.cloud.environment = fake.replace(app.cloud.environment, profile="staging")
    monkeypatch.setattr(runner, "choose_staging", lambda catalog, store, full=False: (
        runner.Selection(catalog.targets[0], "traffic", ("full",)),
    ))
    assert app.cli("run-staging", "--full") == 0
    first, _ = last_json(capsys)
    calls = len(app.cloud.invocations), len(app.sol.calls), len(app.port_calls)
    assert app.cli("run-staging", "--full", day=date(2026, 9, 5)) == 0
    resumed, _ = last_json(capsys)
    assert resumed["run_id"] == first["run_id"] and resumed["report_date"] == fake.DAY.isoformat()
    assert (len(app.cloud.invocations), len(app.sol.calls), len(app.port_calls)) == calls
    assert app.cli("run-staging", "--full", "--new-run", day=date(2026, 9, 5)) == 0
    fresh, _ = last_json(capsys)
    assert fresh["run_id"] != first["run_id"] and fresh["report_date"] == "2026-09-05"
    assert len(app.cloud.invocations) == calls[0] + 20


def test_full_staging_midnight_recovery_retains_already_finished_units(app, monkeypatch, capsys):
    from agent_insights_quality.state import RecordStore
    app.catalog = fake.replace(app.catalog, targets=app.catalog.targets[:2])
    app.cloud.environment = fake.replace(app.cloud.environment, profile="staging")
    settings = app.catalog.root / "config"
    settings.mkdir(parents=True)
    (settings / "runtime.json").write_text(json.dumps({"staging_workers": 1, "hydration_seconds": 0}))
    monkeypatch.setattr(runner, "choose_staging", lambda catalog, store, full=False: tuple(
        runner.Selection(target, "traffic", ("full",)) for target in catalog.targets
    ))
    save = RecordStore.save_progress
    crashed = False
    def interrupt(records, key, value):
        nonlocal crashed
        if records.directory.name == "last-tests" and key == app.catalog.targets[1].key and not crashed:
            crashed = True
            raise RuntimeError("synthetic crash with baseline already indexed")
        save(records, key, value)
    monkeypatch.setattr(RecordStore, "save_progress", interrupt)
    with pytest.raises(RuntimeError, match="already indexed"):
        app.cli("run-staging", "--full")
    calls = len(app.cloud.invocations), len(app.sol.calls)
    assert calls == (40, 2)
    previous_run = app.port_calls[0][0]
    assert app.cli("run-staging", "--full", "--new-run") == 2
    assert "staging_unfinished_run_exists" in capsys.readouterr().err
    assert app.cli("run-staging", "--full", day=date(2026, 9, 5)) == 0
    result, _ = last_json(capsys)
    assert result["run_id"] == previous_run and result["statuses"]["PASS"] == 2
    assert (len(app.cloud.invocations), len(app.sol.calls)) == calls


def test_staging_source_changes_do_not_reuse_another_source_active_reference(app, monkeypatch, capsys):
    app.catalog = fake.replace(app.catalog, targets=app.catalog.targets[:1])
    app.cloud.environment = fake.replace(app.cloud.environment, profile="staging")
    monkeypatch.setattr(runner, "choose_staging", lambda catalog, store, full=False: (
        runner.Selection(catalog.targets[0], "traffic", ("deployment_changed",)),
    ))
    assert app.cli("run-staging") == 0
    first, _ = last_json(capsys)
    monkeypatch.setattr(runner, "source_revision", lambda _: "b" * 40)
    assert app.cli("run-staging") == 0
    second, _ = last_json(capsys)
    assert second["run_id"] != first["run_id"]
    assert len(app.cloud.invocations) == 40


def test_missing_active_staging_selection_fails_closed_before_provider_calls(app, monkeypatch, capsys):
    from agent_insights_quality.state import RecordStore
    app.cloud.environment = fake.replace(app.cloud.environment, profile="staging")
    save = RecordStore.save_completed
    def interrupt(records, key, value):
        if key == "run":
            raise RuntimeError("synthetic early crash")
        return save(records, key, value)
    monkeypatch.setattr(RecordStore, "save_completed", interrupt)
    monkeypatch.setattr(runner, "choose_staging", lambda catalog, store, full=False: (
        runner.Selection(catalog.targets[0], "traffic", ("missing",)),
    ))
    with pytest.raises(RuntimeError, match="early crash"):
        app.cli("run-staging")
    staging = RuntimeStore("staging", root=app.store.root)
    active = staging.outbox("staging").read("incremental")
    staging.run(active["run_id"])._path("completed", "selection").unlink()
    calls = len(app.port_calls)
    assert app.cli("run-staging", day=date(2026, 9, 5)) == 2
    assert "state_record_missing" in capsys.readouterr().err
    assert len(app.port_calls) == calls and not app.cloud.invocations


def test_constructor_io_failure_has_private_diagnostics_not_public_exception_details(app, capsys):
    def constructor():
        try:
            raise TimeoutError(110, "synthetic private CLI credential timeout")
        except TimeoutError as cause:
            error = OSError(5, "synthetic private constructor failure")
            error.winerror = 1234
            raise error from cause
    @asynccontextmanager
    async def ports(*args):
        constructor()
        yield
    code = cli.main(
        ["run-daily", "--test-run", "--rerun", "1"], root=app.catalog.root,
        runtime_factory=lambda _: app.store, ports=ports, today=fake.DAY,
    )
    assert code == 2
    output = capsys.readouterr()
    assert json.loads(output.err)["code"] == "command_io_failed"
    assert "synthetic private" not in output.out + output.err
    startup = next((app.store.directory / "runs").glob("startup-*"))
    status = app.store.run(startup.name).read("command-status")
    diagnostics = json.loads(Path(status["diagnostics_path"]).read_text())
    assert diagnostics["exceptions"][0]["errno"] == 5
    assert diagnostics["exceptions"][0]["winerror"] == 1234
    assert diagnostics["exceptions"][1]["exception_type"] == "builtins.TimeoutError"
    assert "synthetic private" not in json.dumps(diagnostics)
    assert app.cli("status") == 0
    status_output, _ = last_json(capsys)
    assert any(item["code"] == "command_io_failed" for item in status_output["runs"] if "code" in item)
    assert "diagnostics_path" not in json.dumps(status_output) and not app.cloud.invocations


def test_next_source_staging_selects_all_missing_after_global_failure_without_result(app, monkeypatch, capsys):
    app.catalog = fake.replace(app.catalog, targets=app.catalog.targets[:3])
    app.cloud.environment = fake.replace(app.cloud.environment, profile="staging")
    settings = app.catalog.root / "config"
    settings.mkdir(parents=True)
    (settings / "runtime.json").write_text(json.dumps({"staging_workers": 1, "hydration_seconds": 0}))
    monkeypatch.setattr(runner, "git_source_changes", lambda *args: runner.SourceChanges(("README.md",)))
    original = app.cloud.ensure_deployment
    def interrupted():
        raise OSError(5, "synthetic global credential failure")
    async def deploy(target, *args):
        if not target.is_baseline:
            interrupted()
        return await original(target, *args)
    app.cloud.ensure_deployment = deploy
    assert app.cli("run-staging", "--full") == 2
    capsys.readouterr()
    staging = RuntimeStore("staging", root=app.store.root)
    prior_active = staging.outbox("staging").read("full")
    prior_run = staging.run(prior_active["run_id"])
    original_selection = prior_run.read_completed("selection")
    assert prior_run.read("staging-result", missing_ok=True) is None
    baseline = staging.staging_index.read(app.catalog.targets[0].key)
    assert baseline["status"] == "PASS" and not prior_active["completed"]
    assert len(app.cloud.invocations) == 20
    assert all(staging.staging_index.read(target.key, missing_ok=True) is None
               for target in app.catalog.targets[1:])
    app.cloud.ensure_deployment = original
    monkeypatch.setattr(runner, "source_revision", lambda _: "b" * 40)
    assert app.cli("run-staging") == 0
    result, _ = last_json(capsys)
    assert result["run_id"] != prior_active["run_id"] and result["selected"] == 2
    selected = staging.run(result["run_id"]).read_completed("selection")["targets"]
    assert {item["key"] for item in selected} == {target.key for target in app.catalog.targets[1:]}
    assert {item["key"]: item["reasons"] for item in selected} == {
        target.key: ["incomplete"] if prior_run.read(
            f"targets/{target.key}/source", missing_ok=True,
        ) else ["missing"]
        for target in app.catalog.targets[1:]
    }
    assert len(app.cloud.invocations) == 60 and len(app.sol.calls) == 3
    assert staging.staging_index.read(app.catalog.targets[0].key) == baseline
    assert staging.outbox("staging").read("full") == prior_active
    assert prior_run.read_completed("selection") == original_selection
    assert prior_run.read("staging-result", missing_ok=True) is None


@pytest.mark.parametrize("active_mode", ["full", "incremental"])
@pytest.mark.parametrize("change", ["acr", "evaluator", "traffic"])
@pytest.mark.parametrize("reference_exists", [False, True])
def test_next_source_reconciles_unindexed_active_hosted_turns(
    app, monkeypatch, capsys, active_mode, change, reference_exists,
):
    from agent_insights_quality.contracts import Invocation
    app.catalog = fake.replace(app.catalog, targets=(
        fake.replace(app.catalog.targets[1], agent_type="hosted_code"),
    ))
    target = app.catalog.targets[0]
    app.cloud.environment = fake.replace(app.cloud.environment, profile="staging")
    staging = RuntimeStore("staging", root=app.store.root)
    source = ["a" * 40]
    monkeypatch.setattr(runner, "source_revision", lambda _: source[0])
    traffic_path = target.version_root.relative_to(app.catalog.root) / "traffic.json"
    repair_path = {
        "acr": "src/agent_insights_quality/providers/acr.py",
        "evaluator": "src/agent_insights_quality/providers/sol.py",
        "traffic": traffic_path,
    }[change]
    monkeypatch.setattr(runner, "git_source_changes", lambda root, previous, *args: runner.SourceChanges(
        (traffic_path,) if previous == "a" * 40 else (repair_path,),
    ))

    def rejected(deployment, step, request_id, session, persist):
        return Invocation(
            request_id, "rejected-" + request_id, session,
            app.clock.now().isoformat(), app.clock.now().isoformat(),
            "failed", {"error": {"code": "synthetic_old_bad_request"}}, 400,
        )
    app.cloud.invoke_hook = rejected
    assert app.cli("run-staging", "--full") == 2
    capsys.readouterr()
    old_index = staging.staging_index.read(target.key)
    assert old_index["traffic_source_revision"] == "a" * 40
    assert len(app.cloud.invocations) == 20

    source[0] = "b" * 40
    partial_requests = []
    def interrupted(deployment, step, request_id, session, persist):
        partial_requests.append(request_id)
        if len(partial_requests) == 6:
            # Cloud.invoke already persisted its submitting checkpoint.
            raise OSError(32, "synthetic global cleanup interruption")
    app.cloud.invoke_hook = interrupted
    command = ("run-staging", "--full") if active_mode == "full" else ("run-staging",)
    assert app.cli(*command) == 2
    capsys.readouterr()
    prior_active = staging.outbox("staging").read(active_mode)
    prior_run = staging.run(prior_active["run_id"])
    prior_binding = prior_run.read(f"targets/{target.key}/source")
    assert prior_binding["traffic_source_revision"] == "b" * 40
    assert prior_run.read("staging-result", missing_ok=True) is None
    assert staging.staging_index.read(target.key) == old_index
    assert len(partial_requests) == 6
    if not reference_exists:
        # Reproduce the already-written current-format run without the new reference.
        staging.outbox("staging")._path("progress", f"targets/{target.key}").unlink(missing_ok=True)
        prior_binding.pop("binding_run_id", None)
        with staging.ownership():
            prior_run.save_progress(f"targets/{target.key}/source", prior_binding)

    source[0] = "c" * 40
    app.cloud.invoke_hook = None
    assert app.cli("run-staging") == 0
    current, _ = last_json(capsys)
    binding = staging.run(current["run_id"]).read(f"targets/{target.key}/source")
    if change == "traffic":
        assert binding["traffic_source_revision"] == source[0]
        assert binding["work_key"] != prior_binding["work_key"]
        assert len(app.cloud.invocations) == 46
    else:
        assert binding["work_key"] == prior_binding["work_key"]
        assert binding["traffic_run_id"] == prior_active["run_id"]
        assert binding["traffic_source_revision"] == "b" * 40
        assert len(app.cloud.invocations) == 40
        turn = prior_run.read(prior_binding["work_key"] + "/traffic/attempt-03/probe")
        assert turn["request_id"] == partial_requests[-1] and turn["status"] == "unknown"
        for index in range(1, 3):
            for phase in ("setup", "probe"):
                receipt = prior_run.read(prior_binding["work_key"] + f"/traffic/attempt-{index:02d}/{phase}")
                assert receipt["status"] == "completed" and receipt["request_id"] in partial_requests
        assert prior_run.read_completed("environment") == staging.run(current["run_id"]).read_completed("environment")
    assert all(sum(call[2] == request for call in app.cloud.invocations) == 1 for request in partial_requests)
    assert prior_run.read("staging-result", missing_ok=True) is None


def test_private_daily_metrics_are_segmented_paths_only_and_never_use_adx(app, capsys):
    from agent_insights_quality.integration import RunIntegration
    from agent_insights_quality.performance import RunMetrics
    adx = app.store.root / "config" / "adx.json"
    adx.write_text(json.dumps({"schema_version": "1.0", "cluster_uri": "https://synthetic.invalid", "database": "synthetic"}))
    def forbidden(*args, **kwargs):
        pytest.fail("Private metrics accessed ADX or public publication")
    def integrations(*args, **kwargs):
        return RunIntegration(*args, **kwargs, adx_factory=forbidden,
                              outbox_factory=forbidden, write_report=forbidden)
    @asynccontextmanager
    async def ports(*args):
        yield app.cloud, app.sol, app.registry
    segments = []
    def metrics(records):
        value = RunMetrics(records, monotonic=app.clock.monotonic, segment_id=f"segment-{len(segments)}")
        segments.append(value)
        return value
    def invoke():
        return cli.main(
            ["run-daily", "--test-run", "--rerun", "1"], root=app.catalog.root,
            runtime_factory=lambda _: app.store, ports=ports, integrations=integrations,
            metrics_factory=metrics, today=fake.DAY,
        )
    assert invoke() == 0
    first, _ = last_json(capsys)
    report = json.loads(Path(first["performance_path"]).read_text())
    assert "observations" in report and "observations" not in first
    assert report["sol_usage"]["input_tokens"] is report["sol_usage"]["output_tokens"] is None
    assert report["totals"]["model_call:sol"]["count"] == len(app.sol.calls)
    original = Path(first["performance_path"]).read_bytes()
    count = len(app.cloud.invocations), len(app.sol.calls), len(app.cloud.starts)
    assert invoke() == 0
    resumed, _ = last_json(capsys)
    assert resumed["performance_path"] != first["performance_path"]
    assert Path(first["performance_path"]).read_bytes() == original
    assert (len(app.cloud.invocations), len(app.sol.calls), len(app.cloud.starts)) == count
    second = json.loads(Path(resumed["performance_path"]).read_text())
    reused = next(item for item in second["observations"] if item["kind"] == "run")
    assert reused["status"] == "reused" and reused["elapsed_seconds"] is None
    assert not app.store.outbox("publication").directory.exists()
    assert not app.store.outbox("events").directory.exists()
    assert not (app.catalog.root / "reports").exists()
    assert app.cli("status") == 0
    status, _ = last_json(capsys)
    assert status["runs"][0]["performance_path"] == resumed["performance_path"]
    assert "observations" not in json.dumps(status)


def test_unwritable_private_metrics_do_not_block_valid_inline_email(app, monkeypatch, capsys):
    from agent_insights_quality.state import CheckpointError, RecordStore
    save = RecordStore.save_progress
    def fail(records, key, value):
        if key.startswith("performance/"):
            raise CheckpointError()
        return save(records, key, value)
    monkeypatch.setattr(RecordStore, "save_progress", fail)
    assert app.cli("run-daily", "--test-run", "--rerun", "1") == 0
    result, error = last_json(capsys)
    assert result["status"] == "Full"
    assert result["email_status"] == "prepared"
    assert "performance_path" not in result
    assert "logging_failed" in result["warnings"]
    assert "performance_persistence_failed" in error
    email = read_email(app.store.outbox("email"), result["delivery_id"])
    assert "Operational logging reported a failure" in email.request.html


def test_production_sol_receives_optional_metrics_observer_without_wire_changes(tmp_path, monkeypatch):
    import asyncio
    from agent_insights_quality import bootstrap, providers, registry
    from agent_insights_quality.performance import RunMetrics, begin_metrics, metric_session
    from agent_insights_quality.settings import AssessmentSettings
    h = fake.Harness(tmp_path)
    fake.fake_storage(monkeypatch)
    captured = {}
    async def discover(profile):
        return h.cloud.environment
    monkeypatch.setattr(bootstrap, "discover_environment", discover)
    monkeypatch.setattr(bootstrap, "azure_json", lambda _: {
        "properties": {"ConnectionString": "InstrumentationKey=synthetic"},
    })
    monkeypatch.setattr(providers, "AzureRuntime", lambda *args, **kwargs: h.cloud)
    def sol(environment, *, deployment, observer):
        captured.update(deployment=deployment, observer=observer)
        return h.sol
    monkeypatch.setattr(providers, "AzureSol", sol)
    class Blob:
        def __init__(self, *args):
            pass
        async def read(self):
            return None
        async def close(self):
            pass
    monkeypatch.setattr(registry, "AzureRegistryBlob", Blob)
    with h.store.ownership(), metric_session(
        lambda records: RunMetrics(records, segment_id="synthetic"),
    ):
        metrics = begin_metrics(h.store.run("factory"))
        async def create():
            async with cli.production_ports(h.catalog, h.store, "factory", AssessmentSettings()):
                assert captured["observer"].__self__ is metrics
                assert captured["deployment"] == "sol-assessment"
        asyncio.run(create())
