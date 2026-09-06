import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
import hashlib
import json
import re
import sys
import threading
from types import ModuleType

import pytest

from agent_insights_quality.contracts import Deployment, Step
from agent_insights_quality.errors import QualityError
from agent_insights_quality.invocation_context import (
    ALGORITHM, POLICY_KEY, invocation_context, planned_context, traceparent, validate_traceparent,
)
from agent_insights_quality.providers import AzureHttpTransport, AzureRuntime, HttpRequest, HttpResponse
from agent_insights_quality.providers.transport import FOUNDRY_SCOPE
from agent_insights_quality.selection import LastTest, select_staging
from agent_insights_quality.state import CheckpointError, RecordStore, RuntimeStore, StateError
from agent_insights_quality.telemetry import correlate
from agent_insights_quality.trace_audit import audit_run
import test_runner as fake


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    fake.fake_storage(monkeypatch)


class Wire:
    def __init__(self):
        self.calls = []
        self.reject = 0

    async def send(self, request):
        self.calls.append(request)
        await asyncio.sleep(0)
        if self.reject:
            self.reject -= 1
            return HttpResponse(429, {}, b'{"error":{"code":"synthetic_rejection"}}')
        identity = request.headers["x-ms-client-request-id"]
        return HttpResponse(200, {}, json.dumps({
            "id": "response-" + identity, "status": "completed", "output": "synthetic output",
        }).encode())


def harness(path, **settings):
    h = fake.Harness(path, issues=0, **settings)
    h.wire = Wire()
    h.native = AzureRuntime(h.cloud.environment, transport=h.wire, logs=h.cloud, clock=h.clock.now)
    h.cloud.invoke = h.native.invoke
    return h


def run_traffic(h, run_id="trial", **kwargs):
    with h.store.ownership():
        runner = h.runner(run_id, **kwargs)
        runner.initialize(h.catalog.targets, fake.DAY, kind=h.store.environment)
        async def target_traffic(target):
            work = runner._binding(target)
            attempts = runner._plan(target, work)
            deployment = await runner._deployment(target, work)
            return await runner._traffic(target, work, deployment, attempts)
        try:
            return asyncio.run(runner._gather(target_traffic(t) for t in h.catalog.targets))
        finally:
            runner.logger.close()


@pytest.mark.parametrize("request_id", ["opaque fake request", "a" * 32, "0" * 32, "synthetic-client-1"])
def test_w3c_derivation_is_stable_domain_separated_lowercase_and_nonzero(request_id):
    value = traceparent(request_id)
    validate_traceparent(value)
    assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", value)
    assert int(value[3:35], 16) and int(value[36:52], 16)
    assert value == traceparent(request_id) != traceparent(request_id + "-other")
    assert value[36:52] != value[3:19]
    assert value[3:35] == hashlib.sha256(
        b"agent-insights-quality/invocation-context/v1\0trace\0" + request_id.encode()
    ).hexdigest()[:32]


def test_zero_digest_has_valid_nonzero_fallback(monkeypatch):
    class Zero:
        def hexdigest(self):
            return "0" * 64
    monkeypatch.setattr("agent_insights_quality.invocation_context.hashlib.sha256", lambda _: Zero())
    value = traceparent("synthetic")
    validate_traceparent(value)
    assert int(value[3:35], 16) == int(value[36:52], 16) == 1


@pytest.mark.parametrize("value", ["", None, "00-" + "0" * 32 + "-" + "1" * 16 + "-01",
                                  "00-" + "A" * 32 + "-" + "1" * 16 + "-01"])
def test_invalid_context_is_rejected(value):
    with pytest.raises(QualityError):
        validate_traceparent(value)


def test_two_agents_same_native_version_and_concurrent_policy_scopes_do_not_share_context(tmp_path):
    h = harness(tmp_path)
    first = Deployment("weather-agent/v0", "weather-agent", "42", "prompt", "source")
    second = replace(first, target_key="healthcare-agent/v0", agent_name="healthcare-agent")
    step = Step("setup", "setup", {"input": "synthetic"}, {})
    async def invoke(deployment, identity, enabled):
        with invocation_context(enabled):
            return await h.native.invoke(
                deployment, step, request_id=identity, session_id=None,
                previous_response_id=None, persist=lambda value: None,
            )
    async def run():
        return await asyncio.gather(
            invoke(first, "request-one", True), invoke(second, "request-two", True),
            invoke(first, "legacy-request", False),
        )
    results = asyncio.run(run())
    assert len(results) == 3 and len(h.wire.calls) == 3
    headers = {c.headers["x-ms-client-request-id"]: c.headers for c in h.wire.calls}
    assert headers["request-one"]["traceparent"] != headers["request-two"]["traceparent"]
    assert headers["request-one"]["traceparent"] == traceparent("request-one")
    assert "traceparent" not in headers["legacy-request"]
    assert all("baggage" not in h and "tracestate" not in h for h in headers.values())


@pytest.mark.parametrize("hosted", [False, True])
def test_turns_and_attempts_have_unique_context_without_changing_conversation_or_body(tmp_path, hosted):
    h = harness(tmp_path, hosted=hosted, agents=2)
    results = run_traffic(h)
    assert len(h.wire.calls) == 40
    assert len({r.headers["traceparent"] for r in h.wire.calls}) == 40
    for target, receipts in zip(h.catalog.targets, results):
        source = h.store.run("trial").read("targets/" + target.key + "/source")
        for attempt in range(1, 11):
            setup, probe = receipts[attempt, "setup"], receipts[attempt, "probe"]
            wires = {r.headers["x-ms-client-request-id"]: r for r in h.wire.calls}
            one, two = wires[setup.request_id], wires[probe.request_id]
            body1, body2 = json.loads(one.body), json.loads(two.body)
            assert body1["input"] == f"synthetic setup {attempt}"
            assert body2["input"] == f"synthetic probe {attempt}"
            if hosted:
                assert body1["agent_session_id"] == body2["agent_session_id"] == setup.session_id
                assert "previous_response_id" not in body2
            else:
                assert "previous_response_id" not in body1
                assert body2["previous_response_id"] == setup.response_id
            for name, receipt, wire in [("setup", setup, one), ("probe", probe, two)]:
                saved = h.store.run("trial").read_completed(
                    source["work_key"] + f"/traffic/attempt-{attempt:02d}/{name}/outbound-context",
                )
                assert saved == planned_context(receipt.request_id, source["traffic_source_revision"])
                assert wire.headers["traceparent"] == saved["traceparent"]
                assert "not_wire_delivery_proof" in saved["provenance"]
    assert h.store.run("trial").read_completed(POLICY_KEY) == {"algorithm": ALGORITHM}


def test_rejected_retry_reuses_request_and_header_then_completed_resume_never_posts(tmp_path):
    h = harness(tmp_path, daily_attempt_workers=1)
    h.wire.reject = 1
    run_traffic(h)
    assert len(h.wire.calls) == 21
    assert h.wire.calls[0].headers == h.wire.calls[1].headers
    assert h.wire.calls[0].body == h.wire.calls[1].body
    assert len({r.headers["traceparent"] for r in h.wire.calls}) == 20
    h.settings = replace(h.settings, invocation_trace_context=0)
    run_traffic(h)
    assert len(h.wire.calls) == 21
    assert h.store.run("trial").read_completed(POLICY_KEY) == {"algorithm": ALGORITHM}


@pytest.mark.parametrize("failure", ["policy", "context", "receipt"])
def test_failed_pre_post_persistence_sends_nothing_and_prepared_context_resumes(tmp_path, monkeypatch, failure):
    h = harness(tmp_path, daily_attempt_workers=1)
    completed, progress = RecordStore.save_completed, RecordStore.save_progress
    def save_completed(records, key, value):
        if failure == "policy" and key == POLICY_KEY or failure == "context" and key.endswith("/outbound-context"):
            raise CheckpointError()
        completed(records, key, value)
    def save_progress(records, key, value):
        if failure == "receipt" and key.endswith("/attempt-01/setup") and value.get("status") == "submitting":
            raise CheckpointError()
        progress(records, key, value)
    with monkeypatch.context() as patch:
        patch.setattr(RecordStore, "save_completed", save_completed)
        patch.setattr(RecordStore, "save_progress", save_progress)
        with pytest.raises(CheckpointError):
            run_traffic(h)
    assert not h.wire.calls
    if failure == "receipt":
        target = h.catalog.targets[0]
        work = h.store.run("trial").read("targets/" + target.key + "/source")["work_key"]
        prepared = h.store.run("trial").read_completed(work + "/traffic/attempt-01/setup/outbound-context")
        run_traffic(h)
        assert h.wire.calls[0].headers["traceparent"] == prepared["traceparent"]
        assert h.wire.calls[0].headers["x-ms-client-request-id"] == prepared["request_id"]


@pytest.mark.parametrize("damage", ["policy", "context"])
def test_corrupt_frozen_context_fails_before_any_new_post(tmp_path, monkeypatch, damage):
    h = harness(tmp_path, daily_attempt_workers=1)
    original = RecordStore.save_progress
    def stop(records, key, value):
        if key.endswith("/attempt-01/setup"):
            raise CheckpointError()
        original(records, key, value)
    with monkeypatch.context() as patch:
        patch.setattr(RecordStore, "save_progress", stop)
        with pytest.raises(CheckpointError):
            run_traffic(h)
    records = h.store.run("trial")
    if damage == "policy":
        path = records._path("completed", POLICY_KEY)
        path.write_text('{"algorithm":true}')
    else:
        target = h.catalog.targets[0]
        key = records.read("targets/" + target.key + "/source")["work_key"]
        path = records._path("completed", key + "/traffic/attempt-01/setup/outbound-context")
        value = json.loads(path.read_text())
        path.write_text(json.dumps({**value, "traceparent": traceparent("different")}))
    with pytest.raises(StateError):
        run_traffic(h)
    assert not h.wire.calls


def test_unknown_submission_is_not_reposted_or_given_new_context(tmp_path):
    h = harness(tmp_path, daily_attempt_workers=1)
    original = h.wire.send
    async def unknown(request):
        await original(request)
        raise QualityError("synthetic_lost_response", request_accepted=None, retryable=True)
    h.wire.send = unknown
    first = run_traffic(h)[0]
    assert len(h.wire.calls) == 10
    assert all(first[i, "setup"].status == "unknown" for i in range(1, 11))
    before = [(r.headers, r.body) for r in h.wire.calls]
    run_traffic(h)
    assert [(r.headers, r.body) for r in h.wire.calls] == before


def test_legacy_and_inherited_traffic_never_acquires_new_context(tmp_path):
    h = harness(tmp_path, invocation_trace_context=0)
    run_traffic(h, "older", test_run=True, rerun=1)
    records = h.store.run("older")
    records._path("completed", POLICY_KEY).unlink()
    h.settings = replace(h.settings, invocation_trace_context=1)
    run_traffic(h, "older", test_run=True, rerun=1)
    assert records.read_completed(POLICY_KEY) == {"algorithm": None}
    run_traffic(h, "newer", test_run=True, rerun=2, reuse_run_id="older")
    assert h.store.run("newer").read_completed(POLICY_KEY) == {"algorithm": ALGORITHM}
    assert len(h.wire.calls) == 20 and all("traceparent" not in r.headers for r in h.wire.calls)
    assert not list(records.directory.rglob("outbound-context.json"))


@pytest.mark.parametrize("changed", [
    ("invocation_context.py",), ("providers", "runtime.py"), ("providers", "transport.py"),
])
@pytest.mark.parametrize("hosted", [False, True])
def test_trace_driver_change_selects_fresh_staging_traffic_not_reassessment(tmp_path, changed, hosted):
    h = harness(tmp_path, agents=2, hosted=hosted)
    history = {t.key: LastTest("old", "PASS", fake.DAY.isoformat()) for t in h.catalog.targets}
    path = h.catalog.root.joinpath("src", "agent_insights_quality", *changed)
    selected = select_staging(h.catalog, last_tests=history, changed_paths=(path,))
    assert {s.target.key for s in selected} == {t.key for t in h.catalog.targets}
    assert all(s.action == "traffic" and s.reasons == ("traffic_changed",) for s in selected)


@pytest.mark.parametrize("changed,action", [
    (("reporting.py",), None), (("report_review.py",), None), (("trace_audit.py",), None),
    (("providers", "sol.py"), "reassess"), (("assessment.py",), "reassess"),
])
def test_unrelated_report_and_evaluation_changes_do_not_force_fresh_traffic(tmp_path, changed, action):
    h = harness(tmp_path, agents=2)
    history = {t.key: LastTest("old", "PASS", fake.DAY.isoformat()) for t in h.catalog.targets}
    path = h.catalog.root.joinpath("src", "agent_insights_quality", *changed)
    selected = select_staging(h.catalog, last_tests=history, changed_paths=(path,))
    if action is None:
        assert selected == ()
    else:
        assert len(selected) == 2 and all(s.action == action for s in selected)


@pytest.mark.parametrize("case", ["traceparent", "TraceParent", "TRACEPARENT"])
def test_actual_urllib_receives_pinned_context_without_ambient_or_auto_instrumentation(monkeypatch, case):
    ambient = ContextVar("synthetic_ambient", default=None)
    suppressed = ContextVar("synthetic_suppressed", default=False)
    utils = ModuleType("opentelemetry.instrumentation.utils")
    @contextmanager
    def suppress():
        token = suppressed.set(True)
        try:
            yield
        finally:
            suppressed.reset(token)
    utils.suppress_instrumentation = suppress
    monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.utils", utils)
    seen = []
    class Reply:
        status, headers = 200, {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return b"{}"
    class Opener:
        def open(self, request, **kwargs):
            assert ambient.get() is None
            if not suppressed.get():
                request.add_header("TraceParent", traceparent("wrong-ambient"))
            seen.append({k.casefold(): v for k, v in request.header_items()})
            return Reply()
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    transport = AzureHttpTransport()
    monkeypatch.setattr(transport, "_bearer_token", lambda _: "synthetic-token")
    token = ambient.set("synthetic-shared-baggage")
    try:
        result = asyncio.run(transport.send(HttpRequest(
            "POST", "https://synthetic.invalid/response", FOUNDRY_SCOPE,
            {case: traceparent("logical-request")}, b'{"input":"unchanged"}',
        )))
        assert ambient.get() == "synthetic-shared-baggage" and not suppressed.get()
    finally:
        ambient.reset(token)
    assert result.status == 200 and len(seen) == 1
    assert seen[0]["traceparent"] == traceparent("logical-request")
    assert "baggage" not in seen[0] and "tracestate" not in seen[0]


@pytest.mark.parametrize("name", ["TraceParent", "TRACEPARENT", "Baggage", "TraceState"])
def test_instrumentation_header_overwrite_fails_before_network(monkeypatch, name):
    transport = AzureHttpTransport()
    monkeypatch.setattr(transport, "_bearer_token", lambda _: "synthetic-token")
    entered = []
    class Opener:
        def open(self, request, **kwargs):
            request.add_unredirected_header(name, traceparent("different"))
            entered.append(request)
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    with pytest.raises(QualityError, match="trace_context_header_overwrite"):
        asyncio.run(transport.send(HttpRequest(
            "POST", "https://synthetic.invalid/response", FOUNDRY_SCOPE,
            {"traceparent": traceparent("logical")},
        )))
    assert not entered


@pytest.mark.parametrize("extra", [
    {"TraceParent": traceparent("other")}, {"BAGGAGE": "synthetic=value"}, {"TraceState": "synthetic=value"},
])
def test_duplicate_or_ambient_context_headers_rejected_before_credentials(monkeypatch, extra):
    transport = AzureHttpTransport()
    monkeypatch.setattr(transport, "_bearer_token", lambda _: pytest.fail("Invalid context reached credentials"))
    with pytest.raises(QualityError, match="trace_context_headers_conflict"):
        asyncio.run(transport.send(HttpRequest(
            "POST", "https://synthetic.invalid/response", FOUNDRY_SCOPE,
            {"traceparent": traceparent("logical"), **extra},
        )))


def test_isolated_agent_send_still_drains_thread_across_repeated_cancellation(monkeypatch):
    release, finished = threading.Event(), threading.Event()
    transport = AzureHttpTransport()
    calls = []
    async def run():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        def send(request):
            calls.append(request)
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(timeout=5)
                return HttpResponse(200, {}, b"{}")
            finally:
                finished.set()
        monkeypatch.setattr(transport, "_send", send)
        task = asyncio.create_task(transport.send(HttpRequest(
            "POST", "https://synthetic.invalid/response", FOUNDRY_SCOPE,
            {"traceparent": traceparent("logical")},
        )))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done() and not finished.is_set()
        finally:
            release.set()
            settled = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(settled[0], asyncio.CancelledError)
        assert finished.is_set() and len(calls) == 1
    asyncio.run(run())


def test_broken_optional_instrumentation_fails_closed_for_manual_context(monkeypatch):
    monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.utils", ModuleType("opentelemetry.instrumentation.utils"))
    transport = AzureHttpTransport()
    monkeypatch.setattr(transport, "_send", lambda _: pytest.fail("Unsupported instrumentation reached HTTP"))
    with pytest.raises(QualityError, match="trace_context_instrumentation_unavailable"):
        asyncio.run(transport.send(HttpRequest(
            "POST", "https://synthetic.invalid/response", FOUNDRY_SCOPE,
            {"traceparent": traceparent("logical")},
        )))


def test_audit_is_alias_only_read_only_and_mismatches_do_not_change_score_or_readiness(tmp_path, monkeypatch):
    h = harness(tmp_path, agents=2)
    receipts = run_traffic(h)
    with h.store.ownership():
        for number, (target, values) in enumerate(zip(h.catalog.targets, receipts)):
            records = h.store.run("trial")
            source = records.read("targets/" + target.key + "/source")
            deployment = h.cloud.deployments[target.key]
            rows = [{
                "telemetry_table": "requests", "id": "root-" + receipt.response_id,
                "operation_Id": traceparent(receipt.request_id)[3:35] if number == 0 else "shared-platform-trace",
                "operation_ParentId": traceparent(receipt.request_id)[36:52],
                "customDimensions": {
                    "gen_ai.response.id": receipt.response_id, "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": deployment.agent_name, "gen_ai.agent.version": deployment.provider_version,
                },
            } for receipt in values.values()]
            snapshot = correlate(
                rows, [x.response_id for x in values.values()], deployment,
                observed_at=h.clock.now().isoformat(), window_start=h.clock.now().isoformat(),
                window_end=h.clock.now().isoformat(),
            )
            artifact = source["work_key"] + "/snapshots/audit-before"
            records.save_artifact(artifact, snapshot.to_private_dict())
            records.save_completed(source["work_key"] + "/insights", {"visible_snapshot": artifact})
            records.save_progress(source["work_key"] + "/evidence", {"artifact": "must-not-use-later-evidence"})
    before = {p: p.read_bytes() for p in h.store.directory.rglob("*.json")}
    def forbidden(*args, **kwargs):
        pytest.fail("Read-only comparison attempted a write or ownership acquisition")
    with monkeypatch.context() as patch:
        for name in ("save_completed", "save_progress", "save_artifact"):
            patch.setattr(RecordStore, name, forbidden)
        patch.setattr(RuntimeStore, "ownership", forbidden)
        report = audit_run(h.store, "trial")
    assert report["counts"] == {"matches": 20, "different_context": 20}
    assert report["planned_contexts_distinct"] and report["distinct_expected_contexts"] == 40
    assert report["distinct_observed_contexts"] == 21
    assert len(report["shared_observed_contexts"]) == 1
    assert report["policy"] == "comparison_only_no_readiness_or_score_changes"
    assert report["propagation_observation"] == "mixed_contexts_requires_review"
    text = json.dumps(report)
    for request in h.wire.calls:
        assert request.headers["x-ms-client-request-id"] not in text
        assert request.headers["traceparent"] not in text
        assert request.headers["traceparent"][3:35] not in text
    assert "shared-platform-trace" not in text
    assert all(row["snapshot_basis"] == "pre_insights" for row in report["turns"])
    assert {p: p.read_bytes() for p in h.store.directory.rglob("*.json")} == before


def test_audit_without_roots_is_unknown_and_cli_reads_only(tmp_path, monkeypatch, capsys):
    h = harness(tmp_path)
    run_traffic(h)
    from agent_insights_quality import trace_audit
    monkeypatch.setattr(trace_audit, "RuntimeStore", lambda profile: h.store)
    monkeypatch.setattr(sys, "argv", ["trace_audit", "--profile", "daily", "--run-id", "trial"])
    assert trace_audit.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["propagation_observation"] == "unknown"
    assert report["counts"] == {"observed_unavailable": 20}
    assert report["acceptance"] == "not_determined_by_this_comparison"
    assert all(not row["observed_trace_aliases"] for row in report["turns"])
