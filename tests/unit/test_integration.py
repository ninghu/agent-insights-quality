import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from pathlib import Path

import pytest

from agent_insights_quality.contracts import Environment
from agent_insights_quality.email import EmailRecord, EmailRequest, claim_email, record_email_outcome
from agent_insights_quality.errors import QualityError
from agent_insights_quality.integration import (
    RunIntegration, _exception_diagnostics, _previous_official_snapshot, command_status,
)
from agent_insights_quality.publication import PublicationOutbox
from agent_insights_quality.state import CheckpointError, RuntimeStore
from agent_insights_quality.work_items import render_work_item_html, unavailable_context
import test_publication as publishing
import test_runner as fake

DAY = date(2026, 9, 4)
RUN = "daily-2026-09-04"
SOURCE = "a" * 40
QUERY = "https://dev.azure.com/synthetic/project/_queries/query/11111111-1111-1111-1111-111111111111"
CUTOFF = "2026-09-04T17:15:00+00:00"
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
        (directory / "quality-work-items-query-url.txt").write_text(QUERY + "\n")


def snapshot(*, cutoff=CUTOFF, report_date=DAY, previous=None):
    return {
        "report_date": report_date.isoformat(), "query_url": QUERY,
        "window": {
            "start": previous["cutoff"] if previous else (datetime.fromisoformat(cutoff) - timedelta(days=7)).isoformat(),
            "end": cutoff, "timezone": "America/Los_Angeles",
            "basis": "previous_official_report" if previous else "initial_lookback",
            "previous_delivery_id": previous["delivery_id"] if previous else None,
        },
        "active": [{
            "id": 1, "type": "Bug", "title": "Synthetic private quality item", "state": "Active",
            "owner": "Synthetic owner", "url": "https://dev.azure.com/synthetic/project/_workitems/edit/1",
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
        tmp_path / "repo", runtime, kwargs.pop("run_id", RUN), allowed_units=publishing.quality()[1],
        report_date=kwargs.pop("report_date", DAY), test_run=test_run, **kwargs,
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
    catalog = Catalog(root, ("synthetic-agent",), targets, (
        {"agents": [{"name": "synthetic-agent", "owner": "Synthetic owner"}]}, {},
    ))
    monkeypatch.setattr(report_context, "load_catalog", lambda _: catalog)
    return catalog


@pytest.mark.parametrize("profile", ["daily", "staging"])
def test_periodic_run_events_flush_while_lane_is_still_working(tmp_path, profile):
    runtime = RuntimeStore(profile, root=tmp_path / "private")
    config(runtime)
    client = publishing.FakeClient()
    fetches = []
    async def fetch(url, day, *, previous_snapshot):
        fetches.append((url, day))
        assert previous_snapshot is None
        return snapshot()
    async def run():
        tick = Tick()
        written = asyncio.Event()
        loop = asyncio.get_running_loop()
        client.before_manage = lambda _: loop.call_soon_threadsafe(written.set)
        async with integration(tmp_path, runtime, fetch_context=fetch,
                               adx_factory=lambda *_: client, tick=tick) as adapters:
            assert fetches == ([(QUERY, DAY)] if profile == "daily" else [])
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
                write_report=forbidden, fetch_context=lambda *args, **kwargs: asyncio.sleep(0, result=snapshot()),
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
    async def fetch(url, day, *, previous_snapshot):
        calls.append(url)
        assert previous_snapshot is None
        if fails:
            raise QualityError("work_item_unavailable")
        return snapshot()
    async def run():
        async with integration(tmp_path, runtime, test_run=True, fetch_context=fetch) as adapters:
            saved = runtime.run(RUN).read_completed("work-item-context")
            assert saved == adapters.work_item_context
            assert adapters.context is None
            if fails:
                assert adapters.warnings == {"work_item_unavailable"}
            else:
                assert "Synthetic private quality item" in render_work_item_html(adapters.work_item_context)
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
        async def fetch(*args, **kwargs):
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
    diagnostics = Path(status.pop("diagnostics_path"))
    assert status == {"command": "run-daily", "status": "blocked", "code": "unexpected_failure"}
    assert diagnostics.is_file() and diagnostics.is_relative_to(runtime.directory)
    assert "private synthetic message" not in diagnostics.read_text()
    assert "private synthetic message" not in (paths[0] / "runner.log").read_text()
    assert "unexpected_failure" in (paths[0] / "events.jsonl").read_text()


def test_private_diagnostics_capture_errno_windows_code_and_cause_frames_without_payload(tmp_path, capsys):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    error = OSError(5, "synthetic private outer message", "synthetic-private-file")
    error.winerror = 1234
    def credential_boundary():
        try:
            raise TimeoutError(110, "synthetic credential timeout with private payload")
        except TimeoutError as cause:
            raise error from cause
    with runtime.ownership(), pytest.raises(OSError) as caught:
        with command_status(runtime, "run-staging"):
            credential_boundary()
    assert caught.value is error
    run_path = next((runtime.directory / "runs").glob("startup-*"))
    status = runtime.run(run_path.name).read("command-status")
    diagnostic_text = Path(status["diagnostics_path"]).read_text()
    diagnostic = json.loads(diagnostic_text)
    outer, cause = diagnostic["exceptions"]
    assert outer["exception_type"] == "builtins.OSError" and outer["relation"] == "raised"
    assert (outer["errno"], outer["winerror"]) == (5, 1234)
    assert cause["exception_type"] == "builtins.TimeoutError" and cause["relation"] == "cause"
    assert cause["errno"] == 110 and cause["winerror"] is None
    assert any(frame["function"] == "credential_boundary" for frame in cause["frames"])
    assert all(set(frame) == {"filename", "lineno", "function"}
               for item in diagnostic["exceptions"] for frame in item["frames"])
    assert not diagnostic["chain_truncated"]
    output = capsys.readouterr()
    logs = (run_path / "runner.log").read_text() + (run_path / "events.jsonl").read_text()
    assert "command_io_failed" in logs
    for private in ("synthetic private outer", "synthetic credential timeout", "synthetic-private-file"):
        assert private not in diagnostic_text + logs + output.out + output.err
    assert "credential_boundary" not in logs + output.out + output.err
    assert "diagnostics_path" not in logs + output.out + output.err


def test_private_diagnostics_bound_frames_chains_and_do_not_call_exception_formatters():
    class UnprintableError(RuntimeError):
        def __str__(self):
            pytest.fail("Exception messages must never be formatted")
    def recurse(depth):
        if depth:
            return recurse(depth - 1)
        raise UnprintableError("synthetic private payload")
    try:
        recurse(50)
    except UnprintableError as original:
        top = original
        for _ in range(12):
            wrapped = RuntimeError("synthetic hidden cause")
            wrapped.__cause__ = top
            top = wrapped
        bounded = _exception_diagnostics(top)
        assert len(bounded["exceptions"]) == 8 and bounded["chain_truncated"]
        detail = _exception_diagnostics(original)
        assert len(detail["exceptions"][0]["frames"]) == 32
        assert detail["exceptions"][0]["frames_truncated"]
        assert detail["exceptions"][0]["frames"][-1]["function"] == "recurse"
        assert "synthetic private payload" not in json.dumps(detail)
        original.__cause__ = original
        assert _exception_diagnostics(original)["chain_truncated"]


def test_private_diagnostics_distinguish_context_suppression_without_formatting_it():
    try:
        raise TimeoutError("synthetic hidden")
    except TimeoutError:
        try:
            raise ValueError("synthetic outer")
        except ValueError as error:
            diagnostic = _exception_diagnostics(error)
            assert [item["relation"] for item in diagnostic["exceptions"]] == ["raised", "context"]
            error.__suppress_context__ = True
            diagnostic = _exception_diagnostics(error)
            assert [item["relation"] for item in diagnostic["exceptions"]] == ["raised", "suppressed_context"]
            assert "synthetic hidden" not in json.dumps(diagnostic)


def test_diagnostic_checkpoint_failure_propagates_without_authorizing_more_work(tmp_path, monkeypatch):
    from agent_insights_quality.state import RecordStore
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    def fail(*args, **kwargs):
        raise CheckpointError()
    monkeypatch.setattr(RecordStore, "save_artifact", fail)
    continued = False
    with runtime.ownership(), pytest.raises(CheckpointError):
        with command_status(runtime, "run-staging"):
            raise OSError(5, "synthetic failure")
        continued = True
    assert not continued
    path = next((runtime.directory / "runs").glob("startup-*"))
    assert "state_checkpoint_failed" in (path / "runner.log").read_text()


def test_real_official_artifacts_and_email_use_one_result_and_private_context_never_leaks(tmp_path, monkeypatch):
    from agent_insights_quality import public_artifacts
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    config(runtime)
    reviewed_catalog(tmp_path, monkeypatch)
    result, plan = publishing.quality()
    monkeypatch.setattr(public_artifacts, "_plan", lambda *_: plan)
    client = publishing.FakeClient()
    async def fetch(*args, **kwargs):
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
            assert "Sweden Central" in email.request.subject and SOURCE not in email.request.html
            detail = adapters.records.read_artifact("presentation/report")
            assert SOURCE in detail["markdown"]
            report_path = adapters.records._path("artifacts", "presentation/report").with_suffix(".md")
            assert report_path.read_text(encoding="utf-8") == detail["markdown"]
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
    async def fetch(*args, **kwargs):
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
                                   fetch_context=lambda *args, **kwargs: pytest.fail("Refetched context")) as adapters:
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


def prior_email(
    runtime, identifier, *, cutoff=CUTOFF, status="accepted", mode="official",
    mutate=None, save_inputs=True,
):
    day = date.fromisoformat(cutoff[:10])
    request = EmailRequest(
        identifier, "synthetic@example.invalid", "Synthetic report", "<html>synthetic</html>",
        mode, day.isoformat(), mode == "test", 1 if mode == "test" else 0,
    )
    terminal = status in {"accepted", "delivered", "unknown", "rejected"}
    record = EmailRecord(
        request, status, "synthetic-claim" if status != "prepared" else None,
        {"synthetic": "outcome"} if terminal else None,
    )
    runtime.outbox("email").save_progress(identifier, record.to_private_dict())
    frozen = {
        "test_run": mode == "test", "rerun": 1 if mode == "test" else 0,
        "report_date": day.isoformat(), "report": publishing.quality()[0].to_dict(),
        "work_item_context": {
            "schema_version": "1.0", "status": "available",
            "snapshot": snapshot(cutoff=cutoff, report_date=day),
        },
    }
    if mutate is not None:
        mutate(frozen)
    if save_inputs:
        runtime.run(identifier).save_completed("delivery-inputs", frozen)
    return record


@pytest.mark.parametrize("status,mode", [
    ("accepted", "test"), ("delivered", "test"),
    ("accepted", "failure"), ("delivered", "failure"),
    ("rejected", "official"), ("unknown", "official"),
    ("claimed", "official"), ("prepared", "official"),
])
def test_test_failed_uncertain_and_unclaimed_sends_do_not_advance_official_anchor(tmp_path, status, mode):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    with runtime.ownership():
        prior_email(runtime, "friday", cutoff=CUTOFF)
        prior_email(runtime, "later", cutoff="2026-09-06T19:00:00+00:00", status=status, mode=mode)
        assert _previous_official_snapshot(runtime, RUN) == {"delivery_id": "friday", "cutoff": CUTOFF}


def test_official_anchor_uses_successful_snapshot_cutoff_not_report_midnight_or_file_order(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    with runtime.ownership():
        prior_email(runtime, "accepted-later", cutoff="2026-09-05T18:31:43+00:00", status="accepted")
        prior_email(runtime, "written-last-but-earlier", cutoff=CUTOFF, status="delivered")
        assert _previous_official_snapshot(runtime, RUN) == {
            "delivery_id": "accepted-later", "cutoff": "2026-09-05T18:31:43+00:00",
        }


def test_monday_window_uses_friday_cutoff_and_resume_ignores_newer_history(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    config(runtime, adx=False)
    calls = []
    monday = date(2026, 9, 7)
    async def fetch(url, day, *, previous_snapshot):
        calls.append(previous_snapshot)
        return snapshot(
            cutoff="2026-09-07T17:20:00+00:00", report_date=day, previous=previous_snapshot,
        )
    async def run():
        async with integration(
            tmp_path, runtime, run_id="monday", report_date=monday, test_run=True, fetch_context=fetch,
        ) as adapters:
            assert adapters.work_item_context["snapshot"]["window"]["start"] == CUTOFF
    with runtime.ownership():
        prior_email(runtime, "friday")
        asyncio.run(run())
        frozen = runtime.run("monday").read_completed("work-item-context")
        prior_email(runtime, "sunday", cutoff="2026-09-06T20:00:00+00:00")
        asyncio.run(run())
        assert runtime.run("monday").read_completed("work-item-context") == frozen
    assert calls == [{"delivery_id": "friday", "cutoff": CUTOFF}]


def test_absent_sent_snapshot_history_uses_explicit_initial_lookback(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    config(runtime, adx=False)
    async def fetch(url, day, *, previous_snapshot):
        assert previous_snapshot is None
        return snapshot()
    async def run():
        async with integration(tmp_path, runtime, test_run=True, fetch_context=fetch) as adapters:
            html = render_work_item_html(adapters.work_item_context)
            assert "Initial 7-day lookback" in html and "no prior successfully submitted official snapshot" in html
    with runtime.ownership():
        prior_email(runtime, "prior-test", mode="test")
        prior_email(runtime, "prior-unknown", status="unknown")
        asyncio.run(run())


@pytest.mark.parametrize("malformation,expected", [
    ("missing", "work_item_anchor_metadata_missing"),
    ("legacy", "work_item_anchor_legacy"),
    ("restyled-legacy", "work_item_anchor_legacy"),
    ("window", "work_item_anchor_metadata_invalid"),
    ("eligibility", "work_item_anchor_metadata_invalid"),
    ("type", "work_item_anchor_metadata_invalid"),
])
def test_missing_or_malformed_official_metadata_is_disclosed_not_guessed(tmp_path, malformation, expected):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    config(runtime, adx=False)
    def mutate(value):
        if malformation == "legacy":
            del value["work_item_context"]
        elif malformation == "restyled-legacy":
            entry = value["work_item_context"]["snapshot"]["active"][0]
            value["work_item_context"] = {
                "schema_version": "legacy-1.0", "status": "available", "snapshot": {
                    "report_date": DAY.isoformat(), "closed_on": "2026-09-03",
                    "active": [{key: item for key, item in entry.items() if key != "type"}],
                    "closed": [],
                },
            }
        elif malformation == "window":
            del value["work_item_context"]["snapshot"]["window"]
        elif malformation == "eligibility":
            value["report"]["team_report_eligible"] = False
        elif malformation == "type":
            value["work_item_context"]["snapshot"]["active"][0]["type"] = None
    async def forbidden(*args, **kwargs):
        pytest.fail("An unknown boundary was guessed for the query")
    async def run():
        async with integration(tmp_path, runtime, test_run=True, fetch_context=forbidden) as adapters:
            assert adapters.work_item_context["code"] == expected
            assert adapters.warnings == {"work_item_unavailable"}
            assert "Unavailable" in render_work_item_html(adapters.work_item_context)
    with runtime.ownership():
        prior_email(runtime, "friday", mutate=mutate, save_inputs=malformation != "missing")
        asyncio.run(run())


def test_known_unavailable_prior_snapshot_does_not_advance_available_anchor(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    with runtime.ownership():
        prior_email(runtime, "friday")
        prior_email(
            runtime, "later", cutoff="2026-09-06T19:00:00+00:00",
            mutate=lambda value: value.update(work_item_context=unavailable_context("work_item_unavailable")),
        )
        assert _previous_official_snapshot(runtime, RUN) == {"delivery_id": "friday", "cutoff": CUTOFF}


def test_unclaimed_accepted_record_cannot_establish_a_comparison_anchor(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    with runtime.ownership():
        record = prior_email(runtime, "invalid-accepted")
        malformed = record.to_private_dict()
        malformed["claim_id"] = None
        runtime.outbox("email").save_progress("invalid-accepted", malformed)
        with pytest.raises(QualityError, match="work_item_anchor_metadata_invalid"):
            _previous_official_snapshot(runtime, RUN)


def test_legacy_checkpoint_text_is_retained_without_refetch_or_synthetic_type_and_window(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    legacy = {
        "status": "available", "text": "Synthetic historical private text",
        "snapshot": {"report_date": DAY.isoformat(), "closed_on": "2026-09-03", "active": [], "closed": []},
    }
    async def forbidden(*args, **kwargs):
        pytest.fail("Historical snapshot changed on resume")
    async def run():
        async with integration(tmp_path, runtime, test_run=True, fetch_context=forbidden) as adapters:
            assert adapters.context == legacy["text"]
            assert adapters.work_item_context == unavailable_context("work_item_legacy_snapshot")
            assert "Unavailable" in render_work_item_html(adapters.work_item_context)
    with runtime.ownership():
        runtime.run(RUN).save_completed("work-item-context", legacy)
        asyncio.run(run())
        assert runtime.run(RUN).read_completed("work-item-context") == legacy


def test_legacy_unavailable_checkpoint_retains_failure_without_refetch(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    legacy = {"status": "unavailable", "code": "work_item_context_interrupted", "text": None}
    async def forbidden(*args, **kwargs):
        pytest.fail("Historical unavailable context was replaced")
    async def run():
        async with integration(tmp_path, runtime, test_run=True, fetch_context=forbidden) as adapters:
            assert adapters.work_item_context == unavailable_context("work_item_context_interrupted")
            assert adapters.context is None
    with runtime.ownership():
        runtime.run(RUN).save_completed("work-item-context", legacy)
        asyncio.run(run())
        assert runtime.run(RUN).read_completed("work-item-context") == legacy


def test_fetched_mutable_input_cannot_change_the_frozen_in_memory_context(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    config(runtime, adx=False)
    value = snapshot()
    async def fetch(*args, **kwargs):
        return value
    async def run():
        async with integration(tmp_path, runtime, test_run=True, fetch_context=fetch) as adapters:
            value["window"]["start"] = "2026-01-01T00:00:00+00:00"
            assert adapters.work_item_context == runtime.run(RUN).read_completed("work-item-context")
    with runtime.ownership():
        asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "type", "url", "scope", "window"])
def test_optional_fetch_failure_and_malformed_snapshots_freeze_unavailable_not_empty(tmp_path, failure):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    config(runtime, adx=False)
    async def fetch(*args, **kwargs):
        if failure == "timeout":
            raise TimeoutError("synthetic private timeout text")
        value = snapshot()
        if failure == "type":
            value["active"][0]["type"] = None
        elif failure == "url":
            value["active"][0]["url"] = "https://external.invalid/item/1"
        elif failure == "scope":
            value["query_url"] = QUERY.replace("/synthetic/", "/another/")
            value["active"][0]["url"] = value["active"][0]["url"].replace("/synthetic/", "/another/")
        elif failure == "window":
            value["window"].update(basis="previous_official_report", previous_delivery_id="invented")
        return value
    async def run():
        async with integration(tmp_path, runtime, test_run=True, fetch_context=fetch) as adapters:
            assert adapters.work_item_context["status"] == "unavailable"
            assert "None" not in render_work_item_html(adapters.work_item_context)
            assert "synthetic private timeout text" not in json.dumps(adapters.work_item_context)
            assert adapters.warnings == {"work_item_unavailable"}
    with runtime.ownership():
        asyncio.run(run())


def test_frozen_work_item_context_is_passed_only_to_private_email_preparation(tmp_path, monkeypatch):
    from agent_insights_quality import integration as module
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    config(runtime, adx=False)
    reviewed_catalog(tmp_path, monkeypatch)
    captured = []
    def prepare(outbox, delivery_id, result, **kwargs):
        captured.append(deepcopy(kwargs))
        raise OSError("synthetic stop after inspecting email boundary")
    monkeypatch.setattr(module, "prepare_email", prepare)
    async def fetch(*args, **kwargs):
        return snapshot()
    async def run():
        async with integration(tmp_path, runtime, test_run=True, fetch_context=fetch) as adapters:
            with pytest.raises(OSError, match="synthetic stop"):
                adapters.prepare_delivery(
                    publishing.quality()[0], ENVIRONMENT, SOURCE, rerun=1,
                    recipient=lambda: "synthetic@example.invalid",
                )
    with runtime.ownership():
        asyncio.run(run())
        frozen = runtime.run(RUN).read_completed("delivery-inputs")
        assert frozen["work_item_context"] == captured[0]["work_item_context"]
        assert frozen["private_context"] is None
        assert "Synthetic private quality item" not in json.dumps(frozen["report"])
        assert "work_item_context" not in frozen["report"]
        asyncio.run(run())
        assert captured[0]["work_item_context"] == captured[1]["work_item_context"]
