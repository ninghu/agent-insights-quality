import asyncio
from copy import deepcopy
from datetime import date
import json
from pathlib import Path

import pytest

from agent_insights_quality.contracts import Environment
from agent_insights_quality.email import claim_email, record_email_outcome
from agent_insights_quality.errors import QualityError
from agent_insights_quality.integration import RunIntegration, command_status
from agent_insights_quality.publication import PublicationOutbox
from agent_insights_quality.state import CheckpointError, RuntimeStore
import test_publication as publishing
import test_runner as fake

DAY = date(2026, 9, 4)
RUN = "daily-2026-09-04"
SOURCE = "a" * 40
ENVIRONMENT = Environment(
    "daily", "synthetic", "synthetic", "https://synthetic.invalid",
    "/synthetic/telemetry", "syntheticstore", "syntheticregistry",
    "swedencentral", "Sweden Central",
)


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    fake.fake_storage(monkeypatch)


def config(runtime, *, adx=True, context=True):
    directory = runtime.root / "config"
    directory.mkdir(parents=True, exist_ok=True)
    if adx:
        (directory / "adx.json").write_text(json.dumps({
            "schema_version": "1.0", "cluster_uri": "https://synthetic.invalid", "database": "synthetic",
        }))
    if context:
        (directory / "quality-work-items-query-url.txt").write_text("https://synthetic.invalid/query\n")


def snapshot():
    return {
        "report_date": DAY.isoformat(), "closed_on": "2026-09-03",
        "active": [{
            "id": 1, "title": "Synthetic private quality item", "state": "Active",
            "owner": "Synthetic owner", "url": "https://synthetic.invalid/item/1",
        }], "closed": [],
    }


class Tick:
    def __init__(self):
        self.request = asyncio.Event()

    async def __call__(self, stop):
        wake, done = asyncio.create_task(self.request.wait()), asyncio.create_task(stop.wait())
        try:
            await asyncio.wait((wake, done), return_when=asyncio.FIRST_COMPLETED)
            self.request.clear()
            return stop.is_set()
        finally:
            wake.cancel()
            done.cancel()
            await asyncio.gather(wake, done, return_exceptions=True)


def integration(tmp_path, runtime, *, test_run=False, **kwargs):
    return RunIntegration(
        tmp_path / "repo", runtime, RUN, allowed_units=publishing.quality()[1],
        report_date=DAY, test_run=test_run, **kwargs,
    )


def reviewed_catalog(tmp_path, monkeypatch):
    from agent_insights_quality import report_context
    from agent_insights_quality.catalogs import Catalog
    from agent_insights_quality.contracts import Target
    root = tmp_path / "repo"
    _, plan = publishing.quality()
    targets = tuple(Target(
        unit.unit_id, "prompt", "model_mediated" if unit.is_issue else "baseline",
        root / "agents" / unit.unit_id.agent / unit.unit_id.logical_version,
        root / "agents" / unit.unit_id.agent / "v0",
        {"title": "Synthetic reviewed issue", "root_cause": "Contradicts the synthetic input",
         "expected_fix": "Honor synthetic input"},
    ) for unit in plan)
    catalog = Catalog(root, ("synthetic-agent",), targets, ({}, {}))
    monkeypatch.setattr(report_context, "load_catalog", lambda _: catalog)
    return catalog


@pytest.mark.parametrize("profile", ["daily", "staging"])
def test_periodic_run_events_flush_while_lane_is_still_working(tmp_path, profile):
    runtime = RuntimeStore(profile, root=tmp_path / "private")
    config(runtime)
    client = publishing.FakeClient()
    fetches = []
    async def fetch(url, day):
        fetches.append((url, day))
        return snapshot()
    async def run():
        tick = Tick()
        written = asyncio.Event()
        loop = asyncio.get_running_loop()
        client.before_manage = lambda _: loop.call_soon_threadsafe(written.set)
        async with integration(tmp_path, runtime, fetch_context=fetch,
                               adx_factory=lambda *_: client, tick=tick) as adapters:
            assert fetches == ([("https://synthetic.invalid/query", DAY)] if profile == "daily" else [])
            adapters.queue_event(publishing.event(1))
            tick.request.set()
            await written.wait()
            assert client.commands, "Periodic flush must not wait for a final report"
            assert not adapters.stop.is_set()
            adapters.queue_event(publishing.event(2))
        assert client.closed
    with runtime.ownership():
        asyncio.run(run())
        assert len(client.rows) == 2
        assert all("Payload" not in row for row in client.rows)


def test_test_mode_never_constructs_or_reads_any_publication_outbox_even_backlog(tmp_path, monkeypatch):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    config(runtime)
    def forbidden(*args, **kwargs):
        pytest.fail("Explicit test accessed publication")
    with runtime.ownership():
        backlog = PublicationOutbox(runtime.outbox("publication"), framework_run_id="prior-official",
                                    profile="daily", allowed_units=publishing.quality()[1])
        backlog.queue_event(publishing.event())
        existing = {path: path.read_bytes() for path in backlog.records.directory.rglob("*.json")}
        original = runtime.outbox
        def outbox(name):
            if name == "publication":
                forbidden()
            return original(name)
        monkeypatch.setattr(runtime, "outbox", outbox)
        async def run():
            async with integration(
                tmp_path, runtime, test_run=True, adx_factory=forbidden, outbox_factory=forbidden,
                write_report=forbidden, fetch_context=lambda *args: asyncio.sleep(0, result=snapshot()),
            ) as adapters:
                adapters.queue_event(publishing.event())
                assert adapters.publish_report(publishing.quality()[0], ENVIRONMENT, SOURCE) == {}
                await adapters.finish_publication()
                assert adapters.client is None and adapters.outbox is None
        asyncio.run(run())
        assert existing == {path: path.read_bytes() for path in existing}
    assert not (tmp_path / "repo").exists()


@pytest.mark.parametrize("fails", [False, True])
def test_work_items_are_fetched_once_frozen_before_use_and_resume_does_not_refetch(tmp_path, fails):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    config(runtime, adx=False)
    calls = []
    async def fetch(url, day):
        calls.append(url)
        if fails:
            raise QualityError("work_item_unavailable")
        return snapshot()
    async def run():
        async with integration(tmp_path, runtime, test_run=True, fetch_context=fetch) as adapters:
            saved = runtime.run(RUN).read_completed("work-item-context")
            assert saved["text"] == adapters.context
            if fails:
                assert adapters.warnings == {"work_item_unavailable"}
            else:
                assert "Synthetic private quality item" in adapters.context
                assert saved["snapshot"] == snapshot()
    with runtime.ownership():
        asyncio.run(run())
        (runtime.root / "config" / "quality-work-items-query-url.txt").write_text("changed config")
        asyncio.run(run())
    assert len(calls) == 1


def test_interrupted_work_item_fetch_is_disclosed_not_replaced_on_resume(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    with runtime.ownership():
        runtime.run(RUN).save_progress("work-item-context", {"status": "fetching"})
        async def fetch(*args):
            pytest.fail("Interrupted snapshot was fetched again")
        async def run():
            async with integration(tmp_path, runtime, test_run=True, fetch_context=fetch) as adapters:
                assert adapters.context is None
                assert adapters.warnings == {"work_item_unavailable"}
        asyncio.run(run())
        assert runtime.run(RUN).read_completed("work-item-context")["code"] == "work_item_context_interrupted"


def test_missing_adx_config_is_warning_no_resource_discovery_but_events_remain_durable(tmp_path):
    runtime = RuntimeStore("staging", root=tmp_path / "private")
    async def run():
        async with integration(tmp_path, runtime, adx_factory=lambda *args: pytest.fail("Guessed ADX")) as adapters:
            adapters.queue_event(publishing.event())
            assert adapters.warnings == {"adx_delivery_failed"}
    with runtime.ownership():
        asyncio.run(run())
    assert list(runtime.outbox("publication").directory.rglob("evt-*.json"))


def test_invalid_private_adx_value_is_warning_before_client_construction(tmp_path):
    runtime = RuntimeStore("staging", root=tmp_path / "private")
    config(runtime, context=False)
    (runtime.root / "config" / "adx.json").write_text(json.dumps({
        "schema_version": "1.0", "cluster_uri": ["synthetic wrong type"], "database": "synthetic",
    }))
    async def run():
        async with integration(tmp_path, runtime, adx_factory=lambda *args: pytest.fail("Invalid config used")) as adapters:
            assert adapters.warnings == {"adx_delivery_failed"}
    with runtime.ownership():
        asyncio.run(run())


def test_adx_failure_is_warning_and_checkpoint_failure_disables_only_sink(tmp_path):
    runtime = RuntimeStore("staging", root=tmp_path / "private")
    config(runtime, context=False)
    client = publishing.FakeClient()
    client.fail_queries = 10
    async def run():
        async with integration(tmp_path, runtime, adx_factory=lambda *_: client) as adapters:
            adapters.queue_event(publishing.event())
            await adapters.finish_publication()
            assert adapters.warnings == {"adx_delivery_failed"}
            assert not client.commands
        class Broken:
            def __init__(self, *args, **kwargs):
                pass
            def queue_event(self, event):
                raise CheckpointError()
            async def flush_async(self, *args, **kwargs):
                pytest.fail("Failed checkpoint must disable optional side effects")
        async with integration(tmp_path, runtime, adx_factory=lambda *_: publishing.FakeClient(),
                               outbox_factory=Broken) as adapters:
            adapters.queue_event(publishing.event())
            assert adapters.disabled
            runtime.run(RUN).save_progress("qualification-can-continue", {"status": "safe"})
    with runtime.ownership():
        asyncio.run(run())


def test_unexpected_publisher_bug_is_not_swallowed_as_warning(tmp_path):
    runtime = RuntimeStore("staging", root=tmp_path / "private")
    config(runtime, context=False)
    class Bug:
        def __init__(self, *args, **kwargs):
            pass
        async def flush_async(self, *args, **kwargs):
            raise TypeError("synthetic programming error")
    async def run():
        async with integration(tmp_path, runtime, adx_factory=lambda *_: publishing.FakeClient(), outbox_factory=Bug):
            pass
    with runtime.ownership(), pytest.raises(TypeError, match="programming error"):
        asyncio.run(run())


def test_only_eligible_official_reports_queue_generated_only_requests(tmp_path, monkeypatch):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    config(runtime, context=False)
    client = publishing.FakeClient()
    writes = []
    def write(root, document, *, test_run):
        assert not test_run
        writes.append(deepcopy(document))
        return ("reports/daily/2026/09/04/report.json", "reports/daily/2026/09/04/report.md",
                "reports/latest.json", "reports/latest.md")
    async def run():
        async with integration(tmp_path, runtime, adx_factory=lambda *_: client, write_report=write) as adapters:
            result, _ = publishing.quality()
            output = adapters.publish_report(result, ENVIRONMENT, SOURCE)
            assert output["generated_paths"] == [
                "reports/daily/2026/09/04/report.json", "reports/daily/2026/09/04/report.md",
                "reports/latest.json", "reports/latest.md",
            ]
            request = json.loads(Path(output["github_request_path"]).read_text())
            assert request["allowed_paths"] == output["generated_paths"]
            assert request["repository"] == "ninghu/agent-insights-quality"
            assert request["base_branch"] == "main" and request["operation"] == "publish-generated-report"
            assert not any("private" in json.dumps(value) for value in request.values())
            assert writes[0]["region"] == "Sweden Central"
            assert writes[0]["report"] == result.to_dict()
            assert adapters.publish_report(publishing.quality(excluded=3)[0], ENVIRONMENT, SOURCE) == {}
        assert client.rows[0]["Region"] == "Sweden Central"
    with runtime.ownership():
        asyncio.run(run())


def test_public_write_failure_does_not_prevent_final_adx_report_queue(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    config(runtime, context=False)
    client = publishing.FakeClient()
    def fail(*args, **kwargs):
        raise OSError("synthetic private failure text")
    async def run():
        async with integration(tmp_path, runtime, adx_factory=lambda *_: client, write_report=fail) as adapters:
            output = adapters.publish_report(publishing.quality()[0], ENVIRONMENT, SOURCE)
            assert "github_request_path" not in output
            assert "github_publication_failed" in adapters.warnings
        assert len(client.commands) == 1
    with runtime.ownership():
        asyncio.run(run())


def test_bootstrap_failure_is_durable_safe_and_programming_error_propagates(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    with runtime.ownership(), pytest.raises(TypeError, match="private synthetic message"):
        with command_status(runtime, "run-daily"):
            raise TypeError("private synthetic message")
    paths = list((runtime.directory / "runs").glob("startup-*"))
    status = runtime.run(paths[0].name).read("command-status")
    assert status == {"command": "run-daily", "status": "blocked", "code": "unexpected_failure"}
    assert "private synthetic message" not in (paths[0] / "runner.log").read_text()
    assert "unexpected_failure" in (paths[0] / "events.jsonl").read_text()


def test_real_official_artifacts_and_email_use_one_result_and_private_context_never_leaks(tmp_path, monkeypatch):
    from agent_insights_quality import public_artifacts
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    config(runtime)
    reviewed_catalog(tmp_path, monkeypatch)
    result, plan = publishing.quality()
    monkeypatch.setattr(public_artifacts, "_plan", lambda *_: plan)
    client = publishing.FakeClient()
    async def fetch(*args):
        return snapshot()
    async def run():
        async with integration(tmp_path, runtime, adx_factory=lambda *_: client, fetch_context=fetch) as adapters:
            output = adapters.publish_report(result, ENVIRONMENT, SOURCE)
            await adapters.finish_publication()
            assert "github_request_path" in output
            email = adapters.prepare_delivery(
                result, ENVIRONMENT, SOURCE, rerun=0, recipient=lambda: "synthetic@example.invalid",
            )
            assert email.request.mode == "official"
            assert "Synthetic private quality item" in email.request.html
            assert "Synthetic reviewed issue" in email.request.html
            assert "Sweden Central" in email.request.html and SOURCE in email.request.html
            assert DAY.isoformat() in email.request.html
            for relative in output["generated_paths"]:
                document = (tmp_path / "repo" / relative).read_text()
                assert "Synthetic private quality item" not in document
                if relative.endswith(".json"):
                    assert json.loads(document)["report"] == result.to_dict()
            public = json.loads((tmp_path / "repo" / "reports" / "latest.json").read_text())
            assert public["region"] == "Sweden Central"
            assert client.rows[0]["Payload"] == result.to_dict()
            assert "Synthetic private quality item" not in json.dumps(client.rows)
    with runtime.ownership():
        asyncio.run(run())


def test_warning_changes_do_not_overwrite_delivered_email_or_refetch_context(tmp_path, monkeypatch):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    config(runtime, adx=False)
    reviewed_catalog(tmp_path, monkeypatch)
    result, _ = publishing.quality()
    calls = []
    async def fetch(*args):
        calls.append(True)
        raise QualityError("work_item_unavailable")
    async def first():
        async with integration(tmp_path, runtime, test_run=True, fetch_context=fetch) as adapters:
            adapters.warn("github_publication_failed")
            return adapters.prepare_delivery(
                result, ENVIRONMENT, SOURCE, rerun=1, recipient=lambda: "synthetic@example.invalid",
            )
    with runtime.ownership():
        prepared = asyncio.run(first())
        claim_email(runtime.outbox("email"), RUN, claim_id="synthetic-claim")
        delivered = record_email_outcome(
            runtime.outbox("email"), RUN, claim_id="synthetic-claim", outcome="delivered",
            provider_result={"synthetic_delivery": "confirmed"},
        )
        async def second():
            async with integration(tmp_path, runtime, test_run=True,
                                   fetch_context=lambda *args: pytest.fail("Refetched context")) as adapters:
                return adapters.prepare_delivery(
                    publishing.quality(excluded=3)[0], ENVIRONMENT, "b" * 40, rerun=1,
                    recipient=lambda: pytest.fail("Reloaded recipient"),
                )
        assert asyncio.run(second()) == delivered
        assert delivered.request == prepared.request
        assert runtime.run(RUN).read_completed("delivery-inputs")["warnings"] == [
            "github_publication_failed", "work_item_unavailable",
        ]
    assert len(calls) == 1


def test_frozen_input_crash_before_email_reuses_snapshot_and_rejects_reviewed_drift(tmp_path, monkeypatch):
    from agent_insights_quality import integration as module, report_context
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    reviewed = reviewed_catalog(tmp_path, monkeypatch)
    original = module.prepare_email
    def crash(*args, **kwargs):
        raise OSError("synthetic crash before outbox save")
    monkeypatch.setattr(module, "prepare_email", crash)
    async def run():
        async with integration(tmp_path, runtime, test_run=True) as adapters:
            return adapters.prepare_delivery(
                publishing.quality()[0], ENVIRONMENT, SOURCE, rerun=1,
                recipient=lambda: "synthetic@example.invalid",
            )
    with runtime.ownership():
        with pytest.raises(OSError, match="synthetic crash"):
            asyncio.run(run())
        frozen = runtime.run(RUN).read_completed("delivery-inputs")
        monkeypatch.setattr(module, "prepare_email", original)
        modified = fake.replace(reviewed, targets=tuple(
            fake.replace(target, expectation={**target.expectation, "root_cause": "Changed reviewed symptom"})
            for target in reviewed.targets
        ))
        monkeypatch.setattr(report_context, "load_catalog", lambda _: modified)
        with pytest.raises(QualityError, match="delivery_reviewed_context_changed"):
            asyncio.run(run())
        monkeypatch.setattr(report_context, "load_catalog", lambda _: reviewed)
        email = asyncio.run(run())
        assert email.request.recipient == frozen["recipient"]
        assert email.request.report_date == frozen["report_date"]


def test_ambiguous_adx_delivery_is_only_reconciled_not_repeated_on_resume(tmp_path):
    from agent_insights_quality.publication import AdxUnavailable
    runtime = RuntimeStore("staging", root=tmp_path / "private")
    config(runtime, context=False)
    first_client = publishing.FakeClient()
    first_client.accept_count = 0
    first_client.manage_error = AdxUnavailable()
    async def run(client):
        async with integration(tmp_path, runtime, adx_factory=lambda *_: client) as adapters:
            adapters.queue_event(publishing.event())
    with runtime.ownership():
        asyncio.run(run(first_client))
        assert len(first_client.commands) == 1
        second_client = publishing.FakeClient()
        asyncio.run(run(second_client))
        assert second_client.queries and not second_client.commands
