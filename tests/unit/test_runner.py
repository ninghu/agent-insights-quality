import asyncio
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
import json
import re

import pytest

from agent_insights_quality.catalogs import Catalog
from agent_insights_quality.contracts import Attempt, Deployment, Environment, Invocation, QueryResult, Step, Target
from agent_insights_quality.errors import QualityError
from agent_insights_quality.results import UnitId
from agent_insights_quality.runner import Runner, choose_staging
from agent_insights_quality.selection import Selection, SourceChanges
from agent_insights_quality.settings import RuntimeSettings
from agent_insights_quality.state import CheckpointError, RuntimeStore, StateError

DAY = date(2026, 9, 4)


class MemoryLogger:
    """Operational I/O is covered by test_events, not every runner scenario."""

    health_warnings = ()

    def __init__(self, directory, **kwargs):
        self.directory = directory
        self.events = []

    def emit(self, kind, **fields):
        self.events.append({"kind": kind, "stage": fields.get("stage", "run")})
        return True

    def close(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        for name in ("runner.log", "events.jsonl"):
            with (self.directory / name).open("a") as stream:
                stream.write("".join(json.dumps(event) + "\n" for event in self.events))
        self.events.clear()


def fake_storage(monkeypatch):
    from agent_insights_quality import runner, state
    def write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    monkeypatch.setattr(state.os, "fsync", lambda _: None)
    monkeypatch.setattr(state, "_confirm_durable", lambda _: None)
    monkeypatch.setattr(state, "_atomic_write", write)
    monkeypatch.setattr(state, "_inside", lambda root, path: path)
    monkeypatch.setattr(state, "_open_snapshot", lambda path: path.open("rb"))
    monkeypatch.setattr(runner, "RunLogger", MemoryLogger)


@pytest.fixture(autouse=True)
def fake_durable_storage(monkeypatch):
    # State durability is exercised in test_state; these tests fake that boundary.
    fake_storage(monkeypatch)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 4, 12, tzinfo=UTC)
        self.elapsed = 0
        self.waits = []

    def now(self):
        return self.value

    def monotonic(self):
        return self.elapsed

    async def sleep(self, delay):
        self.waits.append(delay)
        self.elapsed += delay
        self.value += timedelta(seconds=delay)
        await asyncio.sleep(0)


def catalog(root, *, agents=1, issues=1, hosted=False):
    targets = []
    names = tuple(f"synthetic-{index}-agent" for index in range(agents))
    for number, name in enumerate(names):
        baseline = root / "agents" / name / "v0"
        for index in range(issues + 1):
            version = f"issue-{number * issues + index:03d}" if index else "v0"
            targets.append(Target(
                UnitId(name, version), "hosted_code" if hosted else "prompt",
                "model_mediated" if index else "baseline",
                baseline.parent / "issues" / version if index else baseline,
                baseline, {"root_cause": "Synthetic input contradiction"},
            ))
    return Catalog(root.resolve(), names, tuple(targets), ({}, {}))


def attempts(target):
    return tuple(Attempt(index, (
        Step("setup", "setup", {"input": f"synthetic setup {index}"}, {"status": 200}),
        Step("probe", "probe", {"input": f"synthetic probe {index}"}, {"status": 200}),
    ), {"case": index}) for index in range(1, 11))


class Registry:
    def __init__(self):
        self.records = {}

    def get(self, key):
        return self.records.get(key)

    async def save(self, value):
        self.records[value.target_key] = value


class Cloud:
    def __init__(self, clock, profile="daily"):
        self.clock = clock
        self.environment = Environment(
            profile, "synthetic", "synthetic", "https://synthetic.invalid",
            "/synthetic/telemetry", "syntheticstore", "syntheticregistry",
            "swedencentral", "Sweden Central",
        )
        self.events, self.rows, self.invocations = [], [], []
        self.deployments, self.active, self.jobs, self.cards = {}, {}, {}, {}
        self.successful_windows = {}
        self.resets = Counter()
        self.ready_count = 10
        self.query_complete = True
        self.deployment_calls = Counter()
        self.active_deploys = self.max_deploys = 0
        self.pending_deployments = 0
        self.invoke_hook = self.session_hook = self.start_hook = self.reset_hook = None
        self.poll_status = "succeeded"
        self.starts = []

    async def ensure_deployment(self, target, revision, existing, persist):
        self.events.append(("deployment", target.key))
        self.deployment_calls[target.key] += 1
        self.active_deploys += 1
        self.max_deploys = max(self.max_deploys, self.active_deploys)
        await asyncio.sleep(0)
        self.active_deploys -= 1
        deployment = Deployment(
            target.key, target.runtime_name(self.environment.profile),
            str(len(self.deployments) + 1), target.agent_type, revision,
            {"provisioning_state": "active"},
        )
        if existing and existing.source_revision == revision:
            deployment = replace(existing, details={"provisioning_state": "active"})
        self.deployments[target.key] = deployment
        persist(deployment)
        if self.pending_deployments:
            self.pending_deployments -= 1
            raise QualityError("deployment_pending", retryable=True, request_accepted=True)
        return deployment

    async def activate(self, deployment):
        self.active[deployment.agent_name] = deployment
        self.events.append(("activate", deployment.target_key))

    async def create_session(self, deployment, request_id, persist):
        self.events.append(("session", deployment.target_key))
        if self.session_hook:
            return self.session_hook(deployment, request_id, persist)
        persist("session-" + request_id)
        return "session-" + request_id

    async def invoke(self, deployment, step, *, request_id, session_id, previous_response_id, persist):
        self.clock.value += timedelta(microseconds=100)
        self.events.append(("invoke", deployment.target_key, step.step_id))
        self.invocations.append((deployment, step, request_id, session_id, previous_response_id))
        persist(Invocation(request_id, None, session_id, self.clock.now().isoformat(), "", "submitting"))
        if self.invoke_hook:
            value = self.invoke_hook(deployment, step, request_id, session_id, persist)
            if value is not None:
                return value
        response = "response-" + request_id
        receipt = Invocation(
            request_id, response, session_id, self.clock.now().isoformat(),
            self.clock.now().isoformat(), "completed", {"id": response, "output": "synthetic output"}, 200,
        )
        index = int(step.body["input"].split()[-1])
        if index <= self.ready_count:
            self.rows.append({
                "telemetry_table": "requests", "operation_Id": "operation-" + deployment.target_key,
                "id": response, "timestamp": self.clock.now().isoformat(),
                "customDimensions": {
                    "gen_ai.response.id": response, "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": deployment.agent_name, "gen_ai.agent.version": deployment.provider_version,
                },
            })
        persist(receipt)
        await asyncio.sleep(0)
        return receipt

    async def query(self, query, *, start, end):
        self.events.append(("query",))
        refs = re.findall(r"'([^']+)'", query)
        rows = [row for row in self.rows if (
            row["customDimensions"]["gen_ai.response.id"] in refs
            or row["operation_Id"] in refs
        )]
        return QueryResult(tuple(rows), self.query_complete)

    async def ensure_monitor(self, name):
        self.events.append(("monitor", name))
        self.cards.setdefault(name, [{"id": "historical", "links": ["old-synthetic-response"]}])
        return name

    async def reset_monitor(self, monitor):
        self.events.append(("reset", monitor))
        self.resets[monitor] += 1
        if self.reset_hook:
            self.reset_hook()

    async def list_insights(self, monitor):
        self.events.append(("cards", monitor))
        return tuple(deepcopy(self.cards[monitor]))

    async def start_insights(self, monitor, lookback_hours, operation_id, persist):
        self.starts.append((monitor, lookback_hours, operation_id))
        target = self.active[monitor].target_key
        self.events.append(("start", target))
        pending = {
            "monitor_id": monitor, "operation_id": operation_id,
            "request_body": {"lookback_hours": lookback_hours}, "submission_state": "submitting",
        }
        persist(pending)
        start = self.clock.now() - timedelta(hours=lookback_hours)
        if monitor in self.successful_windows:
            start = max(start, self.successful_windows[monitor])
        value = self.jobs.setdefault(operation_id, {
            "id": "job-" + operation_id, "status": "running", "target": target,
            "window_start": start.isoformat(),
            "window_end": self.clock.now().isoformat(),
        })
        if self.start_hook:
            self.start_hook(value, persist)
        persist(value)
        return value

    async def get_insights_run(self, monitor, run_id):
        job = next(job for job in self.jobs.values() if job["id"] == run_id)
        self.events.append(("poll", job["target"]))
        if self.poll_status == "succeeded" and not job.get("reported"):
            self.successful_windows[monitor] = datetime.fromisoformat(job["window_end"])
            if not job["target"].endswith("/v0"):
                self.cards[monitor].append({
                    "id": job["target"], "title": "Synthetic expected defect",
                    "links": ["old-synthetic-response", "current-synthetic-response"],
                })
            job["reported"] = True
        return {
            "id": run_id, "status": self.poll_status,
            "window_start": job["window_start"], "window_end": job["window_end"],
        }


class Sol:
    def __init__(self, cloud):
        self.cloud = cloud
        self.calls = []
        self.fail = False

    async def complete_json(self, *, instructions, payload, schema):
        self.calls.append(deepcopy(payload))
        self.cloud.events.append(("assessment", payload["target"]["unit_id"]))
        if self.fail:
            raise QualityError("synthetic_sol_failure")
        judgments = []
        for attempt in payload["attempts"]:
            step = next(step for step in attempt["steps"] if step["phase"] == "probe")
            refs = step["allowed_citation_refs"]
            sufficient = step["endpoint_ref"] is not None and len(refs) > 1
            judgment = {
                "index": attempt["index"], "sufficient": sufficient, "observed": sufficient,
                "citations": [{"attempt": attempt["index"], "step_id": step["step_id"], "refs": refs}]
                if sufficient else [], "reason": "Synthetic independent evidence",
            }
            if "cards" not in payload:
                judgment["contract_violation"] = False
            judgments.append(judgment)
        value = {"attempts": judgments}
        if "cards" not in payload:
            value["additional_findings"] = []
        if "cards" in payload:
            citation = next((item["citations"] for item in judgments if item["sufficient"]), [])
            value.update(cards=[{
                "card_alias": card["card_alias"], "core": "correct" if card["contribution"] == "current" else "unknown",
                "root_group": "synthetic-root" if card["contribution"] == "current" else None,
                "expected_match": card["contribution"] == "current",
                "citations": citation if card["contribution"] == "current" else [],
                "severity": "unknown", "proposed_fix": "unknown", "reason": "Synthetic comparison",
            } for card in payload["cards"]], limitations=[])
        return value


class Harness:
    def __init__(self, tmp_path, *, profile="daily", agents=1, issues=1, hosted=False, **settings):
        self.catalog = catalog(tmp_path / "repo", agents=agents, issues=issues, hosted=hosted)
        self.clock = Clock()
        self.store = RuntimeStore(profile, root=tmp_path / "private")
        self.cloud = Cloud(self.clock, profile)
        self.sol = Sol(self.cloud)
        self.registry = Registry()
        self.settings = RuntimeSettings(
            hydration_seconds=0, poll_interval_seconds=1, poll_timeout_seconds=3,
            insights_poll_timeout_seconds=3,
            retry_backoff_seconds=1, **settings,
        )

    def runner(self, run_id="trial", *, revision="source-one", changes=(), **kwargs):
        return Runner(
            self.catalog, self.store, run_id, self.cloud, self.sol, self.registry,
            settings=self.settings, revision=revision, now=self.clock.now,
            monotonic=self.clock.monotonic, sleep=self.clock.sleep,
            attempts=attempts, deployment_source=lambda target: "deployment-one",
            changes_since=lambda previous: SourceChanges(tuple(changes)),
            **kwargs,
        )

    def daily(self, run_id="trial", **kwargs):
        with self.store.ownership():
            runner = self.runner(run_id, **kwargs)
            runner.initialize(self.catalog.targets, DAY, kind="daily")
            try:
                return asyncio.run(runner.run_daily(self.catalog.targets))
            finally:
                runner.logger.close()

    def staging(self, run_id="stage", *, selections=None, **kwargs):
        selections = tuple(Selection(target, "traffic", ("missing",)) for target in self.catalog.targets) if selections is None else selections
        with self.store.ownership():
            runner = self.runner(run_id, **kwargs)
            runner.initialize(tuple(item.target for item in selections), DAY, kind="staging")
            try:
                return asyncio.run(runner.run_staging(selections))
            finally:
                runner.logger.close()


def test_end_to_end_five_lanes_baseline_first_and_pipelined_assessment(tmp_path):
    h = Harness(tmp_path, agents=5, issues=4, deployment_workers=2)
    result = h.daily()
    assert result.status.value == "Full" and result.score == 100
    assert result.counts.correct_issues == result.counts.expected_issues == 20
    assert len(h.cloud.invocations) == 25 * 20
    assert h.cloud.max_deploys == 2
    assert set(h.cloud.resets.values()) == {1}
    events = h.cloud.events
    assert max(i for i, event in enumerate(events) if event[0] == "poll") > next(
        i for i, event in enumerate(events) if event[0] == "assessment"
    )
    for agent in h.catalog.agents:
        starts = [event[1] for event in events if event[0] == "start" and event[1].startswith(agent)]
        assert starts == [target.key for target in h.catalog.for_agent(agent)]
        for left, right in zip(starts, starts[1:]):
            assert events.index(("poll", left)) < events.index(("activate", right))
    assert all(0 < start[1] < 6 / 60 for start in h.cloud.starts)
    assert all(len(call["attempts"]) == 10 for call in h.sol.calls)
    assert any(card["contribution"] == "historical" for call in h.sol.calls for card in call["cards"])
    count = len(h.cloud.invocations), len(h.cloud.starts), len(h.sol.calls)
    assert h.daily().to_dict() == result.to_dict()
    assert (len(h.cloud.invocations), len(h.cloud.starts), len(h.sol.calls)) == count


@pytest.mark.parametrize("ready,starts", [(6, 2), (5, 0)])
def test_readiness_six_distinct_probe_attempts_not_operations(tmp_path, ready, starts):
    h = Harness(tmp_path)
    h.cloud.ready_count = ready
    result = h.daily()
    assert len(h.cloud.starts) == starts
    assert result.team_report_eligible == (ready == 6)
    assert len(h.cloud.invocations) == 40


def test_daily_uses_explicit_payload_budget_without_changing_traffic(tmp_path, monkeypatch):
    from agent_insights_quality import assessment
    original = assessment.assess_daily
    budgets = []

    async def record_budget(*args, **kwargs):
        budgets.append(kwargs["max_payload_bytes"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(assessment, "assess_daily", record_budget)
    h = Harness(tmp_path, daily_assessment_max_payload_bytes=3_000_000)
    result = h.daily()
    assert result.status.value == "Full" and result.score == 100
    assert budgets == [3_000_000, 3_000_000]
    assert len(h.cloud.invocations) == 40 and len(h.cloud.starts) == 2


def test_readiness_does_not_require_setup_trace_or_success_status(tmp_path):
    h = Harness(tmp_path)
    runner = h.runner()
    target = h.catalog.targets[0]
    from agent_insights_quality.telemetry import ResponseScope, Snapshot
    receipts = {(index, "probe"): Invocation(
        str(index), str(index), None, h.clock.now().isoformat(), h.clock.now().isoformat(),
        "incomplete", {"output": "synthetic"}, 200,
    ) for index in range(1, 7)}
    snapshot = Snapshot("", "", "", (), tuple(ResponseScope(str(index), ("same-op",), ("row",), ("row",))
                                                          for index in range(1, 7)), False)
    assert runner._ready_attempts(attempts(target), receipts, snapshot) == 6
    runner.logger.close()


def test_reassess_uses_exact_old_traffic_snapshot_no_deployment_or_agent_calls(tmp_path):
    h = Harness(tmp_path, profile="staging")
    initial = h.staging()
    calls = len(h.cloud.invocations), sum(h.cloud.deployment_calls.values()), len(h.cloud.events)
    selection = tuple(Selection(target, "reassess", ("evaluation_changed",)) for target in h.catalog.targets)
    result = h.staging("reassess", selections=selection, revision="source-two",
                       changes=("src/agent_insights_quality/assessment.py",))
    assert len(h.cloud.invocations) == calls[0]
    assert sum(h.cloud.deployment_calls.values()) == calls[1]
    assert all(event[0] == "assessment" for event in h.cloud.events[calls[2]:])
    for old, new in zip(initial["results"], result["results"], strict=True):
        assert old["evidence_key"] == new["evidence_key"]
        assert new["traffic_source_revision"] == "source-one"
        assert new["source_revision"] == "source-two"
        assert old["tested_at"] == new["tested_at"]
    assert all(call["snapshot"] == h.sol.calls[index % 2]["snapshot"] for index, call in enumerate(h.sol.calls))


def test_daily_prompt_change_does_not_reassess_staging_but_does_refresh_reused_daily(tmp_path):
    path = "src/agent_insights_quality/prompts/daily.md"
    staging = Harness(tmp_path / "stage", profile="staging")
    staging.staging()
    selected = choose_staging(
        staging.catalog, staging.store, changes_since=lambda _: SourceChanges((path,)),
    )
    assert selected == ()

    daily = Harness(tmp_path / "day")
    daily.daily("old", test_run=True, rerun=1)
    original_calls = len(daily.cloud.invocations), len(daily.cloud.starts)
    previous = deepcopy(daily.sol.calls)
    daily.daily(
        "new", revision="source-two", changes=(path,), reuse_run_id="old",
        test_run=True, rerun=2,
    )
    assert (len(daily.cloud.invocations), len(daily.cloud.starts)) == original_calls
    assert len(daily.sol.calls) == len(previous) * 2
    for target in daily.catalog.targets:
        binding = daily.store.run("new").read(f"targets/{target.key}/source")
        assert binding["traffic_run_id"] == "old"
        assert binding["assessment"]["run_id"] == "new"


def test_incomplete_recollects_new_snapshot_without_resampling(tmp_path):
    h = Harness(tmp_path, profile="staging")
    h.cloud.query_complete = False
    first = h.staging()
    assert all(item["status"] == "INCOMPLETE" for item in first["results"])
    count = len(h.cloud.invocations), sum(h.cloud.deployment_calls.values())
    h.cloud.query_complete = True
    second = h.staging()
    assert (len(h.cloud.invocations), sum(h.cloud.deployment_calls.values())) == count
    assert all(item["status"] == "PASS" for item in second["results"])
    for old, new in zip(first["results"], second["results"], strict=True):
        assert old["evidence_key"] != new["evidence_key"]
        assert h.store.run("stage").read_artifact(old["evidence_key"])["query_complete"] is False


def test_daily_assessment_repair_does_not_repeat_insights_or_traffic(tmp_path):
    h = Harness(tmp_path)
    h.sol.fail = True
    assert h.daily().score is None
    count = len(h.cloud.invocations), len(h.cloud.starts)
    h.sol.fail = False
    assert h.daily().score == 100
    assert (len(h.cloud.invocations), len(h.cloud.starts)) == count


def test_private_new_rerun_reuses_unchanged_work_and_reset_with_provenance(tmp_path):
    h = Harness(tmp_path)
    h.daily(test_run=True, rerun=1)
    count = len(h.cloud.invocations), len(h.cloud.starts), len(h.sol.calls)
    h.daily("trial-two", test_run=True, rerun=2, reuse_run_id="trial", revision="source-two",
            changes=("docs/readme.md",))
    assert (len(h.cloud.invocations), len(h.cloud.starts), len(h.sol.calls)) == count
    assert set(h.cloud.resets.values()) == {1}
    binding = h.store.run("trial-two").read("targets/" + h.catalog.targets[0].key + "/source")
    assert binding["traffic_run_id"] == "trial"


def test_source_change_only_retraffics_affected_issue(tmp_path):
    h = Harness(tmp_path, profile="staging")
    h.staging()
    issue = h.catalog.targets[1]
    changed = issue.version_root.relative_to(h.catalog.root) / "definition.json"
    with h.store.ownership():
        selection = choose_staging(h.catalog, h.store, changes_since=lambda _: SourceChanges((changed,)))
    assert [item.target for item in selection] == [issue]
    before = Counter(item[0].target_key for item in h.cloud.invocations)
    h.staging("changed", selections=selection, revision="source-two", changes=(changed,))
    after = Counter(item[0].target_key for item in h.cloud.invocations)
    assert after[issue.key] - before[issue.key] == 20
    assert after[h.catalog.targets[0].key] == before[h.catalog.targets[0].key]


def test_invocation_acceptance_unknown_never_reposted_and_all_ten_attempts_retained(tmp_path):
    h = Harness(tmp_path, profile="staging")
    def unknown(deployment, step, request_id, session, persist):
        raise QualityError("synthetic_unknown", retryable=True, request_accepted=None)
    h.cloud.invoke_hook = unknown
    h.staging()
    count = len(h.cloud.invocations)
    h.staging()
    assert count == len(h.cloud.invocations) == 20
    assert len(h.sol.calls[-1]["attempts"]) == 10
    assert all(item["steps"][0]["execution"]["status"] == "unknown" for item in h.sol.calls[-1]["attempts"])


def test_session_callback_pending_preserves_id_and_does_not_assume_ready(tmp_path):
    h = Harness(tmp_path, profile="staging", hosted=True)
    def pending(deployment, request_id, persist):
        persist("pending-" + request_id)
        raise QualityError("session_pending", request_accepted=True)
    h.cloud.session_hook = pending
    h.staging()
    sessions = len([event for event in h.cloud.events if event[0] == "session"])
    h.staging()
    assert not h.cloud.invocations
    assert len([event for event in h.cloud.events if event[0] == "session"]) == sessions
    assert all(call["attempts"][0]["steps"][0]["execution"]["status"] == "blocked" for call in h.sol.calls)


def test_checkpoint_failure_aborts_siblings_not_soft_exclusion(tmp_path, monkeypatch):
    h = Harness(tmp_path, agents=2)
    from agent_insights_quality.state import RecordStore
    save = RecordStore.save_progress
    def fail(self, key, value):
        if "/attempt-" in key and value.get("status") == "completed":
            raise CheckpointError()
        return save(self, key, value)
    monkeypatch.setattr(RecordStore, "save_progress", fail)
    with pytest.raises(CheckpointError):
        h.daily()
    assert len(h.cloud.invocations) <= 2
    assert not h.cloud.starts and not h.sol.calls


def test_crash_after_response_callback_resumes_without_repeating_completed_turn(tmp_path):
    h = Harness(tmp_path, profile="staging")
    first = []
    def crash(deployment, step, request_id, session, persist):
        if not first:
            first.append(request_id)
            persist(Invocation(request_id, "response-saved", session, h.clock.now().isoformat(),
                               h.clock.now().isoformat(), "completed", {"output": "synthetic"}, 200))
            raise RuntimeError("synthetic process interruption")
    h.cloud.invoke_hook = crash
    with pytest.raises(RuntimeError, match="interruption"):
        h.staging()
    h.cloud.invoke_hook = None
    h.staging()
    assert [item[2] for item in h.cloud.invocations].count(first[0]) == 1
    assert any(item[4] == "response-saved" for item in h.cloud.invocations)


def test_native_insights_retry_keeps_exact_body_and_callback_identity(tmp_path):
    h = Harness(tmp_path)
    calls = 0
    def retry(value, persist):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise QualityError("synthetic_transport", request_accepted=None, retryable=True)
        persist(value)
        if calls == 2:
            raise QualityError("synthetic_after_callback", request_accepted=True)
    h.cloud.start_hook = retry
    assert h.daily().score == 100
    assert h.cloud.starts[0] == h.cloud.starts[1]
    assert len(h.cloud.starts) == 3


@pytest.mark.parametrize("completion_delay", [730, 800])
def test_insights_can_finish_after_deployment_wait_budget_without_new_submission(tmp_path, completion_delay):
    h = Harness(tmp_path)
    h.settings = replace(
        h.settings, poll_timeout_seconds=600,
        insights_poll_timeout_seconds=1200, poll_interval_seconds=60,
    )
    poll = h.cloud.get_insights_run
    first_poll = {}

    async def delayed(monitor, run_id):
        start = first_poll.setdefault(run_id, h.clock.elapsed)
        job = next(job for job in h.cloud.jobs.values() if job["id"] == run_id)
        is_baseline = job["target"].endswith("/v0")
        h.cloud.poll_status = (
            "running" if is_baseline and h.clock.elapsed - start < completion_delay
            else "succeeded"
        )
        return await poll(monitor, run_id)

    h.cloud.get_insights_run = delayed
    result = h.daily()
    assert result.status.value == "Full" and result.score == 100
    assert completion_delay <= h.clock.elapsed < 1200
    assert len(h.cloud.starts) == 2 and len(h.cloud.invocations) == 40
    assert set(h.cloud.resets.values()) == {1}
    target = h.catalog.targets[0]
    records = h.store.run("trial")
    work = records.read(f"targets/{target.key}/source")["work_key"]
    deployment_deadline = records.read_completed(work + "/deployment/deadline")
    insights_deadline = records.read_completed(work + "/insights/deadline")
    assert datetime.fromisoformat(insights_deadline["until"]) > datetime.fromisoformat(deployment_deadline["until"])


def test_poll_timeout_then_resume_polls_same_job_no_reset_or_new_post(tmp_path):
    h = Harness(tmp_path)
    h.cloud.poll_status = "running"
    assert h.daily().score is None
    assert sum(h.clock.waits) == 3
    first_start = h.cloud.starts[0]
    first_count = len(h.cloud.invocations)
    records = h.store.run("trial")
    work = records.read(f"targets/{h.catalog.targets[0].key}/source")["work_key"]
    original_deadline = records.read_completed(work + "/insights/deadline")
    h.settings = replace(h.settings, insights_poll_timeout_seconds=12)
    h.cloud.poll_status = "succeeded"
    assert h.daily().score == 100
    assert records.read_completed(work + "/insights/deadline") == original_deadline
    assert sum(h.clock.waits) == 3
    assert h.cloud.starts[0] == first_start and len(h.cloud.starts) == 2
    assert len(h.cloud.invocations) == first_count + 20
    assert set(h.cloud.resets.values()) == {1}


@pytest.mark.parametrize("accepted,expected_resets", [(False, 2), (None, 1), (True, 1)])
def test_reset_retries_only_definitive_rejection_and_never_between_versions(tmp_path, accepted, expected_resets):
    h = Harness(tmp_path)
    failed = False
    def reset():
        nonlocal failed
        if not failed:
            failed = True
            raise QualityError("synthetic_reset", retryable=True, request_accepted=accepted)
    h.cloud.reset_hook = reset
    result = h.daily()
    if accepted is not False:
        assert result.score is None
        assert h.daily().score is None
        assert not h.cloud.invocations
    else:
        assert result.score == 100
    assert sum(h.cloud.resets.values()) == expected_resets


def test_corrupt_checkpoint_is_fatal_and_never_recreated(tmp_path):
    h = Harness(tmp_path)
    h.daily()
    path = h.store.run("trial").directory / "progress" / "targets" / h.catalog.targets[0].key / "source.json"
    path.write_text("{broken")
    with pytest.raises(StateError, match="state_record_corrupt"):
        h.daily()


def test_retained_traffic_missing_checkpoint_is_not_new_traffic(tmp_path):
    h = Harness(tmp_path, profile="staging")
    h.staging()
    target = h.catalog.targets[0]
    binding = h.store.staging_index.read(target.key)
    path = h.store.run("stage").directory / "completed" / binding["work_key"] / "traffic" / "attempt-01" / "probe.json"
    path.unlink()
    path = h.store.run("stage").directory / "progress" / binding["work_key"] / "traffic" / "attempt-01" / "probe.json"
    path.unlink()
    count = len(h.cloud.invocations)
    with pytest.raises(StateError, match="traffic_checkpoint_missing"):
        h.staging()
    assert len(h.cloud.invocations) == count


def test_state_logs_and_result_paths_contain_no_test_event_outbox(tmp_path):
    h = Harness(tmp_path)
    h.daily(test_run=True, rerun=1)
    directory = h.store.run("trial").directory
    assert (directory / "runner.log").is_file() and (directory / "events.jsonl").is_file()
    assert not h.store.outbox("events").directory.exists()
    pointer = h.store.run("trial").read("quality-result")
    assert h.store.run("trial").read_artifact(pointer["artifact"])["score"] == 100
    assert json.loads((directory / "events.jsonl").read_text().splitlines()[-1])["kind"] == "completed"


def test_terminal_insights_failure_excludes_one_unit_and_narrows_next_window(tmp_path):
    h = Harness(tmp_path, issues=4)
    original = h.cloud.get_insights_run
    async def poll(monitor, run_id):
        value = await original(monitor, run_id)
        job = next(job for job in h.cloud.jobs.values() if job["id"] == run_id)
        return {**value, "status": "failed"} if job["target"].endswith("issue-001") else value
    h.cloud.get_insights_run = poll
    result = h.daily()
    assert result.status.value == "Partial"
    assert result.coverage.excluded_units == 1
    assert len(h.cloud.starts) == 5
    previous_end = None
    for target in h.catalog.targets:
        binding = h.store.run("trial").read(f"targets/{target.key}/source")
        intent = h.store.run("trial").read(binding["work_key"] + "/insights/start")
        if previous_end:
            assert intent["requested_window_start"] > previous_end
        receipts = h.store.run("trial").read(binding["work_key"] + "/traffic/attempt-10/probe")
        previous_end = receipts["completed_at"]


def test_missing_trace_exclusion_does_not_block_other_versions_or_erase_their_results(tmp_path):
    h = Harness(tmp_path, issues=4)
    invoke = h.cloud.invoke
    async def selective(deployment, step, **kwargs):
        h.cloud.ready_count = 5 if deployment.target_key.endswith("issue-001") else 10
        return await invoke(deployment, step, **kwargs)
    h.cloud.invoke = selective
    first = h.daily()
    assert first.status.value == "Partial" and first.coverage.excluded_units == 1
    assert len(h.cloud.starts) == 4
    assert h.daily().to_dict() == first.to_dict()
    assert len(h.cloud.starts) == 4


def test_definitively_rejected_session_retries_are_bounded_across_resume(tmp_path):
    h = Harness(tmp_path, profile="staging", hosted=True, retry_limit=1)
    def rejected(deployment, request_id, persist):
        raise QualityError("synthetic_rejection", request_accepted=False, retryable=True, status=429)
    h.cloud.session_hook = rejected
    h.staging()
    count = sum(event[0] == "session" for event in h.cloud.events)
    assert count == 40
    h.staging()
    assert sum(event[0] == "session" for event in h.cloud.events) == count


def test_pending_deployment_deadline_and_resume_keep_provider_identity(tmp_path):
    h = Harness(tmp_path, profile="staging", issues=0)
    h.cloud.pending_deployments = 100
    first = h.staging()
    target = h.catalog.targets[0]
    assert first["results"][0]["status"] == "INCOMPLETE"
    assert h.cloud.deployment_calls[target.key] == 4
    assert sum(h.clock.waits) == 3
    h.cloud.pending_deployments = 0
    assert h.staging()["results"][0]["status"] == "PASS"
    assert h.cloud.deployments[target.key].provider_version == "1"


def test_environment_scope_mismatch_is_rejected_before_side_effects(tmp_path):
    h = Harness(tmp_path)
    h.cloud.environment = replace(h.cloud.environment, profile="staging")
    with pytest.raises(QualityError, match="runner_environment_mismatch"):
        h.runner()
    assert not h.cloud.events


def test_reassess_without_retained_evidence_fails_closed(tmp_path):
    h = Harness(tmp_path, profile="staging")
    selections = (Selection(h.catalog.targets[0], "reassess", ("evaluation_changed",)),)
    with pytest.raises(QualityError, match="retained_evidence_missing"):
        h.staging(selections=selections)
    assert not h.cloud.invocations


def test_daily_evidence_repair_never_replaces_pre_insights_visible_snapshot(tmp_path):
    h = Harness(tmp_path)
    h.cloud.query_complete = False
    assert h.daily().score is None
    count = len(h.cloud.invocations), len(h.cloud.starts)
    target = h.catalog.targets[1]
    first = h.store.run("trial").read(f"targets/{target.key}/source")
    visible = h.store.run("trial").read_completed(first["work_key"] + "/insights")["visible_snapshot"]
    h.cloud.query_complete = True
    assert h.daily().score == 100
    last = h.store.run("trial").read(f"targets/{target.key}/source")
    assert last["evidence_key"] != visible
    assert h.store.run("trial").read_artifact(visible)["query_complete"] is False
    assert (len(h.cloud.invocations), len(h.cloud.starts)) == count


def test_missing_assessment_artifact_is_not_accepted_from_mutable_index(tmp_path):
    h = Harness(tmp_path, profile="staging")
    h.staging()
    target = h.catalog.targets[0]
    binding = h.store.staging_index.read(target.key)
    path = h.store.run("stage")._path("artifacts", binding["assessment"]["artifact"])
    path.unlink()
    with pytest.raises(StateError, match="state_record_missing"):
        h.staging()


def test_crash_before_provider_callback_has_durable_intent_and_no_repost(tmp_path):
    h = Harness(tmp_path, profile="staging", issues=0)
    actual = h.cloud.invoke
    crashed = []
    async def invoke(deployment, step, **kwargs):
        crashed.append(kwargs["request_id"])
        raise RuntimeError("synthetic interruption before callback")
    h.cloud.invoke = invoke
    with pytest.raises(RuntimeError, match="interruption"):
        h.staging()
    h.cloud.invoke = actual
    h.staging()
    assert not any(item[2] == crashed[0] for item in h.cloud.invocations)
    assert h.sol.calls[0]["attempts"][0]["steps"][0]["execution"]["status"] == "unknown"


def test_monitor_creation_unknown_never_reposted_on_resume(tmp_path):
    h = Harness(tmp_path)
    calls = []
    async def ensure(name):
        calls.append(name)
        raise QualityError("synthetic_monitor_unknown", request_accepted=None, retryable=True)
    h.cloud.ensure_monitor = ensure
    assert h.daily().score is None
    assert h.daily().score is None
    assert len(calls) == 1
    assert not h.cloud.invocations


def test_daily_requires_baseline_first_even_for_direct_callers(tmp_path):
    h = Harness(tmp_path)
    with h.store.ownership():
        r = h.runner()
        targets = tuple(reversed(h.catalog.targets))
        r.initialize(targets, DAY, kind="daily")
        with pytest.raises(QualityError, match="daily_baseline_must_be_first"):
            asyncio.run(r.run_daily(targets))
        r.logger.close()
    assert not h.cloud.invocations


def admission_service(h, *, rejected=0, unknown=0, metadata=True, extra_delay=0):
    """Model admission-relative lookback and a successful service watermark."""
    accepted, calls, boundaries = {}, [], {}

    async def start(monitor, lookback_hours, operation_id, persist):
        nonlocal rejected, unknown
        target = h.cloud.active[monitor].target_key
        calls.append((target, operation_id, lookback_hours, h.clock.now()))
        persist({
            "monitor_id": monitor, "operation_id": operation_id,
            "request_body": {"lookback_hours": lookback_hours}, "submission_state": "submitting",
        })
        if not target.endswith("/v0") and rejected:
            rejected -= 1
            raise QualityError("synthetic_rate_limit", request_accepted=False, retryable=True, status=429)
        if not target.endswith("/v0") and unknown:
            unknown -= 1
            raise QualityError("synthetic_no_response", request_accepted=None, retryable=True)
        if extra_delay:
            await h.clock.sleep(extra_delay)
        if operation_id not in accepted:
            admitted = h.clock.now()
            window_start = admitted - timedelta(hours=lookback_hours)
            if monitor in boundaries:
                window_start = max(window_start, boundaries[monitor])
            accepted[operation_id] = {
                "id": operation_id, "target": target, "admitted": admitted,
                "window_start": window_start, "window_end": admitted,
            }
        value = {"id": operation_id, "status": "running", "submission_state": "accepted"}
        persist(value)
        return value

    async def poll(monitor, run_id):
        job = accepted[run_id]
        boundaries[monitor] = job["window_end"]
        rows = [
            row for row in h.cloud.rows
            if row["operation_Id"] == "operation-" + job["target"]
            and job["window_start"] <= datetime.fromisoformat(row["timestamp"]) <= job["window_end"]
        ]
        if rows and not job["target"].endswith("/v0"):
            h.cloud.cards[monitor].append({"id": job["target"], "title": "Synthetic defect"})
        result = {"id": run_id, "status": "succeeded"}
        if metadata:
            result.update(window_start=job["window_start"].isoformat(), window_end=job["window_end"].isoformat())
        return result

    h.cloud.start_insights, h.cloud.get_insights_run = start, poll
    return calls, accepted


@pytest.mark.parametrize("metadata", [False, True])
def test_definite_rejections_renew_operation_and_cover_original_traffic_at_admission(tmp_path, metadata):
    h = Harness(tmp_path)
    calls, accepted = admission_service(h, rejected=3, metadata=metadata)
    result = h.daily()
    issue_calls = [item for item in calls if item[0].endswith("issue-001")]
    assert len(issue_calls) == 4
    assert len({item[1] for item in issue_calls}) == 4
    assert result.status.value == "Full" and result.score == 100
    job = accepted[issue_calls[-1][1]]
    probe_times = [
        datetime.fromisoformat(row["timestamp"]) for row in h.cloud.rows
        if row["operation_Id"].endswith("issue-001")
    ]
    assert job["window_start"] <= min(probe_times)
    assert len(h.cloud.invocations) == 40
    payload = next(item for item in h.sol.calls if item["target"]["unit_id"]["logical_version"] == "issue-001")
    assert payload["engine_started_at"] == job["admitted"].isoformat()
    assert payload["engine_window"]["coverage_proven"] is True


@pytest.mark.parametrize("metadata", [False, True])
def test_uncertain_post_replays_exact_operation_but_excluded_window_cannot_be_engine_miss(tmp_path, metadata):
    h = Harness(tmp_path)
    calls, _ = admission_service(h, unknown=3, metadata=metadata)
    result = h.daily()
    issue_calls = [item for item in calls if item[0].endswith("issue-001")]
    assert len(issue_calls) == 4
    assert len({(item[1], item[2]) for item in issue_calls}) == 1
    issue = next(item for item in result.units if item.planned.is_issue)
    assert issue.scorable is False
    assert result.score is None
    assert len(h.cloud.invocations) == 40


def test_saved_assessment_is_recovered_if_derived_result_checkpoint_crashes(tmp_path, monkeypatch):
    h = Harness(tmp_path, profile="staging", issues=0)
    from agent_insights_quality.state import RecordStore
    save = RecordStore.save_progress
    crashed = False
    def interrupt(records, key, value):
        nonlocal crashed
        if key.endswith("/source") and "result" in value and not crashed:
            crashed = True
            raise RuntimeError("synthetic crash before derived result")
        save(records, key, value)
    monkeypatch.setattr(RecordStore, "save_progress", interrupt)
    with pytest.raises(RuntimeError, match="derived result"):
        h.staging()
    pending = h.store.run("stage").read("targets/" + h.catalog.targets[0].key + "/assessment")
    assert len(h.cloud.invocations) == 20 and len(h.sol.calls) == 1
    result = h.staging()
    assert len(h.cloud.invocations) == 20 and len(h.sol.calls) == 1
    assert result["results"][0]["assessment"]["artifact"] == pending["artifact"]
    assert result["results"][0]["status"] == "PASS"


def test_unknown_then_definite_rejection_cannot_retire_a_possibly_accepted_operation(tmp_path):
    h = Harness(tmp_path)
    calls, _ = admission_service(h)
    original = h.cloud.start_insights
    attempted = []
    async def start(monitor, lookback, operation_id, persist):
        if h.cloud.active[monitor].target_key.endswith("issue-001"):
            attempted.append((operation_id, lookback))
            if len(attempted) == 1:
                raise QualityError("synthetic_unknown", retryable=True)
            if len(attempted) == 2:
                raise QualityError("synthetic_rejected_retry", retryable=True, request_accepted=False)
        return await original(monitor, lookback, operation_id, persist)
    h.cloud.start_insights = start
    result = h.daily()
    assert len(attempted) == 3 and len(set(attempted)) == 1
    assert calls[-1][1:3] == attempted[-1]
    assert result.score is None


@pytest.mark.parametrize("metadata", [False, True])
def test_lost_acceptance_response_keeps_exact_operation_and_truthful_admission_bounds(tmp_path, metadata):
    h = Harness(tmp_path)
    calls, accepted = admission_service(h, metadata=metadata)
    original = h.cloud.start_insights
    lost = False
    async def start(monitor, lookback, operation_id, persist):
        nonlocal lost
        if not lost and h.cloud.active[monitor].target_key.endswith("issue-001"):
            lost = True
            await original(monitor, lookback, operation_id, lambda value: None)
            raise QualityError("synthetic_lost_response", retryable=True)
        return await original(monitor, lookback, operation_id, persist)
    h.cloud.start_insights = start
    result = h.daily()
    issue_calls = [item for item in calls if item[0].endswith("issue-001")]
    assert len(issue_calls) == 2 and issue_calls[0][1:3] == issue_calls[1][1:3]
    window = next(item["engine_window"] for item in h.sol.calls
                  if item["target"]["unit_id"]["logical_version"] == "issue-001")
    if metadata:
        assert result.score == 100
        assert window["admission_earliest"] == accepted[issue_calls[0][1]]["admitted"].isoformat()
    else:
        assert result.score is None
        assert window["admission_earliest"] < window["admission_latest"]


@pytest.mark.parametrize("window", [
    {"window_start": "invalid", "window_end": "also invalid"},
    {"window_start": "2026-09-04T12:00:01+00:00", "window_end": "2026-09-04T12:00:00+00:00"},
    {"window_start": True, "window_end": 42},
    {"window_start": "2026-09-04T12:00:00+00:00"},
])
def test_optional_invalid_window_metadata_excludes_without_retraffic(tmp_path, window):
    h = Harness(tmp_path)
    start = h.cloud.start_insights
    async def no_metadata(monitor, lookback, operation_id, persist):
        def stripped(value):
            return {key: item for key, item in value.items() if key not in {"window_start", "window_end"}}
        return stripped(await start(monitor, lookback, operation_id, lambda value: persist(stripped(value))))
    h.cloud.start_insights = no_metadata
    original = h.cloud.get_insights_run
    async def poll(monitor, run_id):
        result = await original(monitor, run_id)
        return {"id": result["id"], "status": result["status"], **window}
    h.cloud.get_insights_run = poll
    result = h.daily()
    assert result.score is None and result.counts.noise_cards == 0
    assert len(h.cloud.invocations) == 40
    assert all("insights_window_metadata_invalid" in call["engine_window"]["reasons"] for call in h.sol.calls)


def test_uncertain_engine_window_cannot_create_scored_noise(tmp_path):
    h = Harness(tmp_path)
    admission_service(h, unknown=3, metadata=False)
    original_poll, original_sol = h.cloud.get_insights_run, h.sol.complete_json
    async def poll(monitor, run_id):
        value = await original_poll(monitor, run_id)
        h.cloud.cards[monitor] = [{"id": "synthetic-noise-card-" + run_id}]
        return value
    async def sol(**kwargs):
        value = await original_sol(**kwargs)
        for card in value["cards"]:
            if card["core"] == "correct":
                card.update(core="incorrect", expected_match=False, root_group=None)
        return value
    h.cloud.get_insights_run, h.sol.complete_json = poll, sol
    result = h.daily()
    issue = next(item for item in result.units if item.planned.is_issue)
    assert not issue.scorable and issue.counts.noise_cards == 0
    assert any(not finding.scored for finding in issue.findings)


@pytest.mark.parametrize("count", [5, 6])
def test_engine_window_retains_six_probe_presence_rule(tmp_path, count):
    from agent_insights_quality.telemetry import Snapshot, ResponseScope
    h = Harness(tmp_path)
    runner = h.runner()
    first = h.clock.now()
    receipts = {
        (index, "probe"): Invocation(
            str(index), str(index), None, (first + timedelta(seconds=index)).isoformat(),
            (first + timedelta(seconds=index)).isoformat(), "completed", {"output": "synthetic"}, 200,
        ) for index in range(1, 11)
    }
    snapshot = Snapshot(
        first.isoformat(), first.isoformat(), (first + timedelta(seconds=11)).isoformat(),
        tuple({"ref": f"row-{index}", "raw": {"timestamp": receipt.started_at}}
              for (index, _), receipt in receipts.items()),
        tuple(ResponseScope(str(index), ("one-operation",), (f"row-{index}",), (f"row-{index}",))
              for index in range(1, 11)), True,
    )
    window = {
        "basis": "bounded_submission", "start_latest": (first + timedelta(seconds=11 - count)).isoformat(),
        "end_earliest": (first + timedelta(seconds=11)).isoformat(), "reasons": [],
    }
    visible, result = runner._engine_visible(attempts(h.catalog.targets[0]), receipts, snapshot, {"engine_window": window})
    assert result["attributable_probe_attempts"] == count
    assert result["coverage_proven"] == (count == 6)
    assert len(visible.scopes) == 10 and len(receipts) == 10
    runner.logger.close()


def test_successful_watermark_limits_the_proven_window_even_with_long_lookback(tmp_path):
    h = Harness(tmp_path)
    runner = h.runner()
    window = runner._engine_window({
        "started_at": h.clock.now().isoformat(),
        "submission_timing": {"first_post_at": h.clock.now().isoformat(), "response_at": h.clock.now().isoformat()},
        "previous_successful_end": (h.clock.now() - timedelta(seconds=1)).isoformat(),
        "request_body": {"lookback_hours": 0.1},
    }, {"status": "succeeded"})
    assert window["start_latest"] == (h.clock.now() - timedelta(seconds=1)).isoformat()
    runner.logger.close()


def test_crash_after_definite_rejection_renews_only_rejected_submission_on_resume(tmp_path, monkeypatch):
    from agent_insights_quality.state import RecordStore
    h = Harness(tmp_path)
    calls, _ = admission_service(h, rejected=1, metadata=False)
    save = RecordStore.save_progress
    crashed = False
    def interrupt(records, key, value):
        nonlocal crashed
        save(records, key, value)
        if key.endswith("/insights/start") and value.get("submission_state") == "rejected" and not crashed:
            crashed = True
            raise RuntimeError("synthetic crash after definite rejection")
    monkeypatch.setattr(RecordStore, "save_progress", interrupt)
    with pytest.raises(RuntimeError, match="definite rejection"):
        h.daily()
    count = len(h.cloud.invocations)
    h.clock.value += timedelta(hours=1)
    assert h.daily().score == 100
    issue_calls = [item for item in calls if item[0].endswith("issue-001")]
    assert len(issue_calls) == 2 and issue_calls[0][1] != issue_calls[1][1]
    assert issue_calls[1][2] > issue_calls[0][2]
    assert len(h.cloud.invocations) == count == 40


def test_crash_after_accepted_callback_does_not_restart_or_retimestamp_insights(tmp_path):
    h = Harness(tmp_path)
    crashed = False
    def interrupt(value, persist):
        nonlocal crashed
        if value["target"].endswith("issue-001") and not crashed:
            crashed = True
            persist(value)
            raise RuntimeError("synthetic crash after accepted callback")
    h.cloud.start_hook = interrupt
    with pytest.raises(RuntimeError, match="accepted callback"):
        h.daily()
    calls = len(h.cloud.invocations), len(h.cloud.starts)
    original_end = next(job["window_end"] for job in h.cloud.jobs.values() if job["target"].endswith("issue-001"))
    h.clock.value += timedelta(hours=1)
    assert h.daily().score == 100
    assert (len(h.cloud.invocations), len(h.cloud.starts)) == calls
    payload = next(item for item in h.sol.calls if item["target"]["unit_id"]["logical_version"] == "issue-001")
    assert payload["engine_started_at"] == original_end


def test_child_evidence_outside_engine_window_is_retained_but_not_citable_as_visible(tmp_path):
    from agent_insights_quality.telemetry import Snapshot, ResponseScope
    h = Harness(tmp_path)
    r = h.runner()
    now = h.clock.now()
    receipt = Invocation("request", "response", None, now.isoformat(), now.isoformat(),
                         "completed", {"output": "synthetic"}, 200)
    snapshot = Snapshot(
        now.isoformat(), (now - timedelta(seconds=1)).isoformat(), (now + timedelta(seconds=20)).isoformat(),
        (
            {"ref": "anchor", "raw": {"timestamp": now.isoformat()}},
            {"ref": "late-child", "raw": {"timestamp": (now + timedelta(seconds=10)).isoformat()}},
            {"ref": "unknown-time", "raw": {"message": "synthetic"}},
        ),
        (ResponseScope("response", ("operation",), ("anchor",), ("anchor", "late-child", "unknown-time")),),
        True,
    )
    visible, _ = r._engine_visible(attempts(h.catalog.targets[0]), {(1, "probe"): receipt}, snapshot, {
        "engine_window": {
            "basis": "provider_window", "reasons": [], "start_latest": (now - timedelta(seconds=1)).isoformat(),
            "end_earliest": (now + timedelta(seconds=1)).isoformat(),
        },
    })
    assert visible.records == snapshot.records
    assert visible.scopes[0].evidence_refs == ("anchor",)
    r.logger.close()


def sol_http_failure(h):
    from agent_insights_quality.providers.sol import SolResponseError
    complete = h.sol.complete_json
    failed = True

    async def respond(**kwargs):
        nonlocal failed
        if failed:
            h.sol.calls.append(deepcopy(kwargs["payload"]))
            raise SolResponseError(
                "sol_http_error", {"error": {"code": "invalid_json_schema", "message": "uniqueItems unsupported"}},
                request_accepted=False, status=400,
            )
        return await complete(**kwargs)

    def repair():
        nonlocal failed
        failed = False

    h.sol.complete_json = respond
    return repair


@pytest.mark.parametrize("original_failure_record", [False, True])
def test_choose_staging_reuses_identical_packet_after_sol_only_http400_repair(tmp_path, original_failure_record):
    h = Harness(tmp_path, profile="staging", issues=0)
    repair = sol_http_failure(h)
    initial = h.staging()
    assert initial["results"][0]["status"] == "INCOMPLETE"
    if original_failure_record:
        # The active pre-fix run recorded its failure in the original traffic run.
        record = h.store.staging_index.read(h.catalog.targets[0].key)
        record.pop("failure_run_id")
        with h.store.ownership():
            h.store.staging_index.save_progress(h.catalog.targets[0].key, record)
    packet = deepcopy(h.sol.calls[0])
    calls = len(h.cloud.invocations), sum(h.cloud.deployment_calls.values()), len(h.cloud.events)
    changes = SourceChanges(("src/agent_insights_quality/providers/sol.py",))
    selected = choose_staging(h.catalog, h.store, changes_since=lambda _: changes)
    assert len(selected) == 1 and selected[0].action == "reassess"
    repair()
    result = h.staging("sol-repair", selections=selected, revision="source-two", changes=changes.paths)
    assert result["results"][0]["status"] == "PASS"
    assert (len(h.cloud.invocations), sum(h.cloud.deployment_calls.values())) == calls[:2]
    assert all(event[0] == "assessment" for event in h.cloud.events[calls[2]:])
    assert h.sol.calls[1] == packet
    assert result["results"][0]["evidence_key"] == initial["results"][0]["evidence_key"]
    assert result["results"][0]["traffic_source_revision"] == "source-one"
    assert "reason" not in result["results"][0]
    assert "failure_run_id" not in result["results"][0]


@pytest.mark.parametrize("gap", ["partial-query", "five-probes", "collector-change", "traffic-change", "full"])
def test_assessment_failure_does_not_reuse_incomplete_or_invalidated_evidence(tmp_path, gap):
    h = Harness(tmp_path, profile="staging", issues=0)
    repair = sol_http_failure(h)
    if gap == "partial-query":
        h.cloud.query_complete = False
    elif gap == "five-probes":
        h.cloud.ready_count = 5
    h.staging()
    target = h.catalog.targets[0]
    changed = {
        "collector-change": "src/agent_insights_quality/telemetry.py",
        "traffic-change": target.version_root.relative_to(h.catalog.root) / "traffic.json",
    }.get(gap, "src/agent_insights_quality/providers/sol.py")
    choices = choose_staging(
        h.catalog, h.store, full=gap == "full", changes_since=lambda _: SourceChanges((changed,)),
    )
    assert choices[0].action == "traffic"
    if gap in {"partial-query", "five-probes", "collector-change"}:
        before = sum(event[0] == "query" for event in h.cloud.events)
        repair()
        h.staging("repair", selections=choices, revision="source-two", changes=(changed,))
        assert sum(event[0] == "query" for event in h.cloud.events) > before
        assert len(h.cloud.invocations) == 20


def test_failed_reassessment_uses_its_own_failure_reference_and_original_evidence(tmp_path):
    h = Harness(tmp_path, profile="staging", issues=0)
    initial = h.staging()
    repair = sol_http_failure(h)
    choices = choose_staging(h.catalog, h.store, changes_since=lambda _: SourceChanges(
        ("src/agent_insights_quality/assessment.py",),
    ))
    failed = h.staging(
        "evaluation-failure", selections=choices, revision="source-two",
        changes=("src/agent_insights_quality/assessment.py",),
    )
    assert failed["results"][0]["failure_run_id"] == "evaluation-failure"
    count = len(h.cloud.invocations), sum(h.cloud.deployment_calls.values()), sum(event[0] == "query" for event in h.cloud.events)
    packet = deepcopy(h.sol.calls[-1])
    choices = choose_staging(h.catalog, h.store, changes_since=lambda _: SourceChanges(
        ("src/agent_insights_quality/providers/sol.py",),
    ))
    assert choices[0].action == "reassess"
    repair()
    result = h.staging(
        "sol-repair", selections=choices, revision="source-three",
        changes=("src/agent_insights_quality/providers/sol.py",),
    )
    assert result["results"][0]["status"] == "PASS"
    assert result["results"][0]["evidence_key"] == initial["results"][0]["evidence_key"]
    assert h.sol.calls[-1] == packet
    assert (len(h.cloud.invocations), sum(h.cloud.deployment_calls.values()),
            sum(event[0] == "query" for event in h.cloud.events)) == count


def test_incomplete_assessment_clears_prior_transport_failure_before_next_selection(tmp_path):
    h = Harness(tmp_path, profile="staging", issues=0)
    repair = sol_http_failure(h)
    h.staging()
    repair()
    h.cloud.query_complete = False
    result = h.staging()
    assert result["results"][0]["status"] == "INCOMPLETE"
    assert "reason" not in result["results"][0] and "failure_run_id" not in result["results"][0]
    choices = choose_staging(h.catalog, h.store, changes_since=lambda _: SourceChanges(
        ("src/agent_insights_quality/providers/sol.py",),
    ))
    assert choices[0].action == "traffic"


def test_staging_latest_reference_is_durable_before_any_provider_side_effect(tmp_path):
    h = Harness(tmp_path, profile="staging", issues=0)
    original = h.cloud.ensure_deployment
    async def ensure(target, *args):
        pointer = h.store.outbox("staging").read(f"targets/{target.key}")
        assert pointer["run_id"] == "stage"
        binding = h.store.run(pointer["run_id"]).read(f"targets/{target.key}/source")
        assert pointer["work_key"] == binding["work_key"]
        assert binding["binding_run_id"] == "stage"
        assert h.store.staging_index.read(target.key, missing_ok=True) is None
        return await original(target, *args)
    h.cloud.ensure_deployment = ensure
    assert h.staging()["results"][0]["status"] == "PASS"


def test_staging_latest_reference_write_failure_stops_before_deployment(tmp_path, monkeypatch):
    from agent_insights_quality.state import RecordStore
    h = Harness(tmp_path, profile="staging", issues=0)
    save = RecordStore.save_progress
    def fail(records, key, value):
        if records.directory.name == "staging" and key.startswith("targets/"):
            raise CheckpointError()
        save(records, key, value)
    monkeypatch.setattr(RecordStore, "save_progress", fail)
    with pytest.raises(CheckpointError):
        h.staging()
    assert not h.cloud.deployment_calls and not h.cloud.invocations


def test_stale_runner_cannot_overwrite_new_target_source_or_index(tmp_path):
    h = Harness(tmp_path, profile="staging", issues=0)
    target = h.catalog.targets[0]
    with h.store.ownership():
        older = h.runner("older")
        older.initialize((target,), DAY, kind="staging")
        old_work = older._binding(target, Selection(target, "traffic", ("missing",)))
        h.clock.value += timedelta(seconds=1)
        newer = h.runner("newer", revision="source-two")
        newer.initialize((target,), DAY, kind="staging")
        new_work = newer._binding(target, Selection(target, "traffic", ("incomplete",)))
        assert new_work.key == old_work.key
        saved = h.store.run("newer").read(f"targets/{target.key}/source")
        for operation in (
            lambda: older._update(target, old_work, reason="synthetic_stale_failure"),
            lambda: older._index_staging(target, old_work),
            lambda: older._binding(target, Selection(target, "traffic", ("incomplete",))),
        ):
            with pytest.raises(StateError, match="staging_target_superseded"):
                operation()
        assert h.store.run("newer").read(f"targets/{target.key}/source") == saved
        assert h.store.staging_index.read(target.key, missing_ok=True) is None
        older.logger.close()
        newer.logger.close()
    assert not h.cloud.invocations


def test_stale_active_run_and_result_index_cannot_displace_latest_work(tmp_path):
    from agent_insights_quality.runner import reconcile_staging_work
    h = Harness(tmp_path, profile="staging", issues=0)
    target = h.catalog.targets[0]
    h.staging("older")
    older_index = h.store.staging_index.read(target.key)
    h.clock.value += timedelta(seconds=1)
    h.staging("newer", revision="source-two", selections=(Selection(target, "traffic", ("full",)),))
    newer_index = h.store.staging_index.read(target.key)
    with h.store.ownership():
        h.store.staging_index.save_progress(target.key, older_index)
        h.store.outbox("staging").save_progress("full", {
            "run_id": "older", "source_revision": "source-one", "report_date": DAY.isoformat(),
            "full": True, "completed": False,
        })
        reconcile_staging_work(h.catalog, h.store)
        assert h.store.outbox("staging").read(f"targets/{target.key}") == {
            "run_id": "newer", "work_key": newer_index["work_key"],
        }
    revisions = []
    choices = choose_staging(
        h.catalog, h.store,
        changes_since=lambda revision: (revisions.append(revision) or SourceChanges(())),
    )
    assert revisions == ["source-two"] and not choices


@pytest.mark.parametrize("damage", ["missing-binding", "wrong-work", "cross-profile"])
def test_latest_target_reference_damage_fails_closed_without_index_fallback(tmp_path, damage):
    h = Harness(tmp_path, profile="staging", issues=0)
    h.staging()
    target = h.catalog.targets[0]
    if damage == "missing-binding":
        h.store.run("stage")._path("progress", f"targets/{target.key}/source").unlink()
    elif damage == "wrong-work":
        with h.store.ownership():
            h.store.outbox("staging").save_progress(f"targets/{target.key}", {
                "run_id": "stage", "work_key": f"targets/{target.key}/work-other",
            })
    else:
        environment = h.store.run("stage")._path("completed", "environment")
        content = json.loads(environment.read_text())
        content["profile"] = "daily"
        environment.write_text(json.dumps(content))
    count = len(h.cloud.invocations)
    with pytest.raises(StateError):
        choose_staging(h.catalog, h.store, changes_since=lambda _: SourceChanges(()))
    assert len(h.cloud.invocations) == count


def test_retained_staging_traffic_cannot_be_reused_in_a_changed_environment(tmp_path):
    h = Harness(tmp_path, profile="staging", issues=0)
    h.staging()
    h.cloud.environment = replace(h.cloud.environment, project_endpoint="https://changed-synthetic.invalid")
    count = len(h.cloud.invocations), sum(h.cloud.deployment_calls.values())
    with pytest.raises(StateError, match="retained_environment_mismatch"):
        h.staging("changed-environment", revision="source-two", selections=(
            Selection(h.catalog.targets[0], "reassess", ("evaluation_changed",)),
        ))
    assert (len(h.cloud.invocations), sum(h.cloud.deployment_calls.values())) == count


def test_unindexed_partial_work_remains_discoverable_without_any_active_run_pointer(tmp_path):
    h = Harness(tmp_path, profile="staging", issues=0, hosted=True)
    target = h.catalog.targets[0]
    seen = []
    def interrupt(deployment, step, request_id, session, persist):
        seen.append(request_id)
        if len(seen) == 6:
            raise OSError(32, "synthetic global failure")
    h.cloud.invoke_hook = interrupt
    with pytest.raises(OSError):
        h.staging("partial")
    assert h.store.staging_index.read(target.key, missing_ok=True) is None
    assert h.store.outbox("staging").read("full", missing_ok=True) is None
    assert h.store.outbox("staging").read("incremental", missing_ok=True) is None
    choices = choose_staging(h.catalog, h.store, changes_since=lambda _: SourceChanges(
        ("src/agent_insights_quality/providers/acr.py",),
    ))
    assert choices[0].reasons == ("incomplete",)
    h.cloud.invoke_hook = None
    result = h.staging("resumed", revision="source-two", selections=choices,
                       changes=("src/agent_insights_quality/providers/acr.py",))
    assert result["results"][0]["status"] == "PASS"
    assert result["results"][0]["traffic_run_id"] == "partial"
    assert result["results"][0]["traffic_source_revision"] == "source-one"
    assert len(h.cloud.invocations) == 20
    assert sum(call[2] == seen[-1] for call in h.cloud.invocations) == 1


def test_frozen_runner_compares_shared_source_once_and_keeps_distinct_bases(tmp_path):
    h = Harness(tmp_path, profile="staging", issues=2)
    h.staging("original")
    changes = SourceChanges(("src/agent_insights_quality/assessment.py",))
    with h.store.ownership():
        h.clock.value += timedelta(seconds=1)
        intermediate = h.runner("intermediate", revision="source-two", changes=changes.paths)
        intermediate.initialize((h.catalog.targets[0],), DAY, kind="staging")
        intermediate._binding(h.catalog.targets[0], Selection(
            h.catalog.targets[0], "reassess", ("evaluation_changed",),
        ))
        intermediate.logger.close()
        h.clock.value += timedelta(seconds=1)
        current = h.runner("current", revision="source-three")
        current.initialize(h.catalog.targets, DAY, kind="staging")
        calls = []
        def compare(base):
            calls.append(base)
            return changes
        current.changes_since = compare
        for target in h.catalog.targets:
            current._binding(target, Selection(target, "reassess", ("evaluation_changed",)))
        assert calls == ["source-two", "source-one"]
        assert current._changes_since("source-one") is changes
        assert current._changes_since("source-two") is changes
        assert calls == ["source-two", "source-one"]
        current.logger.close()


def test_source_comparison_failure_is_not_cached(tmp_path):
    h = Harness(tmp_path, profile="staging", issues=0)
    current = h.runner()
    calls = []
    expected = SourceChanges(("src/agent_insights_quality/assessment.py",))
    def compare(base):
        calls.append(base)
        if len(calls) == 1:
            raise OSError(5, "synthetic source comparison unavailable")
        return expected
    current.changes_since = compare
    with pytest.raises(OSError):
        current._changes_since("previous")
    assert current._changes_since("previous") is expected
    assert current._changes_since("previous") is expected
    assert calls == ["previous", "previous"]
    current.logger.close()
