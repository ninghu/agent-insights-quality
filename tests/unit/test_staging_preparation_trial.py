import asyncio
from collections import Counter
from dataclasses import replace
import json
import sys

import pytest

from agent_insights_quality.errors import QualityError
from agent_insights_quality.performance import RunMetrics
from agent_insights_quality.preparation_audit import audit_run
from agent_insights_quality.results import UnitId
from agent_insights_quality.selection import Selection
from agent_insights_quality.state import RecordStore, RuntimeStore
from agent_insights_quality.telemetry import Snapshot
import test_cli as cli_fakes
import test_runner as fake
from test_session_lookahead import harness, run_profile, run_traffic, session_index, work_key

app = cli_fakes.app


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    fake.fake_storage(monkeypatch)


@pytest.mark.parametrize("budget", [1, 2, 10])
@pytest.mark.parametrize("ahead", [0, 1])
def test_normal_staging_qualifies_ten_original_attempts_with_only_preparation_overlap(tmp_path, budget, ahead):
    h = harness(tmp_path, profile="staging", staging_ahead=ahead, budget=budget)
    create, invoke = h.cloud.create_session, h.cloud.invoke
    sessions, active, overlaps, calls = {}, set(), [], []

    async def prepare(deployment, request_id, persist):
        index = session_index(h, deployment, request_id)
        key = work_key(h, h.catalog.targets[0])
        affinity = h.store.run("trial").read_completed(
            key + f"/traffic/attempt-{index:02d}/session-affinity", missing_ok=True,
        )
        assert bool(affinity) == bool(ahead)
        if ahead:
            assert affinity["provider_version"] == deployment.provider_version
            assert affinity["target_key"] == deployment.target_key
        assert h.cloud.active[deployment.agent_name] == deployment
        if active:
            assert active == {index - 1}
            overlaps.append(index)
        value = await create(deployment, request_id, persist)
        sessions[index] = value
        await h.clock.sleep(1)
        return value

    async def business(deployment, step, **kwargs):
        index = int(step.body["input"].split()[-1])
        assert not active
        assert kwargs["session_id"] == sessions[index] and kwargs["previous_response_id"] is None
        assert len(calls) == (index - 1) * 2 + (step.phase == "probe")
        active.add(index)
        calls.append((index, step.phase, dict(step.body)))
        try:
            await h.clock.sleep(2)
            return await invoke(deployment, step, **kwargs)
        finally:
            active.remove(index)

    h.cloud.create_session, h.cloud.invoke = prepare, business
    with h.store.ownership():
        metrics = RunMetrics(h.store.run("trial"), monotonic=h.clock.monotonic)
        runner = h.runner(metrics=metrics)
        runner.initialize(h.catalog.targets, fake.DAY, kind="staging")
        try:
            result = asyncio.run(asyncio.wait_for(run_profile(h, runner), 5))
            assert runner.attempt_limit._value == budget
        finally:
            runner.logger.close()
        metrics.finalize()
    assert bool(overlaps) == bool(ahead and budget > 1)
    assert len(sessions) == len(set(sessions.values())) == 10
    assert [c[:2] for c in calls] == [(i, phase) for i in range(1, 11) for phase in ("setup", "probe")]
    assert [c[2] for c in calls] == [dict(s.body) for a in fake.attempts(h.catalog.targets[0]) for s in a.steps]
    assert result["results"][0]["status"] == "PASS"
    assert result["results"][0]["minimum_required"] == 8
    assert h.settings.readiness_attempts == 6
    assert len(h.sol.calls) == 1 and len(h.sol.calls[0]["attempts"]) == 10
    assert all(a["steps"][1]["allowed_citation_refs"] for a in h.sol.calls[0]["attempts"])
    assert not h.cloud.starts and not h.cloud.resets
    assert not any(e[0] in {"monitor", "cards", "start"} for e in h.cloud.events)
    assert metrics.peak["port_call:invoke"] == 1
    assert metrics.peak["execution:staging_attempt"] == (min(budget, 2) if ahead else 0)
    assert metrics.peak["execution:daily_attempt"] == 0
    assert not metrics.summary("completed")["active"]
    source = h.store.run("trial").read(f"targets/{h.catalog.targets[0].key}/source")
    snapshot = Snapshot.from_private_dict(h.store.run("trial").read_artifact(source["evidence_key"]))
    assert len(snapshot.attributable_responses) == 20
    assert h.store.run("trial").read_completed("staging-session-lookahead") == {"travel_sessions_ahead": ahead}
    assert h.store.run("trial").read_completed("daily-session-lookahead", missing_ok=True) is None
    saved = {p: p.read_bytes() for p in h.store.run("trial").directory.rglob("*.json")}
    audit = audit_run(h.store, "trial")
    assert audit["frozen_sessions_ahead"] == ahead
    row = audit["units"][0]
    assert row["trial_policy_applies"] == bool(ahead)
    assert row["ready_sessions"] == row["attributable_probe_attempts"] == 10
    assert row["matching_version_affinities"] == (10 if ahead else 0)
    assert row["completed_turns"] == row["planned_turns"] == 20
    assert row["planned_turns_with_call_observation"] == 20
    assert row["sessions_distinct_per_attempt"] and row["ordered_completed_receipts"]
    assert row["turns_bound_to_attempt_session"]
    assert row["observed_business_peak"] == 1
    assert bool(row["observed_preparation_invoke_overlap_seconds"]) == bool(ahead and budget > 1)
    assert audit["performance_segments_complete"]
    output = json.dumps(audit)
    assert all(session not in output for session in sessions.values())
    assert all(c[2] not in output for c in h.cloud.invocations)
    assert all(p.read_bytes() == value for p, value in saved.items())


@pytest.mark.parametrize("budget", [1, 2, 10])
def test_trial_whole_attempt_budget_includes_serial_other_agents_and_separate_versions(tmp_path, budget):
    h = fake.Harness(tmp_path, profile="staging", agents=3, issues=1, hosted=True,
                     daily_attempt_budget=budget, staging_travel_session_lookahead=1)
    first = h.catalog.agents[0]
    prompt = h.catalog.agents[1]
    h.catalog = replace(h.catalog, agents=("travel-agent", *h.catalog.agents[1:]), targets=tuple(
        replace(t, unit_id=UnitId("travel-agent", t.unit_id.logical_version))
        if t.unit_id.agent == first else replace(t, agent_type="prompt")
        if t.unit_id.agent == prompt else t for t in h.catalog.targets
    ))
    active = set()
    invoke = h.cloud.invoke
    async def business(deployment, step, **kwargs):
        assert deployment.agent_name not in active
        active.add(deployment.agent_name)
        assert len(active) <= budget
        assert h.cloud.active[deployment.agent_name] == deployment
        try:
            return await invoke(deployment, step, **kwargs)
        finally:
            active.remove(deployment.agent_name)
    h.cloud.invoke = business
    with h.store.ownership():
        metrics = RunMetrics(h.store.run("trial"))
        runner = h.runner(metrics=metrics)
        runner.initialize(h.catalog.targets, fake.DAY, kind="staging")
        try:
            result = asyncio.run(asyncio.wait_for(run_profile(h, runner), 10))
            assert runner.attempt_limit._value == budget
        finally:
            runner.logger.close()
    assert len(result["results"]) == 6 and all(r["status"] == "PASS" for r in result["results"])
    assert metrics.peak["execution:staging_attempt"] <= budget
    assert len(h.cloud.invocations) == 120
    assert len({call[3] for call in h.cloud.invocations if call[3] is not None}) == 40
    assert len({call[0].agent_name for call in h.cloud.invocations}) == 6
    for target in h.catalog.targets:
        calls = [c for c in h.cloud.invocations if c[0].target_key == target.key]
        assert [c[1].phase for c in calls] == ["setup", "probe"] * 10
        assert len({c[0].provider_version for c in calls}) == 1
        for offset, call in enumerate(calls):
            assert call[4] == (
                "response-" + calls[offset - 1][2] if target.is_prompt and offset % 2 else None
            )
            assert (call[3] is None) == target.is_prompt
    phases = [o for o in metrics.observations if o["kind"] == "attempt_phase"]
    assert Counter(o["name"] for o in phases) == {"session_preparation": 20, "business_execution": 20}
    assert all(o["unit"].startswith("travel-agent/") for o in phases)
    assert not h.cloud.starts


def test_trial_policy_and_completed_phases_resume_without_resampling(tmp_path):
    h = harness(tmp_path, profile="staging", staging_ahead=1)
    first = h.staging("trial")
    records = h.store.run("trial")
    saved = {p: p.read_bytes() for p in records.directory.joinpath("completed").rglob("*.json")}
    counts = len(h.cloud.invocations), len(h.sol.calls), Counter(e[0] for e in h.cloud.events)
    h.settings = replace(h.settings, staging_travel_session_lookahead=0)
    second = h.staging("trial")
    assert second["results"] == json.loads(json.dumps(first["results"]))
    assert second["session_preparation_trial"]["frozen"] == 1
    assert second["session_preparation_trial"]["requested"] == 0
    assert (len(h.cloud.invocations), len(h.sol.calls), Counter(e[0] for e in h.cloud.events)) == counts
    assert all(p.read_bytes() == value for p, value in saved.items())


def test_legacy_or_disabled_staging_resume_cannot_be_relabelled_as_trial(tmp_path):
    h = harness(tmp_path, profile="staging")
    h.staging("trial")
    records = h.store.run("trial")
    records._path("completed", "staging-session-lookahead").unlink()
    h.settings = replace(h.settings, staging_travel_session_lookahead=1)
    result = h.staging("trial")
    assert result["session_preparation_trial"] == {
        "requested": 1, "frozen": 0, "current_run_travel_units": [], "acceptance": "not_exercised",
    }
    assert len(h.cloud.invocations) == 20
    assert not list(records.directory.rglob("session-affinity.json"))
    assert audit_run(h.store, "trial")["current_run_trial_units"] == 0


@pytest.mark.parametrize("reason", ["reassess", "incomplete"])
def test_requested_trial_rejects_inherited_traffic_before_provider_calls(tmp_path, reason):
    h = harness(tmp_path, profile="staging")
    h.staging("old")
    old = h.store.run("old")
    saved = {p: p.read_bytes() for p in old.directory.rglob("*.json")}
    counts = len(h.cloud.invocations), len(h.sol.calls), len(h.cloud.events)
    h.settings = replace(h.settings, staging_travel_session_lookahead=1)
    selection = Selection(h.catalog.targets[0], "reassess" if reason == "reassess" else "traffic", (reason,))
    with pytest.raises(QualityError, match="staging_session_trial_requires_fresh_travel"):
        h.staging("next", selections=(selection,), revision="source-two")
    assert (len(h.cloud.invocations), len(h.sol.calls), len(h.cloud.events)) == counts
    assert all(p.read_bytes() == value for p, value in saved.items())
    assert not list(old.directory.rglob("session-affinity.json"))
    audit = audit_run(h.store, "next")
    assert audit["current_run_trial_units"] == 0
    assert audit["units"][0]["traffic_owned_by_this_run"] is False
    assert audit["units"][0]["observed_preparation_invoke_overlap_seconds"] is None


@pytest.mark.parametrize("hosted,agent", [(False, "travel-agent"), (True, "finance-agent")])
def test_trial_without_fresh_hosted_travel_is_explicit_error(tmp_path, hosted, agent):
    h = harness(tmp_path, profile="staging", staging_ahead=1, hosted=hosted, agent=agent)
    with pytest.raises(QualityError, match="staging_session_trial_requires_fresh_travel"):
        h.staging("trial")
    assert not h.cloud.events and not h.sol.calls


def test_mixed_selection_keeps_inherited_unit_old_and_prepares_only_fresh_travel(tmp_path):
    h = harness(tmp_path, profile="staging", issues=1)
    h.staging("old")
    h.settings = replace(h.settings, staging_travel_session_lookahead=1)
    old, fresh = h.catalog.targets
    selections = (Selection(old, "reassess", ("evaluation_changed",)),
                  Selection(fresh, "traffic", ("traffic_changed",)))
    result = h.staging("next", selections=selections, revision="source-two")
    assert result["session_preparation_trial"]["current_run_travel_units"] == [fresh.key]
    assert len(h.cloud.invocations) == 60
    assert h.store.run("next").read(f"targets/{old.key}/source")["traffic_run_id"] == "old"
    assert h.store.run("next").read(f"targets/{fresh.key}/source")["traffic_run_id"] == "next"
    assert not list(h.store.run("old").directory.rglob("session-affinity.json"))
    assert len(list(h.store.run("next").directory.rglob("session-affinity.json"))) == 10
    audit = audit_run(h.store, "next")
    assert audit["current_run_trial_units"] == 1
    assert not audit["performance_segments_complete"]
    assert all(row["observed_business_peak"] is None for row in audit["units"])


def test_inherited_unfinished_serial_work_is_blocked_not_retagged_for_acceptance(tmp_path):
    h = harness(tmp_path, profile="staging")
    def interrupt(deployment, request_id, persist):
        if session_index(h, deployment, request_id, run_id="old") == 2:
            raise OSError("synthetic interrupted preparation")
        persist("synthetic-session-" + request_id)
        return "synthetic-session-" + request_id
    h.cloud.session_hook = interrupt
    with pytest.raises(OSError):
        run_traffic(h, "old")
    records = h.store.run("old")
    saved = {p: p.read_bytes() for p in records.directory.rglob("*.json")}
    counts = len(h.cloud.invocations), len(h.cloud.events)
    h.settings = replace(h.settings, staging_travel_session_lookahead=1)
    with pytest.raises(QualityError, match="staging_session_trial_requires_fresh_travel"):
        h.staging("next", revision="source-two", selections=(
            Selection(h.catalog.targets[0], "traffic", ("incomplete",)),
        ))
    assert (len(h.cloud.invocations), len(h.cloud.events)) == counts
    assert all(p.read_bytes() == value for p, value in saved.items())


def test_staging_flag_never_enables_daily_or_changes_its_policy_records(tmp_path):
    h = harness(tmp_path, ahead=0, staging_ahead=1)
    run_traffic(h)
    records = h.store.run("trial")
    assert records.read_completed("daily-session-lookahead") == {"travel_sessions_ahead": 0}
    assert records.read_completed("staging-session-lookahead", missing_ok=True) is None
    assert not list(records.directory.rglob("session-affinity.json"))
    assert [e[0] for e in h.cloud.events if e[0] in {"session", "invoke"}] == ["session", "invoke", "invoke"] * 10


@pytest.mark.parametrize("gap", ["truncated", "unfinished", "resume"])
def test_audit_discloses_metrics_gaps_and_deduplicates_batches_and_resumed_phases(tmp_path, gap):
    h = harness(tmp_path, profile="staging", staging_ahead=1)
    with h.store.ownership():
        metrics = RunMetrics(h.store.run("trial"), max_records=3 if gap == "truncated" else 20_000,
                             checkpoint_every=1)
    h.staging("trial", metrics=metrics)
    if gap != "unfinished":
        with h.store.ownership():
            metrics.finalize()
    if gap == "resume":
        with h.store.ownership():
            resumed = RunMetrics(h.store.run("trial"), checkpoint_every=1)
        h.staging("trial", metrics=resumed)
        with h.store.ownership():
            resumed.finalize()
    audit = audit_run(h.store, "trial")
    assert audit["performance_segments_complete"] == (gap == "resume")
    assert audit["performance_segments"] == (2 if gap == "resume" else 1)
    row = audit["units"][0]
    if gap == "resume":
        assert row["measured_calls"]["create_session_calls"] == 10
        assert row["measured_calls"]["invocation_calls"] == row["planned_turns_with_call_observation"] == 20
    if gap == "truncated":
        assert row["planned_turns_with_call_observation"] < row["completed_turns"]
    assert len(h.cloud.invocations) == 20
    assert audit["acceptance"] == "requires_qualification_and_native_continuation_review"


@pytest.mark.parametrize("damage", ["session", "affinity", "receipt_order"])
def test_audit_reports_contract_disagreements_without_changing_qualification(tmp_path, damage):
    h = harness(tmp_path, profile="staging", staging_ahead=1)
    h.staging("trial")
    records = h.store.run("trial")
    key = work_key(h, h.catalog.targets[0]) + "/traffic/"
    if damage == "session":
        path = key + "attempt-02/session"
        value = records.read_completed(path)
        value["session_id"] = records.read_completed(key + "attempt-01/session")["session_id"]
    elif damage == "affinity":
        path = key + "attempt-02/session-affinity"
        value = records.read_completed(path)
        value["provider_version"] = "synthetic-wrong-version"
    else:
        path = key + "attempt-02/setup"
        value = records.read_completed(path)
        value["started_at"] = "2020-01-01T00:00:00+00:00"
    records._path("completed", path).write_text(json.dumps(value))
    row = audit_run(h.store, "trial")["units"][0]
    if damage == "session":
        assert not row["sessions_distinct_per_attempt"] and not row["turns_bound_to_attempt_session"]
    elif damage == "affinity":
        assert row["matching_version_affinities"] == 9
    else:
        assert not row["ordered_completed_receipts"]
    assert row["qualification_status"] == "PASS"
    assert len(h.cloud.invocations) == 20 and len(h.sol.calls) == 1


def test_audit_cli_is_read_only_and_uses_staging_without_extra_commands(tmp_path, monkeypatch, capsys):
    from agent_insights_quality import preparation_audit
    h = harness(tmp_path, profile="staging", staging_ahead=1)
    h.staging("trial")
    def forbidden(*args, **kwargs):
        pytest.fail("Audit attempted ownership or a write")
    monkeypatch.setattr(RuntimeStore, "ownership", forbidden)
    for method in ("save_completed", "save_progress", "save_artifact"):
        monkeypatch.setattr(RecordStore, method, forbidden)
    def store(profile):
        assert profile == "staging"
        return h.store
    monkeypatch.setattr(preparation_audit, "RuntimeStore", store)
    monkeypatch.setattr(sys, "argv", ["preparation-audit", "--run-id", "trial"])
    assert preparation_audit.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["frozen_sessions_ahead"] == 1 and result["current_run_trial_units"] == 1
    assert not result["performance_segments_complete"]


@pytest.mark.parametrize("completed", [False, True])
def test_staging_cli_empty_selection_cannot_claim_requested_trial(app, monkeypatch, capsys, completed):
    from agent_insights_quality import runner
    monkeypatch.setattr(runner, "choose_staging", lambda *args, **kwargs: ())
    if completed:
        assert app.cli("run-staging") == 0
        capsys.readouterr()
    config = app.catalog.root / "config"
    config.mkdir(parents=True)
    (config / "runtime.json").write_text('{"staging_travel_session_lookahead":1}')
    assert app.cli("run-staging") == 2
    assert ("staging_session_trial_frozen_off" if completed
            else "staging_session_trial_requires_fresh_travel") in capsys.readouterr().err
    assert not app.port_calls and not app.cloud.invocations


def test_staging_cli_completed_real_trial_keeps_frozen_policy_and_does_not_replay(app, monkeypatch, capsys):
    from agent_insights_quality import runner
    app.cloud.environment = replace(app.cloud.environment, profile="staging")
    target = replace(app.catalog.targets[0], unit_id=UnitId("travel-agent", "v0"), agent_type="hosted_code")
    app.catalog = replace(app.catalog, targets=(target,), agents=("travel-agent",))
    monkeypatch.setattr(runner, "choose_staging", lambda *args, **kwargs: (Selection(target, "traffic", ("missing",)),))
    config = app.catalog.root / "config"
    config.mkdir(parents=True)
    (config / "runtime.json").write_text('{"staging_travel_session_lookahead":1,"hydration_seconds":0}')
    assert app.cli("run-staging") == 0
    capsys.readouterr()
    calls = len(app.port_calls), len(app.cloud.invocations), len(app.sol.calls)
    assert app.cli("run-staging") == 0
    assert (len(app.port_calls), len(app.cloud.invocations), len(app.sol.calls)) == calls
    (config / "runtime.json").write_text("{}")
    assert app.cli("run-staging") == 0
    assert (len(app.port_calls), len(app.cloud.invocations), len(app.sol.calls)) == calls
