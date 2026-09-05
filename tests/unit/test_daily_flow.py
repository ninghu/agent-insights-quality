"""Scheduling tests use synthetic ports, clocks and local checkpoint fixtures only."""

import asyncio
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import uuid

import pytest

from agent_insights_quality.contracts import Invocation
from agent_insights_quality.errors import QualityError
from agent_insights_quality.performance import RunMetrics
from agent_insights_quality.results import UnitId
from agent_insights_quality.state import CheckpointError, RecordStore
import test_runner as fake


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    fake.fake_storage(monkeypatch)


class InterleavedCloud(fake.Cloud):
    def __init__(self, clock, profile="daily"):
        super().__init__(clock, profile)
        self.running = Counter()
        self.peak = Counter()
        self.global_running = self.global_peak = 0
        self.responses, self.sessions = {}, {}
        self.completed = []

    async def invoke(self, deployment, step, **kwargs):
        key = deployment.target_key
        index = int(step.body["input"].split()[-1])
        binding = key, index
        session, previous = kwargs["session_id"], kwargs["previous_response_id"]
        if deployment.agent_type == "hosted_code":
            if step.phase == "setup":
                assert session is not None and session not in self.sessions.values()
                self.sessions[binding] = session
            assert session == self.sessions[binding] and previous is None
        else:
            assert session is None
            assert previous == self.responses.get(binding)
        if step.phase == "probe":
            assert binding in self.responses
        self.running[key] += 1
        self.global_running += 1
        self.peak[key] = max(self.peak[key], self.running[key])
        self.global_peak = max(self.global_peak, self.global_running)
        try:
            receipt = await super().invoke(deployment, step, **kwargs)
            self.responses[binding] = receipt.response_id
            self.completed.append((key, index, step.phase))
            return receipt
        finally:
            self.running[key] -= 1
            self.global_running -= 1


@pytest.mark.parametrize("hosted", [False, True])
@pytest.mark.parametrize("workers,budget", [(2, 10), (4, 10), (4, 4)])
def test_daily_attempt_bounds_conversations_and_frozen_version_boundary(tmp_path, hosted, workers, budget):
    h = fake.Harness(tmp_path, agents=5, issues=1, hosted=hosted,
                     daily_attempt_workers=workers, daily_attempt_budget=budget)
    h.cloud = InterleavedCloud(h.clock)
    h.sol = fake.Sol(h.cloud)
    activate = h.cloud.activate
    async def checked_activate(deployment):
        previous = h.cloud.active.get(deployment.agent_name)
        if previous:
            assert h.cloud.running[previous.target_key] == 0
            records = h.store.run("trial")
            work = records.read(f"targets/{previous.target_key}/source")["work_key"]
            assert records.read_completed(work + "/traffic-done") == {"completed": True}
            insight = records.read_completed(work + "/insights")
            assert records.read_artifact(work + "/insights/before")["cards"] == insight["before"]
            assert records.read_artifact(work + "/insights/after")["cards"] == insight["after"]
            assert records.read_artifact(insight["visible_snapshot"])
        await activate(deployment)
    h.cloud.activate = checked_activate
    assert h.daily().score == 100
    assert h.cloud.global_peak == budget
    assert all(1 < peak <= workers for peak in h.cloud.peak.values())
    assert not h.cloud.global_running and not any(h.cloud.running.values())
    assert len(h.cloud.invocations) == 10 * 20
    assert Counter(event[1] for event in h.cloud.events if event[0] == "activate") == {
        target.key: 1 for target in h.catalog.targets
    }
    for target in h.catalog.targets:
        calls = [item for item in h.cloud.invocations if item[0].target_key == target.key]
        if budget >= 5 * workers:
            assert [item[1].phase for item in calls[:2]] == ["setup", "setup"]
            assert h.cloud.peak[target.key] == workers
        assert len({item[2] for item in calls}) == 20
        assert Counter(json.dumps(item[1].body) for item in calls) == Counter(
            json.dumps(step.body) for attempt in fake.attempts(target) for step in attempt.steps
        )


@pytest.mark.parametrize("profile,travel", [("staging", False), ("daily", True)])
def test_staging_and_shared_travel_booking_fixture_remain_serial(tmp_path, profile, travel):
    h = fake.Harness(tmp_path, profile=profile, hosted=True, daily_attempt_workers=4)
    if travel:
        h.catalog = replace(h.catalog, agents=("travel-agent",), targets=tuple(
            replace(target, unit_id=UnitId("travel-agent", target.unit_id.logical_version))
            for target in h.catalog.targets
        ))
    h.cloud = InterleavedCloud(h.clock, profile)
    h.sol = fake.Sol(h.cloud)
    h.staging() if profile == "staging" else h.daily()
    assert set(h.cloud.peak.values()) == {1}
    for target in h.catalog.targets:
        assert [phase for key, _, phase in h.cloud.completed if key == target.key] == ["setup", "probe"] * 10


def test_configured_four_workers_are_supported_without_a_throughput_claim(tmp_path):
    h = fake.Harness(tmp_path, issues=0, daily_attempt_workers=4)
    h.cloud = InterleavedCloud(h.clock)
    h.sol = fake.Sol(h.cloud)
    h.daily()
    assert set(h.cloud.peak.values()) == {4}
    assert h.cloud.global_peak == 4 and len(h.cloud.invocations) == 20


@pytest.mark.parametrize("hosted", [False, True])
@pytest.mark.parametrize("parallel_workers", [2, 4])
def test_serial_parallel_fixed_outcomes_and_request_binding_are_equivalent(
    tmp_path, hosted, parallel_workers, monkeypatch,
):
    from agent_insights_quality import runner
    def run(workers):
        count = 0
        def identity():
            nonlocal count
            count += 1
            return uuid.UUID(int=count)
        monkeypatch.setattr(runner.uuid, "uuid4", identity)
        h = fake.Harness(tmp_path / str(workers), agents=2, issues=1, hosted=hosted,
                         daily_attempt_workers=workers)
        h.cloud = InterleavedCloud(h.clock)
        h.sol = fake.Sol(h.cloud)
        result = h.daily().to_dict()
        bindings = {}
        for deployment, step, request, session, previous in h.cloud.invocations:
            index = int(step.body["input"].split()[-1])
            bindings[deployment.target_key, index, step.step_id] = (
                step.body, step.expected, bool(session), bool(previous),
            )
        counts = Counter(event[0] for event in h.cloud.events)
        return result, bindings, counts, h
    serial, parallel = run(1), run(parallel_workers)
    assert serial[:3] == parallel[:3]
    assert serial[3].cloud.global_peak < parallel[3].cloud.global_peak
    assert len(serial[3].sol.calls) == len(parallel[3].sol.calls) == 4


def test_unknown_daily_conversation_does_not_repeat_or_advance_lane(tmp_path):
    h = fake.Harness(tmp_path, agents=2, issues=1)
    target = h.catalog.targets[0]
    unknown = []
    def fail(deployment, step, request, session, persist):
        if deployment.target_key == target.key and step.body["input"] == "synthetic setup 1":
            unknown.append(request)
            raise QualityError("synthetic_unknown", request_accepted=None)
    h.cloud.invoke_hook = fail
    h.daily()
    initial = [item[2] for item in h.cloud.invocations]
    h.daily()
    assert len(unknown) == 1
    assert [item[2] for item in h.cloud.invocations] == initial
    assert len(initial) == 19 + 40
    assert not any(event == ("activate", h.catalog.targets[1].key) for event in h.cloud.events)
    records = h.store.run("trial")
    work = records.read(f"targets/{target.key}/source")["work_key"]
    assert records.read(work + "/traffic/attempt-01/setup")["status"] == "unknown"
    assert records.read(work + "/traffic/attempt-01/probe")["status"] == "blocked"


@pytest.mark.parametrize("hosted", [False, True])
def test_cancelled_fresh_attempts_resume_saved_turns_without_reposting_unknowns(tmp_path, hosted):
    h = fake.Harness(tmp_path, hosted=hosted, daily_attempt_workers=2)
    original = h.cloud.invoke
    posted, completed = [], []
    async def run():
        saved, pending = asyncio.Event(), asyncio.Event()
        async def interrupted(deployment, step, **kwargs):
            posted.append(kwargs["request_id"])
            if step.body["input"] == "synthetic setup 1":
                receipt = await original(deployment, step, **kwargs)
                completed.append(receipt)
                saved.set()
                return receipt
            await saved.wait()
            pending.set()
            await asyncio.Event().wait()
        h.cloud.invoke = interrupted
        runner = h.runner(test_run=True, rerun=5, fresh_traffic=True)
        runner.initialize(h.catalog.targets, fake.DAY, kind="daily")
        task = asyncio.create_task(runner.run_daily(h.catalog.targets))
        await pending.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        runner.logger.close()
    with h.store.ownership():
        asyncio.run(run())
    records = h.store.run("trial")
    target = h.catalog.targets[0]
    work = records.read(f"targets/{target.key}/source")["work_key"]
    assert len(completed) == 1 and len(posted) == 3
    unknown_keys = [
        key for key in ("setup", "probe")
        if records.read(work + "/traffic/attempt-01/" + key)["status"] == "submitting"
    ]
    assert unknown_keys == ["probe"]
    old_sessions = {
        path.relative_to(records.directory): path.read_bytes()
        for path in records.directory.rglob("session.json")
    }
    h.cloud.invoke = original
    assert h.daily(test_run=True, rerun=5).score is None
    assert records.read_completed("run")["fresh_traffic"] is True
    assert records.read(work + "/traffic/attempt-01/probe")["status"] == "unknown"
    assert records.read(work + "/traffic/attempt-02/setup")["status"] == "unknown"
    assert records.read(work + "/traffic/attempt-02/probe")["status"] == "blocked"
    assert len(h.cloud.invocations) == 17
    assert all(item[2] not in posted for item in h.cloud.invocations[1:])
    assert not h.cloud.starts
    assert all((records.directory / key).read_bytes() == value for key, value in old_sessions.items())
    before = len(h.cloud.invocations)
    h.daily(test_run=True, rerun=5, fresh_traffic=True)
    assert len(h.cloud.invocations) == before


def test_pipeline_starts_before_slow_lane_finishes_and_binds_immutable_snapshots(tmp_path):
    h = fake.Harness(tmp_path, agents=5, issues=1)
    active = peak = 0
    held = []
    async def run():
        nonlocal active, peak
        gate = asyncio.Event()
        slow_started = asyncio.Event()
        complete = h.sol.complete_json
        invoke = h.cloud.invoke
        async def slow(deployment, step, **kwargs):
            if deployment.target_key == h.catalog.targets[-2].key:
                slow_started.set()
                await gate.wait()
            return await invoke(deployment, step, **kwargs)
        async def assess(**kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await slow_started.wait()
                held.append(deepcopy(kwargs["payload"]))
                if active == 4:
                    gate.set()
                await gate.wait()
                for _ in range(3):
                    await asyncio.sleep(0)
                return await complete(**kwargs)
            finally:
                active -= 1
        h.cloud.invoke, h.sol.complete_json = slow, assess
        metrics = RunMetrics(h.store.run("trial"), monotonic=h.clock.monotonic)
        runner = h.runner(metrics=metrics)
        runner.initialize(h.catalog.targets, fake.DAY, kind="daily")
        result = await runner.run_daily(h.catalog.targets)
        assert result.score == 100 and peak == 4 and active == 0
        assert len(held) == len(h.catalog.targets)
        for payload in held:
            unit = payload["target"]["unit_id"]
            target = next(item for item in h.catalog.targets if item.unit_id.to_dict() == unit)
            binding = h.store.run("trial").read(f"targets/{target.key}/source")
            insight = h.store.run("trial").read_completed(binding["work_key"] + "/insights")
            assert payload["card_snapshots"] == {"before": insight["before"], "after": insight["after"]}
            if target.is_baseline:
                assert not any(card["id"].startswith(target.unit_id.agent) for card in insight["after"])
        measured = [record for record in metrics.observations if record["kind"] == "model_call"]
        assert len(measured) == len(h.catalog.targets)
        assert {record["unit"] for record in measured} == {target.key for target in h.catalog.targets}
        assert all(record["unit"].startswith(record["lane"] + "/") for record in measured)
        assert not metrics.summary("completed")["active"]
        runner.logger.close()
    with h.store.ownership():
        asyncio.run(run())


def test_ordinary_assessment_failure_excludes_only_its_unit_without_blocking_lane(tmp_path):
    h = fake.Harness(tmp_path)
    complete = h.sol.complete_json
    calls = []
    async def assess(**kwargs):
        version = kwargs["payload"]["target"]["unit_id"]["logical_version"]
        calls.append(version)
        if version == "v0":
            raise QualityError("synthetic_sol_failure")
        return await complete(**kwargs)
    h.sol.complete_json = assess
    result = h.daily()
    assert result.status.value == "Partial" and result.score == 100
    assert result.coverage.excluded_units == 1
    assert len(calls) == len(h.cloud.starts) == 2 and len(h.cloud.invocations) == 40


@pytest.mark.parametrize("cancel", [False, True])
def test_pipeline_fatal_or_cancellation_drains_traffic_and_assessment_before_unlock(tmp_path, monkeypatch, cancel):
    h = fake.Harness(tmp_path, agents=2, issues=2)
    finalized, active = [], set()
    async def run():
        assessment_entered, traffic_entered = asyncio.Event(), asyncio.Event()
        invoke = h.cloud.invoke
        complete = h.sol.complete_json
        async def held_invoke(deployment, step, **kwargs):
            if not deployment.target_key.endswith("/v0"):
                active.add("traffic")
                receipt = Invocation(
                    kwargs["request_id"], None, kwargs["session_id"],
                    h.clock.now().isoformat(), "", "submitting",
                )
                kwargs["persist"](receipt)
                traffic_entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    active.discard("traffic")
                    finalized.append("traffic")
            return await invoke(deployment, step, **kwargs)
        async def held_assessment(**kwargs):
            active.add("assessment")
            assessment_entered.set()
            try:
                await traffic_entered.wait()
                if cancel:
                    await asyncio.Event().wait()
                return await complete(**kwargs)
            finally:
                active.discard("assessment")
                finalized.append("assessment")
        h.cloud.invoke, h.sol.complete_json = held_invoke, held_assessment
        save = RecordStore.save_artifact
        def fail(records, key, value):
            if not cancel and "/assessments/" in key:
                raise CheckpointError()
            save(records, key, value)
        monkeypatch.setattr(RecordStore, "save_artifact", fail)
        metrics = RunMetrics(h.store.run("trial"))
        runner = h.runner(metrics=metrics)
        runner.initialize(h.catalog.targets, fake.DAY, kind="daily")
        task = asyncio.create_task(runner.run_daily(h.catalog.targets))
        await assessment_entered.wait()
        await traffic_entered.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else CheckpointError):
            await task
        assert not active and {"traffic", "assessment"} <= set(finalized)
        assert all(task.done() for task in asyncio.all_tasks() if task is not asyncio.current_task())
        assert not metrics.summary("cancelled")["active"]
        assert h.store.run("trial").read("quality-result", missing_ok=True) is None
        runner.logger.close()
    with h.store.ownership():
        asyncio.run(run())
    with h.store.ownership():
        assert not active


def evidence_harness(tmp_path, *, ready=9, grace=30, hydration=120):
    h = fake.Harness(tmp_path, issues=0)
    h.settings = replace(h.settings, hydration_seconds=hydration, daily_evidence_grace_seconds=grace)
    h.cloud.ready_count = 10
    query = h.cloud.query
    h.observation_times = []
    async def observe(query_text, **kwargs):
        if "project" in query_text:
            h.observation_times.append(h.clock.elapsed)
        result = await query(query_text, **kwargs)
        allowed = 10 if getattr(h, "late_at", float("inf")) <= h.clock.elapsed else ready
        ids = {
            "response-" + item[2] for item in h.cloud.invocations
            if item[1].phase == "setup" or int(item[1].body["input"].split()[-1]) <= allowed
        }
        return replace(result, records=tuple(
            row for row in result.records if row["customDimensions"]["gen_ai.response.id"] in ids
        ))
    h.cloud.query = observe
    return h


@pytest.mark.parametrize("ready,late,grace,hydration,elapsed,starts", [
    (9, 12, 30, 120, 12, 1),
    (9, None, 30, 120, 30, 1),
    (6, None, 30, 120, 30, 1),
    (5, None, 30, 120, 120, 0),
    (10, None, 30, 120, 0, 1),
    (9, None, 30, 7, 7, 1),
    (9, None, 0, 120, 0, 1),
])
def test_daily_evidence_grace_is_bounded_and_not_ten_response_gate(
    tmp_path, ready, late, grace, hydration, elapsed, starts,
):
    h = evidence_harness(tmp_path, ready=ready, grace=grace, hydration=hydration)
    if late is not None:
        h.late_at = late
    h.daily()
    assert h.clock.elapsed == elapsed
    assert len(h.cloud.starts) == starts and len(h.cloud.invocations) == 20
    records = h.store.run("trial")
    work = records.read(f"targets/{h.catalog.targets[0].key}/source")["work_key"]
    if starts:
        insight = records.read_completed(work + "/insights")
        snapshot = records.read_artifact(insight["visible_snapshot"])
        assert snapshot["observed_at"] <= insight["started_at"]
        from agent_insights_quality.telemetry import Snapshot
        probe_ids = {"response-" + item[2] for item in h.cloud.invocations if item[1].phase == "probe"}
        assert len(probe_ids & Snapshot.from_private_dict(snapshot).attributable_responses) == (
            10 if late is not None else ready
        )
    if ready == 10:
        assert records.read_completed(work + "/evidence/grace-deadline", missing_ok=True) is None
        assert len([event for event in h.cloud.events if event[0] == "query"]) == 2


def test_grace_resume_keeps_original_deadline_and_queries_mature_evidence_without_sleep(tmp_path):
    h = evidence_harness(tmp_path)
    original_sleep = h.clock.sleep
    async def interrupt(delay):
        raise OSError("synthetic interruption during observation")
    h.clock.sleep = interrupt
    with pytest.raises(OSError):
        h.daily()
    records = h.store.run("trial")
    work = records.read(f"targets/{h.catalog.targets[0].key}/source")["work_key"]
    frozen = records.read_completed(work + "/evidence/grace-deadline")
    old_requests = [item[2] for item in h.cloud.invocations]
    h.clock.value += timedelta(seconds=40)
    h.clock.elapsed += 40
    h.clock.sleep = original_sleep
    h.daily()
    assert records.read_completed(work + "/evidence/grace-deadline") == frozen
    assert h.clock.waits == []
    assert [item[2] for item in h.cloud.invocations] == old_requests
    assert len(h.cloud.starts) == 1


def test_grace_query_failures_do_not_extend_deadline_or_fall_back_to_success(tmp_path):
    h = evidence_harness(tmp_path, grace=3)
    query = h.cloud.query
    async def fail(query_text, **kwargs):
        if h.clock.elapsed:
            raise QualityError("synthetic_query_failure", retryable=True)
        return await query(query_text, **kwargs)
    h.cloud.query = fail
    assert h.daily().score is None
    assert h.clock.elapsed == 3
    assert not h.cloud.starts and not h.sol.calls and len(h.cloud.invocations) == 20


def test_snapshot_grace_checkpoint_crash_does_not_start_new_grace_on_resume(tmp_path, monkeypatch):
    h = evidence_harness(tmp_path)
    save = RecordStore.save_completed
    def fail(records, key, value):
        if key.endswith("/grace-deadline"):
            raise CheckpointError()
        save(records, key, value)
    monkeypatch.setattr(RecordStore, "save_completed", fail)
    with pytest.raises(CheckpointError):
        h.daily()
    records = h.store.run("trial")
    work = records.read(f"targets/{h.catalog.targets[0].key}/source")["work_key"]
    prior = records.read_artifact(records.read(work + "/evidence")["artifact"])
    monkeypatch.setattr(RecordStore, "save_completed", save)
    h.clock.value += timedelta(seconds=40)
    h.clock.elapsed += 40
    h.daily()
    assert h.clock.waits == []
    assert records.read_completed(work + "/evidence/grace-deadline")["until"] == (
        fake.datetime.fromisoformat(prior["observed_at"]) + timedelta(seconds=30)
    ).isoformat()
    assert len(h.cloud.invocations) == 20 and len(h.cloud.starts) == 1


def test_complete_probe_roots_need_neither_setup_roots_nor_child_trees(tmp_path):
    h = fake.Harness(tmp_path, issues=0)
    h.settings = replace(h.settings, hydration_seconds=120)
    query = h.cloud.query
    async def probes_only(query_text, **kwargs):
        result = await query(query_text, **kwargs)
        probes = {"response-" + item[2] for item in h.cloud.invocations if item[1].phase == "probe"}
        return replace(result, records=tuple(
            row for row in result.records if row["customDimensions"]["gen_ai.response.id"] in probes
        ))
    h.cloud.query = probes_only
    h.daily()
    assert h.clock.waits == [] and len(h.cloud.starts) == 1
