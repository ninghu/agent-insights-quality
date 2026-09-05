"""Profile-specific configured assessor intent and execution-only reuse."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path

import pytest

from agent_insights_quality import catalogs, cli, report_context, runner
from agent_insights_quality.errors import QualityError
from agent_insights_quality.performance import RunMetrics
from agent_insights_quality.settings import AssessmentSettings, load_assessment_settings
import test_runner as fake
import test_provider_sol_cooldown as sol_fake

SOL = AssessmentSettings()
ASTRA = AssessmentSettings(
    deployment_name="astra-assessment", model="gpt-6-astra", model_version="2026-09-03",
    credential="azure_cli",
)
DAY = fake.DAY


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    fake.fake_storage(monkeypatch)


def write_settings(root, name, settings):
    path = root / "config" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings.to_dict() if isinstance(settings, AssessmentSettings) else settings))
    return path


def make_cli(h, monkeypatch, *, after_ports=None):
    h.catalog = replace(h.catalog, targets=tuple(replace(target, expectation={
        "title": "Synthetic reviewed defect", "root_cause": "Synthetic input contradiction",
        "expected_fix": "Honor the synthetic reviewed input",
    }) for target in h.catalog.targets))
    monkeypatch.setattr(catalogs, "load_catalog", lambda _: h.catalog)
    monkeypatch.setattr(report_context, "load_catalog", lambda _: h.catalog)
    monkeypatch.setattr(cli, "_committed_inputs", lambda _: None)
    monkeypatch.setattr(runner, "source_revision", lambda _: "a" * 40)
    monkeypatch.setattr(runner, "git_source_changes", lambda *args: runner.SourceChanges(()))
    original = runner.Runner
    def create(*args, **kwargs):
        return original(
            *args, **kwargs, attempts=fake.attempts, now=h.clock.now,
            monotonic=h.clock.monotonic, sleep=h.clock.sleep,
            deployment_source=lambda _: "deployment-one", changes_since=lambda _: runner.SourceChanges(()),
        )
    monkeypatch.setattr(runner, "Runner", create)
    write_settings(h.store.root, "email-recipient.json", {
        "schema_version": "1.0.0", "purpose": "daily_test", "recipient": "synthetic@example.invalid",
    })
    constructed = []
    @asynccontextmanager
    async def ports(catalog, runtime, run_id, settings):
        assert runtime.run(run_id).read_completed("assessment-settings") == settings.to_dict()
        constructed.append((run_id, settings))
        if after_ports:
            after_ports(settings)
        yield h.cloud, h.sol, h.registry
    def invoke(*args):
        return cli.main(
            args, root=h.catalog.root, runtime_factory=lambda _: h.store, ports=ports, today=DAY,
            metrics_factory=lambda records: RunMetrics(records, segment_id=f"metrics-{len(constructed)}",
                                                     monotonic=h.clock.monotonic),
        )
    return invoke, constructed


def test_new_daily_override_freezes_all_fields_before_ports_and_labels_test_email(tmp_path, monkeypatch, capsys):
    h = fake.Harness(tmp_path, issues=4)
    invoke, constructed = make_cli(h, monkeypatch)
    write_settings(h.store.root, "assessment.json", SOL)
    write_settings(h.store.root, "daily-assessment.json", ASTRA)
    assert invoke("run-daily", "--test-run", "--rerun", "2") == 0
    status = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert constructed == [(status["run_id"], ASTRA)]
    records = h.store.run(status["run_id"])
    assert records.read_completed("assessment-settings") == ASTRA.to_dict()
    for target in h.catalog.targets:
        binding = records.read(f"targets/{target.key}/source")
        assert binding["configured_assessor"] == ASTRA.to_dict()
        artifact = records.read_artifact(binding["assessment"]["artifact"])
        assert artifact["configured_assessor"] == ASTRA.to_dict()
        assert artifact["private_detail"]["input"]["target"]["unit_id"] == target.unit_id.to_dict()
    delivery = records.read_completed("delivery-inputs")
    assert delivery["configured_assessor"] == ASTRA.to_dict()
    from agent_insights_quality.email import read_email
    request = read_email(h.store.outbox("email"), status["delivery_id"]).request
    assert "Configured assessment (intent, not observed serving metadata)" in request.html
    assert all(value in request.html for value in ASTRA.to_dict().values())
    assert "a" * 40 in request.html
    assert not h.store.outbox("publication").directory.exists()
    assert not (h.catalog.root / "reports").exists()
    performance = json.loads(Path(status["performance_path"]).read_text())
    assert performance["run_id"] == status["run_id"]


def test_staging_ignores_daily_override_even_if_invalid(tmp_path, monkeypatch):
    h = fake.Harness(tmp_path, profile="staging", issues=0)
    invoke, constructed = make_cli(h, monkeypatch)
    write_settings(h.store.root, "assessment.json", SOL)
    write_settings(h.store.root, "daily-assessment.json", {"unreviewed": "invalid"})
    assert invoke("run-staging") == 0
    assert constructed[0][1] == SOL
    assert h.store.run(constructed[0][0]).read_completed("assessment-settings") == SOL.to_dict()


def test_unfinished_n1_resumes_frozen_sol_despite_new_astra_default_without_new_execution(tmp_path, monkeypatch):
    h = fake.Harness(tmp_path, issues=4)
    invoke, constructed = make_cli(h, monkeypatch)
    write_settings(h.store.root, "assessment.json", SOL)
    original = h.sol.complete_json
    first = True
    async def crash(**kwargs):
        nonlocal first
        if first:
            first = False
            raise OSError("synthetic failure before first judgment")
        return await original(**kwargs)
    h.sol.complete_json = crash
    assert invoke("run-daily", "--test-run", "--rerun", "1") == 2
    n1 = constructed[0][0]
    assert h.store.run(n1).read_completed("assessment-settings") == SOL.to_dict()
    assert h.store.run(n1).read_completed("delivery-inputs", missing_ok=True) is None
    counts = len(h.cloud.invocations), len(h.cloud.starts), dict(h.cloud.resets)
    write_settings(h.store.root, "daily-assessment.json", ASTRA)
    assert invoke("run-daily", "--test-run", "--rerun", "1") == 0
    assert [settings for _, settings in constructed] == [SOL, SOL]
    assert (len(h.cloud.invocations), len(h.cloud.starts), dict(h.cloud.resets)) == counts
    for target in h.catalog.targets:
        assert h.store.run(n1).read(f"targets/{target.key}/source")["configured_assessor"] == SOL.to_dict()


def test_completed_n1_resume_does_not_read_new_defaults_or_rewrite_email(tmp_path, monkeypatch):
    from agent_insights_quality.email import claim_email, read_email, record_email_outcome
    h = fake.Harness(tmp_path, issues=4)
    invoke, constructed = make_cli(h, monkeypatch)
    assert invoke("run-daily", "--test-run", "--rerun", "1") == 0
    n1 = constructed[0][0]
    with h.store.ownership():
        claim_email(h.store.outbox("email"), n1, claim_id="synthetic-claim")
        delivered = record_email_outcome(
            h.store.outbox("email"), n1, claim_id="synthetic-claim", outcome="delivered",
            provider_result={"synthetic": "confirmed"},
        )
    counts = len(h.cloud.invocations), len(h.cloud.starts), len(h.sol.calls), len(constructed)
    write_settings(h.store.root, "daily-assessment.json", {"invalid": True})
    write_settings(h.store.root, "assessment.json", {"invalid": True})
    assert invoke("run-daily", "--test-run", "--rerun", "1") == 0
    assert (len(h.cloud.invocations), len(h.cloud.starts), len(h.sol.calls), len(constructed)) == counts
    assert read_email(h.store.outbox("email"), n1) == delivered
    assert h.store.run(n1).read_completed("assessment-settings") == SOL.to_dict()


@pytest.mark.parametrize("legacy_binding", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_next_n2_reuses_only_execution_when_configured_assessor_changes(
    tmp_path, monkeypatch, changed, legacy_binding,
):
    h = fake.Harness(tmp_path, issues=4)
    invoke, constructed = make_cli(h, monkeypatch)
    assert invoke("run-daily", "--test-run", "--rerun", "1") == 0
    n1 = constructed[0][0]
    records = h.store.run(n1)
    if legacy_binding:
        for target in h.catalog.targets:
            path = records._path("progress", f"targets/{target.key}/source")
            value = json.loads(path.read_text())
            value.pop("configured_assessor")
            path.write_text(json.dumps(value))
            artifact_path = records._path("artifacts", value["assessment"]["artifact"])
            value = json.loads(artifact_path.read_text())
            value.pop("configured_assessor")
            artifact_path.write_text(json.dumps(value))
    original = {path: path.read_bytes() for path in records.directory.rglob("*.json")}
    counts = len(h.cloud.invocations), len(h.cloud.starts), len(h.sol.calls), dict(h.cloud.resets)
    event_count = len(h.cloud.events)
    write_settings(h.store.root, "daily-assessment.json", ASTRA if changed else SOL)
    assert invoke("run-daily", "--test-run", "--rerun", "2") == 0
    n2, identity = constructed[-1]
    assert identity == (ASTRA if changed else SOL)
    assert len(h.sol.calls) == counts[2] + (5 if changed else 0)
    assert (len(h.cloud.invocations), len(h.cloud.starts), dict(h.cloud.resets)) == (counts[0], counts[1], counts[3])
    assert all(event[0] == "assessment" for event in h.cloud.events[event_count:])
    assert {path: path.read_bytes() for path in original} == original
    for target in h.catalog.targets:
        old = records.read(f"targets/{target.key}/source")
        current = h.store.run(n2).read(f"targets/{target.key}/source")
        assert current["traffic_run_id"] == old["traffic_run_id"] == n1
        assert current["work_key"] == old["work_key"]
        assert current["evidence_key"] == old["evidence_key"]
        if changed:
            assert current["assessment"]["run_id"] == n2
            assert current["configured_assessor"] == ASTRA.to_dict()
            artifact = h.store.run(n2).read_artifact(current["assessment"]["artifact"])
            assert artifact["configured_assessor"] == ASTRA.to_dict()
            old_artifact = records.read_artifact(old["assessment"]["artifact"])
            assert artifact["private_detail"]["input"] == old_artifact["private_detail"]["input"]
        else:
            assert current["assessment"] == old["assessment"]


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("model"),
    lambda value: value.update(extra="not-supported"),
    lambda value: value.update(deployment_name=None),
    lambda value: value.update(model="https://synthetic.invalid"),
    lambda value: value.update(model_version="2026-99-99"),
    lambda value: value.update(credential="managed_identity"),
])
def test_invalid_daily_override_is_strict_and_cannot_construct_ports(tmp_path, monkeypatch, mutation):
    h = fake.Harness(tmp_path, issues=4)
    invoke, constructed = make_cli(h, monkeypatch)
    value = ASTRA.to_dict()
    mutation(value)
    write_settings(h.store.root, "daily-assessment.json", value)
    assert invoke("run-daily", "--test-run", "--rerun", "2") == 2
    assert not constructed and not h.cloud.invocations


@pytest.mark.parametrize("content", ["{", '{"model":"one","model":"two"}', " " * 16385])
def test_corrupt_duplicate_or_oversized_daily_override_never_falls_back(tmp_path, monkeypatch, content):
    h = fake.Harness(tmp_path, issues=4)
    invoke, constructed = make_cli(h, monkeypatch)
    path = write_settings(h.store.root, "daily-assessment.json", ASTRA)
    path.write_text(content)
    assert invoke("run-daily", "--test-run", "--rerun", "2") == 2
    assert not constructed


def test_assessment_settings_snapshot_is_exact_and_round_trips():
    assert AssessmentSettings.from_dict(ASTRA.to_dict()) == ASTRA
    assert SOL.to_dict() == {
        "deployment_name": "sol-assessment", "model": "gpt-5.6-sol",
        "model_version": "2026-07-09", "credential": "azure_cli",
    }
    with pytest.raises(QualityError, match="assessment_settings_invalid"):
        AssessmentSettings.from_dict({"deployment_name": "astra-assessment"})


def test_changed_identity_on_same_run_is_rejected_before_calls(tmp_path):
    h = fake.Harness(tmp_path, issues=1)
    h.daily(test_run=True, rerun=1, assessment_settings=SOL)
    count = len(h.cloud.invocations), len(h.cloud.starts), len(h.sol.calls)
    with pytest.raises(QualityError, match="run_assessor_mismatch"):
        h.daily(test_run=True, rerun=1, assessment_settings=ASTRA)
    assert (len(h.cloud.invocations), len(h.cloud.starts), len(h.sol.calls)) == count


def test_mismatched_runtime_client_deployment_is_rejected(tmp_path):
    h = fake.Harness(tmp_path)
    h.sol.deployment = "other-assessment"
    with pytest.raises(QualityError, match="assessor_deployment_mismatch"):
        h.runner(assessment_settings=ASTRA)
    assert not h.cloud.events


def test_frozen_incomplete_identity_is_not_completed_from_new_default(tmp_path, monkeypatch):
    h = fake.Harness(tmp_path, issues=4)
    invoke, constructed = make_cli(h, monkeypatch)
    records = h.store.run("daily-2026-09-04-test-1")
    with h.store.ownership():
        records.save_completed("assessment-settings", {"deployment_name": "sol-assessment"})
    write_settings(h.store.root, "daily-assessment.json", ASTRA)
    assert invoke("run-daily", "--test-run", "--rerun", "1") == 2
    assert not constructed


def test_bootstrap_failure_after_capture_still_freezes_assessor_before_run_metadata(tmp_path, monkeypatch):
    h = fake.Harness(tmp_path, issues=4)
    fail = True
    def after(settings):
        if fail:
            raise OSError("synthetic bootstrap failure after assessor capture")
    invoke, constructed = make_cli(h, monkeypatch, after_ports=after)
    assert invoke("run-daily", "--test-run", "--rerun", "1") == 2
    n1 = constructed[0][0]
    assert h.store.run(n1).read_completed("run", missing_ok=True) is None
    assert h.store.run(n1).read_completed("assessment-settings") == SOL.to_dict()
    write_settings(h.store.root, "daily-assessment.json", ASTRA)
    fail = False
    assert invoke("run-daily", "--test-run", "--rerun", "1") == 0
    assert [identity for _, identity in constructed] == [SOL, SOL]


@pytest.mark.parametrize("field,value", [
    ("deployment_name", "another-reviewed-alias"),
    ("model", "gpt-6-astra"),
    ("model_version", "2026-09-03"),
])
def test_each_changed_assessment_identity_field_invalidates_only_judgments(tmp_path, field, value):
    h = fake.Harness(tmp_path, issues=1)
    h.daily(test_run=True, rerun=1, assessment_settings=SOL)
    count = len(h.sol.calls), len(h.cloud.invocations), len(h.cloud.starts), len(h.cloud.events)
    changed = replace(SOL, **{field: value})
    result = h.daily("n2", test_run=True, rerun=2, reuse_run_id="trial", assessment_settings=changed)
    assert result.score == 100
    assert len(h.sol.calls) == count[0] + 2
    assert (len(h.cloud.invocations), len(h.cloud.starts)) == count[1:3]
    assert all(event[0] == "assessment" for event in h.cloud.events[count[3]:])
    for target in h.catalog.targets:
        assert h.store.run("n2").read(f"targets/{target.key}/source")["configured_assessor"] == changed.to_dict()


def test_missing_prior_assessor_identity_fails_closed_without_relabeling_old_judgments(tmp_path):
    h = fake.Harness(tmp_path, issues=1)
    h.daily(test_run=True, rerun=1, assessment_settings=SOL)
    h.store.run("trial")._path("completed", "assessment-settings").unlink()
    counts = len(h.cloud.invocations), len(h.cloud.starts), len(h.sol.calls)
    with pytest.raises(QualityError, match="state_record_missing"):
        h.daily("n2", test_run=True, rerun=2, reuse_run_id="trial", assessment_settings=ASTRA)
    assert (len(h.cloud.invocations), len(h.cloud.starts), len(h.sol.calls)) == counts


def test_new_daily_constructor_gets_requested_alias_and_observer_without_changing_provider_body(tmp_path, monkeypatch):
    from agent_insights_quality import bootstrap, providers, registry
    from agent_insights_quality.performance import begin_metrics, metric_session
    h = fake.Harness(tmp_path)
    async def discover(profile):
        return h.cloud.environment
    monkeypatch.setattr(bootstrap, "discover_environment", discover)
    monkeypatch.setattr(bootstrap, "azure_json", lambda _: {
        "properties": {"ConnectionString": "InstrumentationKey=synthetic"},
    })
    monkeypatch.setattr(providers, "AzureRuntime", lambda *args, **kwargs: h.cloud)
    captured = {}
    def assessor(environment, *, deployment, observer):
        captured.update(deployment=deployment, observer=observer)
        return h.sol
    monkeypatch.setattr(providers, "AzureSol", assessor)
    class Blob:
        def __init__(self, *args):
            pass
        async def read(self):
            return None
        async def close(self):
            pass
    monkeypatch.setattr(registry, "AzureRegistryBlob", Blob)
    write_settings(h.store.root, "daily-assessment.json", ASTRA)
    with h.store.ownership(), metric_session(lambda records: RunMetrics(records, segment_id="new-assessor")):
        records = h.store.run("n2")
        settings = cli._assessment_for_run(h.store, records)
        metrics = begin_metrics(records)
        async def construct():
            async with cli.production_ports(h.catalog, h.store, "n2", settings):
                assert captured["deployment"] == "astra-assessment"
                assert captured["observer"].__self__ is metrics
                assert records.read_completed("assessment-settings") == ASTRA.to_dict()
        asyncio.run(construct())


def test_astra_alias_changes_only_structured_client_model_field_and_usage_is_actual():
    settings = AssessmentSettings.from_dict(ASTRA.to_dict())
    clock = sol_fake.Clock()
    response = json.loads(sol_fake.success().body)
    response.update(model="synthetic-observed-server-name", usage={"input_tokens": 7, "output_tokens": 3})
    from agent_insights_quality.providers import AzureSol, HttpResponse
    transport = sol_fake.Transport(clock, HttpResponse(200, {}, json.dumps(response).encode()))
    observed = []
    client = AzureSol(
        sol_fake.ENVIRONMENT, transport=transport, deployment=settings.deployment_name,
        sleep=clock.sleep, monotonic=lambda: clock.now, wall_clock=lambda: sol_fake.WALL_TIME,
        observer=observed.append,
    )
    assert asyncio.run(sol_fake.complete(client)) == {"ok": True}
    body = json.loads(transport.requests[0][1].body)
    assert body["model"] == "astra-assessment" and body["text"]["format"]["strict"] is True
    usage = next(item for item in observed if item["kind"] == "sol_usage")
    assert usage["input_tokens"] == 7 and usage["output_tokens"] == 3
    assert "model_version" not in usage


def test_existing_shared_partial_settings_contract_still_applies_only_to_shared_loader(tmp_path):
    path = write_settings(tmp_path, "assessment.json", {"deployment_name": "synthetic-sol"})
    assert load_assessment_settings(path).deployment_name == "synthetic-sol"
    with pytest.raises(QualityError, match="assessment_settings_invalid"):
        load_assessment_settings(path, require_complete=True)
