import asyncio
import json
import threading
from collections import Counter
from dataclasses import asdict, replace

import pytest

from agent_insights_quality.contracts import Invocation
from agent_insights_quality.errors import QualityError
from agent_insights_quality.performance import RunMetrics
from agent_insights_quality.providers import AzureHttpTransport, AzureRuntime, HttpResponse
from agent_insights_quality.results import UnitId
from agent_insights_quality.state import CheckpointError, RecordStore, RuntimeStore, StateError
import test_runner as fake


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    fake.fake_storage(monkeypatch)


def harness(path, *, budget=10, ahead=1, profile="daily", hosted=True, issues=0, agent="travel-agent"):
    h = fake.Harness(path, profile=profile, hosted=hosted, issues=issues,
                     daily_attempt_budget=budget, daily_travel_session_lookahead=ahead)
    h.catalog = replace(h.catalog, agents=(agent,), targets=tuple(
        replace(target, unit_id=UnitId(agent, target.unit_id.logical_version))
        for target in h.catalog.targets
    ))
    return h


def work_key(h, target, run_id="trial"):
    return h.store.run(run_id).read(f"targets/{target.key}/source")["work_key"]


def session_index(h, deployment, request_id, *, run_id="trial"):
    records = h.store.run(run_id)
    target = next(t for t in h.catalog.targets if t.key == deployment.target_key)
    key = work_key(h, target, run_id)
    matches = []
    for index in range(1, 11):
        value = records.read(key + f"/traffic/attempt-{index:02d}/session", missing_ok=True)
        if value and value["request_id"] == request_id:
            assert value["status"] == "submitting"
            matches.append(index)
    assert len(matches) == 1
    return matches[0]


async def traffic(h, runner):
    target = h.catalog.targets[0]
    work = runner._binding(target)
    attempts = runner._plan(target, work)
    deployment = await runner._deployment(target, work)
    return await runner._traffic(target, work, deployment, attempts)


def run_traffic(h, run_id="trial", *, metrics=None, **kwargs):
    with h.store.ownership():
        runner = h.runner(run_id, metrics=metrics, **kwargs)
        runner.initialize(h.catalog.targets, fake.DAY, kind=h.store.environment)
        try:
            return asyncio.run(asyncio.wait_for(traffic(h, runner), 5))
        finally:
            runner.logger.close()


@pytest.mark.parametrize("budget", [1, 2, 10])
def test_next_session_overlaps_business_without_parallel_business_or_version_advance(tmp_path, budget):
    h = harness(tmp_path, budget=budget, issues=1)
    native = h.cloud.create_session
    invoke = h.cloud.invoke
    activate = h.cloud.activate
    sessions, active, calls = {}, set(), []
    overlaps = []
    completed = Counter()
    ready_unfinished = set()
    async def execute():
        metrics = RunMetrics(h.store.run("trial"), monotonic=h.clock.monotonic)
        runner = h.runner(metrics=metrics)
        runner.initialize(h.catalog.targets, fake.DAY, kind="daily")
        async def create(deployment, request_id, persist):
            index = session_index(h, deployment, request_id)
            target = next(t for t in h.catalog.targets if t.key == deployment.target_key)
            key = work_key(h, target)
            affinity = h.store.run("trial").read_completed(key + f"/traffic/attempt-{index:02d}/session-affinity")
            assert affinity["provider_version"] == deployment.provider_version
            assert affinity["target_key"] == deployment.target_key
            assert h.cloud.active[deployment.agent_name] == deployment
            calls.append((deployment.target_key, index, request_id))
            if index > 1 and active:
                overlaps.append((deployment.target_key, index))
                assert active == {(deployment.target_key, index - 1)}
            assert len(ready_unfinished) <= 1
            value = await native(deployment, request_id, persist)
            sessions[deployment.target_key, index] = value
            ready_unfinished.add((deployment.target_key, index))
            await h.clock.sleep(1)
            return value
        async def business(deployment, step, **kwargs):
            index = int(step.body["input"].split()[-1])
            binding = deployment.target_key, index
            assert not active
            assert kwargs["session_id"] == sessions[binding]
            assert kwargs["previous_response_id"] is None
            assert completed[deployment.target_key] >= (index - 1) * 2
            active.add(binding)
            try:
                await h.clock.sleep(2)
                result = await invoke(deployment, step, **kwargs)
                completed[deployment.target_key] += 1
                if step.phase == "probe":
                    ready_unfinished.remove(binding)
                return result
            finally:
                active.remove(binding)
        async def activation(deployment):
            previous = h.cloud.active.get(deployment.agent_name)
            if previous:
                assert not active and not ready_unfinished
                assert completed[previous.target_key] == 20
                target = next(t for t in h.catalog.targets if t.key == previous.target_key)
                key = work_key(h, target)
                assert h.store.run("trial").read_completed(key + "/traffic-done")
                insight = h.store.run("trial").read_completed(key + "/insights")
                assert h.store.run("trial").read_artifact(key + "/insights/after")["cards"] == insight["after"]
                assert h.store.run("trial").read_artifact(insight["visible_snapshot"])
            await activate(deployment)
        h.cloud.create_session, h.cloud.invoke, h.cloud.activate = create, business, activation
        result = await asyncio.wait_for(runner.run_daily(h.catalog.targets), 5)
        assert result.score == 100
        assert bool(overlaps) == (budget > 1)
        assert metrics.peak["execution:daily_attempt"] == min(budget, 2)
        assert metrics.peak["port_call:invoke"] == 1
        assert runner.attempt_limit._value == budget
        assert not metrics.summary("completed")["active"]
        phases = [r for r in metrics.observations if r["kind"] == "attempt_phase"]
        assert Counter(r["name"] for r in phases) == {"session_preparation": 20, "business_execution": 20}
        assert all(r["unit"].startswith(r["lane"] + "/") and r["attempt"] in range(1, 11) for r in phases)
        assert any(r["elapsed_seconds"] > 0 for r in phases if r["name"] == "session_preparation")
        assert "not additive" in metrics.summary("completed")["semantics"]["session_lookahead"]
        runner.logger.close()
    with h.store.ownership():
        asyncio.run(execute())
    assert len(calls) == len(set(sessions.values())) == 20
    assert not active and not ready_unfinished
    for target in h.catalog.targets:
        assert [index for key, index, _ in calls if key == target.key] == list(range(1, 11))
        assert [item[1].phase for item in h.cloud.invocations if item[0].target_key == target.key] == ["setup", "probe"] * 10


def test_prepared_work_uses_shared_ten_slot_budget_with_other_agent_lanes(tmp_path):
    h = fake.Harness(tmp_path, agents=5, issues=0, hosted=True, daily_travel_session_lookahead=1)
    first = h.catalog.agents[0]
    h.catalog = replace(h.catalog, agents=("travel-agent", *h.catalog.agents[1:]), targets=tuple(
        replace(t, unit_id=UnitId("travel-agent", t.unit_id.logical_version))
        if t.unit_id.agent == first else t for t in h.catalog.targets
    ))
    async def run():
        metrics = RunMetrics(h.store.run("trial"))
        runner = h.runner(metrics=metrics)
        runner.initialize(h.catalog.targets, fake.DAY, kind="daily")
        await runner.run_daily(h.catalog.targets)
        assert metrics.peak["execution:daily_attempt"] == 10
        assert metrics.peak["port_call:invoke"] <= 10
        assert runner.attempt_limit._value == 10
        runner.logger.close()
    with h.store.ownership():
        asyncio.run(run())
    assert len(h.cloud.invocations) == 100


@pytest.mark.parametrize("profile,hosted,agent,ahead", [
    ("staging", True, "travel-agent", 1),
    ("daily", False, "travel-agent", 1),
    ("daily", True, "finance-agent", 1),
    ("daily", True, "travel-agent", 0),
])
def test_ineligible_paths_have_no_lookahead_phases_or_affinity_records(tmp_path, profile, hosted, agent, ahead):
    h = harness(tmp_path, profile=profile, hosted=hosted, agent=agent, ahead=ahead)
    with h.store.ownership():
        metrics = RunMetrics(h.store.run("trial"))
    values = run_traffic(h, metrics=metrics)
    assert len(values) == 20
    assert not any(r["kind"] == "attempt_phase" for r in metrics.observations)
    assert not list(h.store.run("trial").directory.rglob("session-affinity.json"))
    if agent == "travel-agent":
        assert [item[1].phase for item in h.cloud.invocations] == ["setup", "probe"] * 10
    assert len([e for e in h.cloud.events if e[0] == "session"]) == (10 if hosted else 0)


def test_completed_traffic_and_run_policy_resume_without_any_new_session(tmp_path):
    h = harness(tmp_path, issues=1)
    h.daily(test_run=True, rerun=5, fresh_traffic=True)
    records = h.store.run("trial")
    original = records.read_completed("run")
    counts = len(h.cloud.invocations), len([e for e in h.cloud.events if e[0] == "session"])
    h.settings = replace(h.settings, daily_travel_session_lookahead=0)
    h.daily(test_run=True, rerun=5)
    assert (len(h.cloud.invocations), len([e for e in h.cloud.events if e[0] == "session"])) == counts
    assert records.read_completed("daily-session-lookahead") == {"travel_sessions_ahead": 1}
    assert records.read_completed("run") == original
    assert records.read("source")["settings"]["daily_travel_session_lookahead"] == 1


def test_serial_and_lookahead_keep_identical_business_bodies_bindings_and_outcomes(tmp_path):
    outcomes = []
    for ahead in (0, 1):
        h = harness(tmp_path / str(ahead), ahead=ahead)
        receipts = run_traffic(h)
        sessions, calls = {}, {}
        for deployment, step, request_id, session_id, previous in h.cloud.invocations:
            index = int(step.body["input"].split()[-1])
            key = index, step.step_id
            receipt = receipts[key]
            assert (receipt.request_id, receipt.session_id) == (request_id, session_id)
            assert previous is None
            assert sessions.setdefault(index, session_id) == session_id
            assert key not in calls
            calls[key] = (
                deployment.provider_version, asdict(step), receipt.status,
                receipt.response["output"], receipt.http_status,
            )
        assert len(calls) == 20 and len(set(sessions.values())) == 10
        assert len([e for e in h.cloud.events if e[0] == "session"]) == 10
        outcomes.append(calls)
    assert outcomes[0] == outcomes[1]


def test_policy_checkpoint_failure_prevents_all_provider_effects(tmp_path, monkeypatch):
    h = harness(tmp_path)
    save = RecordStore.save_completed
    def fail(records, key, value):
        if key == "daily-session-lookahead":
            raise CheckpointError()
        save(records, key, value)
    monkeypatch.setattr(RecordStore, "save_completed", fail)
    with h.store.ownership():
        runner = h.runner()
        try:
            with pytest.raises(CheckpointError):
                runner.initialize(h.catalog.targets, fake.DAY, kind="daily")
            assert runner._stopped and not runner._initialized
            assert not h.cloud.events
        finally:
            runner.logger.close()


def test_legacy_run_stays_off_and_corrupt_frozen_policy_fails_closed(tmp_path):
    h = harness(tmp_path, ahead=0)
    run_traffic(h)
    path = h.store.run("trial")._path("completed", "daily-session-lookahead")
    path.unlink()
    h.settings = replace(h.settings, daily_travel_session_lookahead=1)
    run_traffic(h)
    assert h.store.run("trial").read_completed("daily-session-lookahead") == {"travel_sessions_ahead": 0}
    assert not list(h.store.run("trial").directory.rglob("session-affinity.json"))
    path.write_text('{"travel_sessions_ahead":true}')
    with pytest.raises(StateError, match="session_lookahead_policy_invalid"):
        run_traffic(h)


@pytest.mark.parametrize("prepared_state", ["ready", "pending"])
def test_cancel_after_preparation_reuses_session_and_completed_turn(tmp_path, prepared_state):
    h = harness(tmp_path)
    original_session, original_invoke = h.cloud.create_session, h.cloud.invoke
    posted, preserved = [], {}
    async def run():
        second_ready = asyncio.Event()
        async def create(deployment, request_id, persist):
            index = session_index(h, deployment, request_id)
            posted.append(request_id)
            value = await original_session(deployment, request_id, persist)
            if index == 2:
                second_ready.set()
                if prepared_state == "pending":
                    await asyncio.Event().wait()
            return value
        async def invoke(deployment, step, **kwargs):
            receipt = await original_invoke(deployment, step, **kwargs)
            if step.body["input"] == "synthetic setup 1":
                preserved["receipt"] = receipt
                await asyncio.Event().wait()
            return receipt
        h.cloud.create_session, h.cloud.invoke = create, invoke
        runner = h.runner()
        runner.initialize(h.catalog.targets, fake.DAY, kind="daily")
        existing_tasks = set(asyncio.all_tasks())
        task = asyncio.create_task(traffic(h, runner))
        await second_ready.wait()
        key = work_key(h, h.catalog.targets[0])
        preserved["session"] = h.store.run("trial").read(key + "/traffic/attempt-02/session")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runner.attempt_limit._value == 10
        assert asyncio.all_tasks() <= existing_tasks
        runner.logger.close()
    with h.store.ownership():
        asyncio.run(asyncio.wait_for(run(), 5))
    h.cloud.create_session, h.cloud.invoke = original_session, original_invoke
    with h.store.ownership():
        metrics = RunMetrics(h.store.run("trial"))
    assert len(run_traffic(h, metrics=metrics)) == 20
    assert len(h.cloud.invocations) == (20 if prepared_state == "ready" else 18)
    assert sum(item[2] == preserved["receipt"].request_id for item in h.cloud.invocations) == 1
    assert len([e for e in h.cloud.events if e[0] == "session"]) == 10
    key = work_key(h, h.catalog.targets[0])
    assert h.store.run("trial").read(key + "/traffic/attempt-02/session") == preserved["session"]
    reused = [o for o in metrics.observations if o["kind"] == "attempt_phase"
              and o["name"] == "session_preparation" and o["attempt"] in (1, 2) and o["status"] == "reused"]
    assert len(reused) == (2 if prepared_state == "ready" else 1)
    assert all(o["elapsed_seconds"] is None for o in reused)


def test_budget_one_cancels_waiting_prefetch_without_creating_a_session(tmp_path):
    h = harness(tmp_path, budget=1)
    original = h.cloud.invoke
    async def run():
        started = asyncio.Event()
        async def invoke(deployment, step, **kwargs):
            receipt = await original(deployment, step, **kwargs)
            started.set()
            await asyncio.Event().wait()
            return receipt
        h.cloud.invoke = invoke
        metrics = RunMetrics(h.store.run("trial"))
        runner = h.runner(metrics=metrics)
        runner.initialize(h.catalog.targets, fake.DAY, kind="daily")
        existing = set(asyncio.all_tasks())
        task = asyncio.create_task(traffic(h, runner))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runner.attempt_limit._value == 1 and asyncio.all_tasks() <= existing
        assert not metrics.summary("cancelled")["active"]
        runner.logger.close()
    with h.store.ownership():
        asyncio.run(asyncio.wait_for(run(), 5))
    assert len([e for e in h.cloud.events if e[0] == "session"]) == 1
    key = work_key(h, h.catalog.targets[0])
    assert h.store.run("trial").read(key + "/traffic/attempt-02/session", missing_ok=True) is None
    h.cloud.invoke = original
    assert len(run_traffic(h)) == 20
    assert len(h.cloud.invocations) == 20
    assert len([e for e in h.cloud.events if e[0] == "session"]) == 10


@pytest.mark.parametrize("accepted", [None, True, False])
def test_ambiguous_or_rejected_preparation_is_checkpointed_and_not_blindly_reposted(tmp_path, accepted):
    h = harness(tmp_path)
    original = h.cloud.create_session
    posted = []
    async def create(deployment, request_id, persist):
        index = session_index(h, deployment, request_id)
        if index == 2:
            posted.append(request_id)
            if accepted is not False:
                persist("synthetic-pending-session")
            raise QualityError("session_pending", retryable=True, request_accepted=accepted)
        return await original(deployment, request_id, persist)
    h.cloud.create_session = create
    values = run_traffic(h)
    assert values[2, "setup"].status == values[2, "probe"].status == "blocked"
    assert len(h.cloud.invocations) == 18
    attempts = len(posted)
    assert attempts == (4 if accepted is False else 1)
    assert len(set(posted)) == 1
    run_traffic(h)
    assert len(posted) == attempts and len(h.cloud.invocations) == 18
    key = work_key(h, h.catalog.targets[0])
    value = h.store.run("trial").read(key + "/traffic/attempt-02/session")
    assert value["request_id"] == posted[0]
    if accepted is not False:
        assert value["status"] == "unknown" and value["session_id"] == "synthetic-pending-session"


def test_unknown_business_stops_later_turns_and_bounds_unused_prepared_sessions(tmp_path):
    h = harness(tmp_path, issues=1)
    original = h.cloud.create_session
    sessions, invocations = [], []
    async def run():
        second_ready = asyncio.Event()
        async def create(deployment, request_id, persist):
            index = session_index(h, deployment, request_id)
            sessions.append(index)
            result = await original(deployment, request_id, persist)
            if index == 2:
                second_ready.set()
            return result
        async def invoke(deployment, step, **kwargs):
            invocations.append((deployment.target_key, step.step_id, kwargs["request_id"]))
            await second_ready.wait()
            persist = kwargs["persist"]
            persist(Invocation(
                kwargs["request_id"], "synthetic-uncertain-response", kwargs["session_id"],
                h.clock.now().isoformat(), h.clock.now().isoformat(), "unknown",
                {"output": "synthetic uncertain result"},
            ))
            raise QualityError("synthetic_unknown", request_accepted=None)
        h.cloud.create_session, h.cloud.invoke = create, invoke
        runner = h.runner()
        runner.initialize(h.catalog.targets, fake.DAY, kind="daily")
        result = await runner.run_daily(h.catalog.targets)
        assert result.score is None
        runner.logger.close()
    with h.store.ownership():
        asyncio.run(asyncio.wait_for(run(), 5))
    assert sessions == [1, 2] and len(invocations) == 1
    assert not h.cloud.starts
    assert not any(e == ("activate", h.catalog.targets[1].key) for e in h.cloud.events)
    h.daily()
    assert sessions == [1, 2] and len(invocations) == 1
    key = work_key(h, h.catalog.targets[0])
    assert h.store.run("trial").read(key + "/traffic/attempt-01/setup")["status"] == "unknown"
    for index in range(1, 11):
        assert h.store.run("trial").read(key + f"/traffic/attempt-{index:02d}/probe")["status"] == "blocked"


@pytest.mark.parametrize("failure", ["cancel", "affinity", "session_ready"])
def test_prefetch_cancellation_or_fatal_checkpoint_drains_both_workers_before_unlock(tmp_path, monkeypatch, failure):
    h = harness(tmp_path)
    original = h.cloud.create_session
    active, drained = set(), set()
    async def run():
        prep_started = asyncio.Event()
        async def invoke(deployment, step, **kwargs):
            active.add("business")
            try:
                await asyncio.Event().wait()
            finally:
                active.remove("business")
                drained.add("business")
        async def create(deployment, request_id, persist):
            index = session_index(h, deployment, request_id)
            if index == 2:
                assert "business" in active
                active.add("preparation")
                try:
                    result = await original(deployment, request_id, persist)
                    prep_started.set()
                    if failure == "cancel":
                        await asyncio.Event().wait()
                    return result
                finally:
                    active.remove("preparation")
                    drained.add("preparation")
            return await original(deployment, request_id, persist)
        save = RecordStore.save_completed
        def fail(records, key, value):
            if "/attempt-02/" in key and (
                failure == "affinity" and key.endswith("/session-affinity")
                or failure == "session_ready" and key.endswith("/session")
            ):
                assert "business" in active
                raise CheckpointError()
            save(records, key, value)
        monkeypatch.setattr(RecordStore, "save_completed", fail)
        h.cloud.invoke, h.cloud.create_session = invoke, create
        metrics = RunMetrics(h.store.run("trial"))
        runner = h.runner(metrics=metrics)
        runner.initialize(h.catalog.targets, fake.DAY, kind="daily")
        existing_tasks = set(asyncio.all_tasks())
        task = asyncio.create_task(runner.run_daily(h.catalog.targets))
        if failure == "cancel":
            await prep_started.wait()
            task.cancel()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else CheckpointError):
            await task
        assert not active and "business" in drained
        if failure != "affinity":
            assert "preparation" in drained
        assert runner.attempt_limit._value == 10
        assert not metrics.summary("failed")["active"]
        assert asyncio.all_tasks() <= existing_tasks
        runner.logger.close()
    with h.store.ownership():
        asyncio.run(asyncio.wait_for(run(), 5))
    with h.store.ownership():
        assert not active and not h.cloud.starts


@pytest.mark.parametrize("failure", ["cancel", "checkpoint"])
@pytest.mark.parametrize("outcome", [201, 202, 429, "no_response"])
def test_native_session_thread_holds_permit_and_ownership_until_drained(
    tmp_path, monkeypatch, failure, outcome,
):
    h = harness(tmp_path)
    original_session, original_invoke = h.cloud.create_session, h.cloud.invoke
    release, finished = threading.Event(), threading.Event()
    transport = AzureHttpTransport()
    native = AzureRuntime(
        h.cloud.environment, transport=transport, logs=h.cloud, clock=h.clock.now, sleep=h.clock.sleep,
    )
    posts, session_attempts, holder = [], [], {}

    async def exercise():
        entered, business_started, finish_business = asyncio.Event(), asyncio.Event(), asyncio.Event()
        checkpoint_failed, ownership_released = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        def blocking(request):
            assert request.method == "POST" and "/endpoint/sessions?" in request.url
            posts.append(request)
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(timeout=5), "Session POST worker was not released"
                if outcome == "no_response":
                    raise OSError("Synthetic lost session response")
                return HttpResponse(outcome, {}, json.dumps({
                    "id": "synthetic-unpersisted-session",
                    "version_indicator": json.loads(request.body)["version_indicator"],
                }).encode())
            finally:
                finished.set()
        monkeypatch.setattr(transport, "_send", blocking)
        async def create(deployment, request_id, persist):
            index = session_index(h, deployment, request_id)
            session_attempts.append(index)
            assert index in (1, 2), "Cancellation authorized another session"
            if index == 2:
                return await native.create_session(deployment, request_id, persist)
            return await original_session(deployment, request_id, persist)
        async def business(deployment, step, **kwargs):
            receipt = await original_invoke(deployment, step, **kwargs)
            assert step.body["input"] == "synthetic setup 1"
            business_started.set()
            await finish_business.wait()
            return receipt
        save = RecordStore.save_completed
        def fail(records, key, value):
            if failure == "checkpoint" and key.endswith("/attempt-01/setup"):
                checkpoint_failed.set()
                raise CheckpointError()
            save(records, key, value)
        monkeypatch.setattr(RecordStore, "save_completed", fail)
        h.cloud.create_session, h.cloud.invoke = create, business
        async def owned():
            try:
                with h.store.ownership():
                    metrics = RunMetrics(h.store.run("trial"))
                    runner = h.runner(metrics=metrics)
                    holder.update(runner=runner, metrics=metrics)
                    runner.initialize(h.catalog.targets, fake.DAY, kind="daily")
                    try:
                        await runner.run_daily(h.catalog.targets)
                    finally:
                        runner.logger.close()
            finally:
                ownership_released.set()
        existing_tasks = set(asyncio.all_tasks())
        task = asyncio.create_task(owned())
        triggered = False
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.wait_for(business_started.wait(), 5)
            if failure == "cancel":
                task.cancel()
            else:
                finish_business.set()
                await asyncio.wait_for(checkpoint_failed.wait(), 5)
            triggered = True
            for repeat in range(3):
                if failure == "cancel" and repeat:
                    task.cancel()
                for _ in range(20):
                    await asyncio.sleep(0)
                assert not task.done() and not finished.is_set()
                assert h.store._owned and not ownership_released.is_set()
                assert holder["runner"].attempt_limit._value == 9
                with pytest.raises(StateError, match="state_owned"):
                    with RuntimeStore("daily", root=h.store.root).ownership():
                        pytest.fail("Ownership released with a session POST still running")
                assert session_attempts == [1, 2] and len(posts) == len(h.cloud.invocations) == 1
        finally:
            release.set()
            if not triggered:
                task.cancel()
            settled = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(settled[0], asyncio.CancelledError if failure == "cancel" else CheckpointError)
        assert finished.is_set() and ownership_released.is_set() and not h.store._owned
        assert holder["runner"].attempt_limit._value == 10
        assert not holder["metrics"].summary("failed")["active"]
        assert asyncio.all_tasks() <= existing_tasks

    asyncio.run(exercise())
    key = work_key(h, h.catalog.targets[0]) + "/traffic/attempt-02/session"
    saved = h.store.run("trial").read(key)
    assert saved == {"request_id": posts[0].headers["x-ms-client-request-id"], "status": "submitting"}
    assert h.store.run("trial").read_completed(key, missing_ok=True) is None
    with h.store.ownership():
        runner = h.runner()
        runner.initialize(h.catalog.targets, fake.DAY, kind="daily")
        target = h.catalog.targets[0]
        try:
            with pytest.raises(QualityError, match="session_outcome_unresolved"):
                asyncio.run(runner._session(target, runner._binding(target), h.cloud.deployments[target.key], 2))
        finally:
            runner.logger.close()
    assert h.store.run("trial").read(key) == saved
    assert session_attempts == [1, 2] and len(posts) == len(h.cloud.invocations) == 1
    assert not h.cloud.starts and not h.clock.waits


@pytest.mark.parametrize("damage", ["version", "missing_session", "missing_affinity", "wrong_session"])
def test_changed_version_affinity_and_missing_completed_session_fail_before_post(tmp_path, damage):
    h = harness(tmp_path)
    run_traffic(h)
    records = h.store.run("trial")
    target = h.catalog.targets[0]
    key = work_key(h, target)
    records._path("completed", key + "/traffic-done").unlink()
    activations = len([e for e in h.cloud.events if e[0] == "activate"])
    if damage == "version":
        original = h.cloud.ensure_deployment
        async def changed(target, revision, existing, persist):
            value = await original(target, revision, existing, persist)
            return replace(value, provider_version="different-version")
        h.cloud.ensure_deployment = changed
    elif damage == "missing_session":
        for collection in ("completed", "progress"):
            path = records._path(collection, key + "/traffic/attempt-01/session")
            if path.exists():
                path.unlink()
    elif damage == "missing_affinity":
        records._path("completed", key + "/traffic/attempt-01/session-affinity").unlink()
    else:
        path = records._path("completed", key + "/traffic/attempt-01/setup")
        receipt = records.read_completed(key + "/traffic/attempt-01/setup")
        path.write_text(json.dumps({**receipt, "session_id": "different-session"}))
    with pytest.raises(StateError):
        run_traffic(h)
    if damage == "version":
        assert len([e for e in h.cloud.events if e[0] == "activate"]) == activations
    assert len([e for e in h.cloud.events if e[0] == "session"]) == 10


def test_inherited_unfinished_traffic_keeps_original_serial_preparation_path(tmp_path):
    h = harness(tmp_path, ahead=0)
    original = h.cloud.create_session
    def interrupt(deployment, request_id, persist):
        index = session_index(h, deployment, request_id)
        if index == 2:
            raise OSError("synthetic interruption before session outcome")
        persist("original-session-" + request_id)
        return "original-session-" + request_id
    h.cloud.session_hook = interrupt
    with pytest.raises(OSError):
        run_traffic(h, test_run=True, rerun=1)
    h.cloud.session_hook = None
    h.cloud.create_session = original
    h.settings = replace(h.settings, daily_travel_session_lookahead=1)
    with h.store.ownership():
        metrics = RunMetrics(h.store.run("followup"))
    result = run_traffic(h, "followup", metrics=metrics, test_run=True, rerun=2, reuse_run_id="trial")
    assert len(result) == 20 and len(h.cloud.invocations) == 18
    assert not any(o["kind"] == "attempt_phase" for o in metrics.observations)
    assert not list(h.store.run("trial").directory.rglob("session-affinity.json"))
    assert h.store.run("followup").read_completed("daily-session-lookahead") == {"travel_sessions_ahead": 1}
    assert h.store.run("followup").read(f"targets/{h.catalog.targets[0].key}/source")["traffic_run_id"] == "trial"
