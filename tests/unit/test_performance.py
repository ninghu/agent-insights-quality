import asyncio
import json
from pathlib import Path
import uuid

import pytest

from agent_insights_quality.errors import QualityError
from agent_insights_quality.performance import RunMetrics, binding, limited
from agent_insights_quality.selection import Selection
from agent_insights_quality.state import CheckpointError, RecordStore, RuntimeStore
import test_runner as fake


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    fake.fake_storage(monkeypatch)


class Clock:
    def __init__(self):
        self.time = 0.0

    def __call__(self):
        return self.time

    def advance(self, seconds):
        self.time += seconds


def test_queue_execution_overlap_and_wall_are_distinct(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    clock = Clock()
    with runtime.ownership():
        metrics = RunMetrics(runtime.run("synthetic"), monotonic=clock, segment_id="first")
        async def run():
            semaphore = asyncio.Semaphore(1)
            first_started, finish_first, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
            async def first():
                with binding(metrics, lane="one"), metrics.span("lane", "daily"):
                    async with limited(metrics, semaphore, "assessment"):
                        first_started.set()
                        await finish_first.wait()
            async def second():
                with binding(metrics, lane="two"), metrics.span("lane", "daily"):
                    second_started.set()
                    async with limited(metrics, semaphore, "assessment"):
                        clock.advance(3)
            a = asyncio.create_task(first())
            await first_started.wait()
            clock.advance(1)
            b = asyncio.create_task(second())
            await second_started.wait()
            clock.advance(4)
            finish_first.set()
            await asyncio.gather(a, b)
        asyncio.run(run())
        path = metrics.finalize()
    report = json.loads(Path(path).read_text())
    assert report["wall_elapsed_seconds"] == 8
    queues = [item for item in report["observations"] if item["kind"] == "queue"]
    execution = [item for item in report["observations"] if item["kind"] == "execution"]
    assert [item["elapsed_seconds"] for item in queues] == [0, 4]
    assert [item["elapsed_seconds"] for item in execution] == [5, 3]
    assert report["totals"]["lane:daily"]["measured_seconds"] == 12
    assert report["peak_active"]["lane:daily"] == 2
    assert report["peak_active"]["execution:assessment"] == 1
    assert not report["active"]
    assert [item["start_offset_seconds"] for item in execution] == [0, 5]
    assert [item["end_offset_seconds"] for item in execution] == [5, 8]


def test_reuse_skip_and_segments_do_not_fake_fast_service_or_overwrite(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    clock = Clock()
    with runtime.ownership():
        records = runtime.run("synthetic")
        first = RunMetrics(records, monotonic=clock, segment_id="first")
        with first.span("stage", "traffic"):
            clock.advance(7)
        first.finalize()
        original = records.read_artifact(first.artifact_key)
        clock.advance(100)
        second = RunMetrics(records, monotonic=clock, segment_id="second")
        with second.span("stage", "traffic"):
            second.reuse("stage", "traffic")
            clock.advance(2)
        second.reuse("turn", "invoke", skipped=True, attempt=1, turn="probe")
        second.finalize()
        assert records.read_artifact(first.artifact_key) == original
        report = records.read_artifact(second.artifact_key)
        assert report["wall_elapsed_seconds"] == 2
        assert [item["elapsed_seconds"] for item in report["observations"]] == [None, None]
        assert report["totals"]["stage:traffic"]["reused"] == 1
        assert report["totals"]["turn:invoke"]["skipped"] == 1
        assert report["totals"]["stage:traffic"]["measured_seconds"] == 0
        assert records.read("performance/latest")["segment_id"] == "second"


def test_cancellation_decrements_active_and_preserves_original_exception(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    clock = Clock()
    with runtime.ownership():
        metrics = RunMetrics(runtime.run("synthetic"), monotonic=clock, segment_id="cancel")
        async def run():
            semaphore = asyncio.Semaphore(1)
            ready = asyncio.Event()
            async def work():
                async with limited(metrics, semaphore, "deployment"):
                    with metrics.span("port_call", "ensure_deployment"):
                        ready.set()
                        await asyncio.Event().wait()
            task = asyncio.create_task(work())
            await ready.wait()
            clock.advance(3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert semaphore._value == 1
        asyncio.run(run())
        metrics.finalize("cancelled")
        report = runtime.run("synthetic").read_artifact(metrics.artifact_key)
        assert not report["active"]
        assert report["totals"]["port_call:ensure_deployment"]["statuses"] == {"cancelled": 1}
        assert report["totals"]["port_call:ensure_deployment"]["measured_seconds"] == 3


@pytest.mark.parametrize("fail_at", ["progress", "artifact"])
def test_unwritable_metrics_warn_only_and_never_hide_original_error(tmp_path, monkeypatch, capsys, fail_at):
    runtime = RuntimeStore("daily", root=tmp_path)
    method = getattr(RecordStore, "save_" + fail_at)
    def fail(records, key, value):
        if key.startswith("performance/"):
            raise CheckpointError()
        return method(records, key, value)
    monkeypatch.setattr(RecordStore, "save_" + fail_at, fail)
    sentinel = QualityError("synthetic_original", request_accepted=None)
    with runtime.ownership():
        metrics = RunMetrics(runtime.run("synthetic"), segment_id="failed")
        with pytest.raises(QualityError) as caught:
            with metrics.span("port_call", "invoke"):
                raise sentinel
        assert caught.value is sentinel
        assert metrics.finalize("failed") is None
        runtime.run("synthetic").save_progress("primary-checkpoint", {"status": "retained"})
    assert metrics.health_warnings == ("performance_persistence_failed",)
    output = capsys.readouterr()
    assert "WARNING performance_persistence_failed" in output.err
    assert "synthetic_original" not in output.err


def test_record_limit_discloses_drops_and_periodic_progress_keeps_small_summary(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        records = runtime.run("synthetic")
        metrics = RunMetrics(records, segment_id="bounded", max_records=2, checkpoint_every=2)
        for index in range(5):
            metrics.reuse("turn", "invoke", attempt=index + 1, turn="probe")
        progress = records.read("performance/bounded")
        assert "observations" not in progress
        assert progress["totals"]["turn:invoke"]["count"] == 4
        assert progress["persisted_observation_count"] == 2
        batch = records.read_artifact("performance/bounded/batches/batch-000001")
        assert len(batch["observations"]) == 2
        metrics.finalize()
        report = records.read_artifact(metrics.artifact_key)
        assert len(report["observations"]) == 2
        assert report["dropped_records"] == 3
        assert report["totals"]["turn:invoke"]["count"] == 5


def test_queue_cancellation_never_acquires_or_changes_pending_work(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        metrics = RunMetrics(runtime.run("synthetic"), segment_id="queued")
        async def run():
            semaphore = asyncio.Semaphore(0)
            waiting = asyncio.Event()
            async def work():
                waiting.set()
                async with limited(metrics, semaphore, "assessment"):
                    pytest.fail("Cancelled waiter executed")
            task = asyncio.create_task(work())
            await waiting.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert semaphore._value == 0
        asyncio.run(run())
        metrics.finalize("cancelled")
        assert not metrics.summary("cancelled")["active"]
        assert metrics.totals["queue:assessment"]["statuses"] == {"cancelled": 1}


@pytest.mark.parametrize("enabled", [False, True])
def test_primary_checkpoint_failure_remains_fatal_with_metrics(tmp_path, monkeypatch, enabled):
    h = fake.Harness(tmp_path, agents=2)
    save = RecordStore.save_progress
    def fail(records, key, value):
        if "/traffic/attempt-" in key:
            raise CheckpointError()
        save(records, key, value)
    monkeypatch.setattr(RecordStore, "save_progress", fail)
    with h.store.ownership():
        metrics = RunMetrics(h.store.run("trial"), segment_id="trial") if enabled else None
        r = h.runner(metrics=metrics)
        r.initialize(h.catalog.targets, fake.DAY, kind="daily")
        with pytest.raises(CheckpointError):
            asyncio.run(r.run_daily(h.catalog.targets))
        if metrics:
            metrics.finalize("failed")
            assert not metrics.summary("failed")["active"]
        r.logger.close()
    assert not h.cloud.invocations and not h.cloud.starts and not h.sol.calls


@pytest.mark.parametrize("profile", ["daily", "staging"])
@pytest.mark.parametrize("uncertain", [False, True])
def test_fake_pipeline_is_identical_with_metrics_including_resume(tmp_path, monkeypatch, profile, uncertain):
    from agent_insights_quality import runner
    def run(root, enabled):
        count = 0
        def identity():
            nonlocal count
            count += 1
            return uuid.UUID(int=count)
        monkeypatch.setattr(runner.uuid, "uuid4", identity)
        h = fake.Harness(root, profile=profile, agents=2, issues=1, deployment_workers=1)
        h.cloud.pending_deployments = 1
        failed = False
        def unknown(*args):
            nonlocal failed
            if uncertain and not failed:
                failed = True
                raise QualityError("synthetic_unknown", retryable=True, request_accepted=None)
        h.cloud.invoke_hook = unknown
        summaries = []
        outcomes = []
        with h.store.ownership():
            for segment in ("initial", "resume"):
                metrics = RunMetrics(
                    h.store.run("trial"), monotonic=h.clock.monotonic,
                    segment_id=segment, checkpoint_every=100,
                ) if enabled else None
                r = h.runner(metrics=metrics)
                r.initialize(h.catalog.targets, fake.DAY, kind=profile)
                if profile == "daily":
                    result = asyncio.run(r.run_daily(h.catalog.targets)).to_dict()
                else:
                    result = asyncio.run(r.run_staging(tuple(
                        runner.Selection(target, "traffic", ("missing",)) for target in h.catalog.targets
                    )))
                outcomes.append(result)
                if metrics:
                    metrics.finalize()
                    summaries.append(h.store.run("trial").read_artifact(metrics.artifact_key))
                r.logger.close()
        checkpoints = {
            str(path.relative_to(h.store.directory)): json.loads(path.read_text())
            for path in h.store.directory.rglob("*.json")
            if "performance" not in path.parts
        }
        return h, outcomes, checkpoints, summaries
    plain, results_plain, checkpoints_plain, _ = run(tmp_path / "plain", False)
    measured, results_measured, checkpoints_measured, summaries = run(tmp_path / "measured", True)
    assert measured.cloud.events == plain.cloud.events
    assert measured.cloud.invocations == plain.cloud.invocations
    assert measured.cloud.starts == plain.cloud.starts
    assert measured.cloud.resets == plain.cloud.resets
    assert measured.sol.calls == plain.sol.calls
    assert measured.clock.waits == plain.clock.waits
    assert results_measured == results_plain
    assert checkpoints_measured == checkpoints_plain
    assert all(not item["active"] for item in summaries)
    assert summaries[0]["totals"]["queue:deployment"]["count"] > 0
    assert summaries[0]["peak_active"]["execution:deployment"] == 1
    assert summaries[0]["configuration"]["daily_lanes"] == 5
    assert summaries[0]["configuration"]["attempts"] == 10
    assert summaries[0]["configuration"]["readiness_attempts"] == 6
    reused = [item for item in summaries[1]["observations"] if item["status"] == "reused"]
    assert reused and all(item["elapsed_seconds"] is None for item in reused)
    assert all(item.get("input_tokens") is None for item in summaries[0]["observations"]
               if item["kind"] == "model_call")
    assert not (measured.store.outbox("events").directory.exists())


def test_partial_resume_marks_reused_attempts_and_never_resubmits_unknown_turn(tmp_path):
    h = fake.Harness(tmp_path, profile="staging", issues=0, hosted=True)
    seen = []
    def interrupt(*args):
        seen.append(args[2])
        if len(seen) == 6:
            raise OSError("synthetic interrupted invocation")
    h.cloud.invoke_hook = interrupt
    with h.store.ownership():
        first = RunMetrics(h.store.run("trial"), segment_id="before")
        r = h.runner(metrics=first)
        r.initialize(h.catalog.targets, fake.DAY, kind="staging")
        selections = tuple(Selection(
            target, "traffic", ("missing",),
        ) for target in h.catalog.targets)
        with pytest.raises(OSError):
            asyncio.run(r.run_staging(selections))
        first.finalize("failed")
        r.logger.close()
        assert not first.summary("failed")["active"]
        h.cloud.invoke_hook = None
        second = RunMetrics(h.store.run("trial"), segment_id="after")
        resumed = h.runner(metrics=second)
        resumed.initialize(h.catalog.targets, fake.DAY, kind="staging")
        result = asyncio.run(resumed.run_staging(selections))
        second.finalize()
        resumed.logger.close()
        assert result["results"][0]["status"] == "PASS"
        attempts = [item for item in second.observations if item["kind"] == "attempt"]
        assert attempts[0]["status"] == attempts[1]["status"] == "reused"
        assert attempts[0]["elapsed_seconds"] is attempts[1]["elapsed_seconds"] is None
        turn = next(item for item in second.observations
                    if item["kind"] == "turn" and item["attempt"] == 3 and item["turn"] == "probe")
        assert turn["status"] == "reused" and turn["receipt_status"] == "unknown"
        assert turn["elapsed_seconds"] is None
    assert len(h.cloud.invocations) == 20
    assert sum(item[2] == seen[-1] for item in h.cloud.invocations) == 1
