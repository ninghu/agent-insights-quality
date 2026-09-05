import asyncio
import json

import pytest

from agent_insights_quality.errors import QualityError
from agent_insights_quality.performance import ObservedSol, RunMetrics
from agent_insights_quality.providers import HttpResponse, SolResponseError
from agent_insights_quality.state import RuntimeStore
import test_provider_sol_cooldown as fake
import test_runner


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    test_runner.fake_storage(monkeypatch)


def response(usage=None):
    value = json.loads(fake.success().body)
    if usage is not None:
        value["usage"] = usage
    return HttpResponse(200, {}, json.dumps(value).encode())


@pytest.mark.parametrize("usage,expected", [
    ({"input_tokens": 10, "output_tokens": 3, "private_extra": "never-copy"}, (10, 3, "known")),
    ({"input_tokens": 0, "output_tokens": 0}, (0, 0, "known")),
    ({"input_tokens": 12}, (12, None, "partial")),
    ({"input_tokens": -1, "output_tokens": True}, (None, None, "unknown")),
    (None, (None, None, "unknown")),
])
def test_only_actual_sol_usage_is_retained_without_modifying_result_or_wire(tmp_path, usage, expected):
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        metrics = RunMetrics(runtime.run("trial"), segment_id="usage")
        clock = fake.Clock()
        events = []
        def observe(event):
            events.append(event)
            metrics.observe_sol(event)
        plain = fake.Transport(clock, response(usage))
        measured = fake.Transport(clock, response(usage))
        assert asyncio.run(fake.complete(fake.provider(clock, plain))) == {"ok": True}
        assert asyncio.run(fake.complete(fake.provider(clock, measured, observer=observe))) == {"ok": True}
        assert plain.requests == measured.requests
        value = next(item for item in events if item["kind"] == "sol_usage")
        assert (value["input_tokens"], value["output_tokens"], value["usage_status"]) == expected
        assert "private_extra" not in json.dumps(events)
        metrics.finalize()
        report = runtime.run("trial").read_artifact(metrics.artifact_key)
        assert report["sol_usage"]["input_tokens"] == expected[0]
        assert report["sol_usage"]["output_tokens"] == expected[1]
        assert report["sol_usage"]["response_count"] == 1
        assert not report["active"]


def test_http_time_queue_and_actual_429_cooldown_are_separate_and_retry_unchanged(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    def execute(observer=None):
        clock = fake.Clock()
        class Transport(fake.Transport):
            async def send(self, request):
                value = await super().send(request)
                clock.now += 2
                return value
        transport = Transport(clock, fake.limited({"Retry-After": "60"}), response({"input_tokens": 20, "output_tokens": 4}))
        sol = fake.provider(clock, transport, observer=observer)
        result = asyncio.run(fake.complete(sol))
        return result, clock, transport
    with runtime.ownership():
        events = []
        a, ca, ta = execute()
        b, cb, tb = execute(events.append)
        assert a == b == {"ok": True}
        assert ca.waits == cb.waits == [60]
        assert ta.requests == tb.requests
        completed = [event for event in events if event["status"] != "started"]
        http = [event for event in completed if event["kind"] == "sol_http"]
        assert [item["elapsed_seconds"] for item in http] == [2, 2]
        assert [item["status"] for item in http] == ["rejected", "accepted"]
        assert [item["request_accepted"] for item in http] == [False, True]
        cooldown = next(event for event in completed if event["kind"] == "sol_cooldown")
        assert cooldown["elapsed_seconds"] == cooldown["requested_seconds"] == 60
        queue = next(event for event in completed if event["kind"] == "sol_recovery_queue")
        assert queue["elapsed_seconds"] == 0
        assert all(set(item) <= {
            "kind", "status", "elapsed_seconds", "requested_seconds", "http_status", "request_accepted",
            "input_tokens", "output_tokens", "usage_status",
        } for item in events)
        assert "headers" not in json.dumps(events) and "Synthetic private" not in json.dumps(events)


def test_unknown_sol_post_is_not_retried_by_observation_and_missing_usage_remains_unknown(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        metrics = RunMetrics(runtime.run("trial"), segment_id="unknown")
        clock = fake.Clock()
        error = QualityError("synthetic_unknown", request_accepted=None)
        transport = fake.Transport(clock, error)
        sol = fake.provider(clock, transport, observer=metrics.observe_sol)
        with pytest.raises(QualityError) as caught:
            asyncio.run(fake.complete(ObservedSol(sol, metrics)))
        plain = fake.Transport(clock, error)
        with pytest.raises(QualityError) as expected:
            asyncio.run(fake.complete(fake.provider(clock, plain)))
        assert caught.value.code == expected.value.code
        assert caught.value.request_accepted is expected.value.request_accepted is None
        assert len(transport.requests) == len(plain.requests) == 1
        metrics.finalize("failed")
        report = runtime.run("trial").read_artifact(metrics.artifact_key)
        assert report["sol_usage"]["response_count"] == 0
        assert report["sol_usage"]["input_tokens"] is report["sol_usage"]["output_tokens"] is None
        http = next(item for item in report["observations"] if item["kind"] == "sol_http")
        assert http["request_accepted"] is None and http["status"] == "failed"
        assert not report["active"]


def test_observer_io_failure_does_not_change_accepted_response_or_schema_validation(capsys):
    calls = []
    def unavailable(event):
        calls.append(event["kind"])
        raise OSError("private synthetic metrics failure")
    clock = fake.Clock()
    transport = fake.Transport(clock, response({"input_tokens": 3, "output_tokens": 2}))
    sol = fake.provider(clock, transport, observer=unavailable)
    assert asyncio.run(fake.complete(sol)) == {"ok": True}
    assert sol.observer_warnings == {"performance_persistence_failed"}
    assert len(transport.requests) == 1
    assert "private synthetic" not in capsys.readouterr().err
    malformed = HttpResponse(200, {}, b'{"status":"completed","output":[],"usage":{"input_tokens":4}}')
    transport = fake.Transport(clock, malformed)
    with pytest.raises(SolResponseError, match="sol_output_schema_invalid"):
        asyncio.run(fake.complete(fake.provider(clock, transport, observer=unavailable)))


def test_sol_http_cancellation_decrements_private_active_counter(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        metrics = RunMetrics(runtime.run("trial"), segment_id="cancel")
        async def run():
            ready = asyncio.Event()
            class Transport:
                async def send(self, request):
                    ready.set()
                    await asyncio.Event().wait()
            clock = fake.Clock()
            sol = fake.provider(clock, Transport(), observer=metrics.observe_sol)
            task = asyncio.create_task(fake.complete(sol))
            await ready.wait()
            assert metrics.active["sol_http:sol_http"] == 1
            clock.now += 3
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        asyncio.run(run())
        metrics.finalize("cancelled")
        assert not metrics.summary("cancelled")["active"]
        assert metrics.totals["sol_http:sol_http"]["statuses"] == {"cancelled": 1}


def test_actual_sol_http_overlap_peak_and_token_coverage_counts(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        metrics = RunMetrics(runtime.run("trial"), segment_id="overlap")
        async def run():
            ready, release = asyncio.Event(), asyncio.Event()
            requests = []
            class Transport:
                async def send(self, request):
                    requests.append(request)
                    if len(requests) == 2:
                        ready.set()
                    await release.wait()
                    return response({"input_tokens": 5, "output_tokens": 2})
            clock = fake.Clock()
            sol = fake.provider(clock, Transport(), observer=metrics.observe_sol)
            tasks = [asyncio.create_task(fake.complete(sol, str(index))) for index in range(2)]
            await ready.wait()
            assert metrics.active["sol_http:sol_http"] == 2
            clock.now += 2
            release.set()
            assert await asyncio.gather(*tasks) == [{"ok": True}, {"ok": True}]
            assert len(requests) == 2
        asyncio.run(run())
        metrics.finalize()
        report = runtime.run("trial").read_artifact(metrics.artifact_key)
        assert report["peak_active"]["sol_http:sol_http"] == 2
        assert report["totals"]["sol_http:sol_http"]["measured_seconds"] == 4
        assert report["sol_usage"] == {
            "response_count": 2, "known_input_count": 2, "known_output_count": 2,
            "input_tokens": 10, "output_tokens": 4,
        }
        assert not report["active"]
