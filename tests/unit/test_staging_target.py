from datetime import date, timedelta
import json
from pathlib import Path

import pytest

from agent_insights_quality import cli, runner, staging_target
from agent_insights_quality.catalogs import load_catalog
from agent_insights_quality.runner import choose_staging
from agent_insights_quality.state import RecordStore, RuntimeStore
from test_cli import app as app, last_json
import test_runner as fake


def staging(app, monkeypatch):
    app.cloud.environment = fake.replace(app.cloud.environment, profile="staging")
    monkeypatch.setattr(runner, "choose_staging", lambda catalog, store, full=False: tuple(
        runner.Selection(target, "traffic", ("full",) if full else ("missing",))
        for target in catalog.targets
    ))
    return RuntimeStore("staging", root=app.store.root), app.catalog.targets[1]


@pytest.mark.parametrize("arguments", [
    ("--target", "issue-001", "--new-run"),
    ("--target", "synthetic-0-agent", "--new-run"),
    ("--target", "synthetic-0-agent/*", "--new-run"),
    ("--target", "synthetic-0-agent/issue-999", "--new-run"),
    ("--target", "../issue-001", "--new-run"),
    ("--target", "synthetic-0-agent/issue-001", "--full", "--new-run"),
    ("--target", "synthetic-0-agent/issue-001", "--target", "synthetic-0-agent/issue-001", "--new-run"),
    ("--target", "synthetic-0-agent/issue-001", "--target", "synthetic-0-agent/v0", "--new-run"),
    ("--target", "synthetic-0-agent/issue-001"),
    ("--target", "synthetic-0-agent/issue-001", "--after-run", "unrecognized"),
    ("--target", "synthetic-0-agent/issue-001", "--new-run", "--after-run", "unrecognized"),
    ("--after-run", "unrecognized", "--full", "--new-run"),
    ("--new-run",),
])
def test_target_arguments_fail_before_live_ports(app, capsys, arguments):
    assert app.cli("run-staging", *arguments) == 2
    assert "staging_" in capsys.readouterr().err
    assert not app.port_calls and not app.cloud.invocations


def test_real_catalog_single_target_is_canonical_and_duplicate_catalog_is_rejected():
    catalog = load_catalog(Path(__file__).resolve().parents[2])
    args = cli.parser().parse_args(["run-staging", "--target", "finance-agent/issue-019", "--new-run"])
    target = staging_target.argument_target(args, catalog)
    assert target.key == "finance-agent/issue-019" and len(catalog.targets) == 41
    assert staging_target.selection(target) == (runner.Selection(target, "traffic", ("fresh_target",)),)
    with pytest.raises(fake.QualityError, match="staging_target_invalid"):
        staging_target.argument_target(args, fake.replace(catalog, targets=(*catalog.targets, target)))


def test_target_fresh_measurement_preserves_old_incomplete_and_other_targets(app, monkeypatch, capsys):
    store, target = staging(app, monkeypatch)
    app.cloud.ready_count = 7
    assert app.cli("run-staging", "--full") == 2
    previous, _ = last_json(capsys)
    old = store.staging_index.read(target.key)
    assert old["status"] == "INCOMPLETE" and old["result"]["passing_attempts"] == 7
    untouched = {item.key: store.staging_index.read(item.key) for item in app.catalog.targets if item != target}
    old_files = {
        path: path.read_bytes() for directory in ("completed", "artifacts")
        for path in (store.run(previous["run_id"]).directory / directory).rglob("*.json")
    }
    full_control = store.outbox("staging").read("full")
    old_count = len(app.cloud.invocations)
    app.cloud.ready_count = 10
    app.clock.value += timedelta(seconds=1)
    assert app.cli("run-staging", "--target", target.key, "--new-run") == 0
    result, _ = last_json(capsys)
    assert result["scope"] == "single-target" and result["selected"] == 1 and result["target"] == target.key
    assert result["statuses"] == {"PASS": 1, "FAIL": 0, "INCOMPLETE": 0}
    records = store.run(result["run_id"])
    fresh = store.staging_index.read(target.key)
    assert fresh["work_key"] != old["work_key"] and fresh["traffic_run_id"] == result["run_id"]
    assert fresh["result"]["passing_attempts"] == 10
    assert len(app.cloud.invocations) == old_count + 20
    assert {call[0].target_key for call in app.cloud.invocations[old_count:]} == {target.key}
    assert records.read_completed("run")["targets"] == [target.key]
    assert records.read_completed("selection")["targets"] == [
        {"key": target.key, "action": "traffic", "reasons": ["fresh_target"]},
    ]
    assert len(records.read_completed(fresh["work_key"] + "/plan")["attempts"]) == 10
    assert records.read_completed(staging_target.INTENT)["prior"] == staging_target.reference(old)
    assert all(path.read_bytes() == content for path, content in old_files.items())
    assert all(store.staging_index.read(key) == value for key, value in untouched.items())
    assert store.outbox("staging").read("full") == full_control
    assert not app.cloud.starts and not store.outbox("events").directory.exists()


@pytest.mark.parametrize("outcome", ["PASS", "FAIL", "INCOMPLETE"])
def test_repeat_targeted_request_never_resamples_any_final_judgment(app, monkeypatch, capsys, outcome):
    store, target = staging(app, monkeypatch)
    if outcome == "INCOMPLETE":
        app.cloud.ready_count = 7
    elif outcome == "FAIL":
        complete = app.sol.complete_json
        async def missed(**kwargs):
            result = await complete(**kwargs)
            for item in result["attempts"]:
                item["observed"] = False
            return result
        app.sol.complete_json = missed
    args = ("run-staging", "--target", target.key, "--new-run")
    expected = 2 if outcome == "INCOMPLETE" else 0
    assert app.cli(*args) == expected
    first, _ = last_json(capsys)
    assert first["statuses"][outcome] == 1 and first["measurement_terminal"]
    calls = len(app.cloud.invocations), len(app.sol.calls), len(app.port_calls)
    for arguments in (args, ("run-staging", "--target", target.key)):
        assert app.cli(*arguments, day=date(2026, 9, 5)) == expected
        resumed, _ = last_json(capsys)
        assert resumed["run_id"] == first["run_id"] and resumed["report_date"] == fake.DAY.isoformat()
    assert (len(app.cloud.invocations), len(app.sol.calls), len(app.port_calls)) == calls
    app.clock.value += timedelta(seconds=1)
    next_args = (*args, "--after-run", first["run_id"])
    assert app.cli(*next_args, day=date(2026, 9, 5)) == expected
    second, _ = last_json(capsys)
    assert second["run_id"] != first["run_id"] and len(app.cloud.invocations) == 40
    assert app.cli(*next_args, day=date(2026, 9, 6)) == expected
    assert last_json(capsys)[0]["run_id"] == second["run_id"] and len(app.cloud.invocations) == 40
    assert app.cli(*args) == 2
    assert "staging_target_request_conflict" in capsys.readouterr().err
    assert store.run(first["run_id"]).read("targets/" + target.key + "/source")["traffic_run_id"] == first["run_id"]


def test_target_intent_is_frozen_before_provider_startup_and_resume_is_exact(app, monkeypatch, capsys):
    store, target = staging(app, monkeypatch)
    original = RecordStore.save_completed
    def interrupted(records, key, value):
        if key == "run":
            intent = records.read_completed(staging_target.INTENT)
            assert intent["target"] == target.key and intent["source_revision"] == "a" * 40
            assert intent["report_date"] == fake.DAY.isoformat()
            assert store.outbox("staging").read("targeted")["run_id"] == intent["run_id"]
            raise RuntimeError("startup interruption")
        return original(records, key, value)
    monkeypatch.setattr(RecordStore, "save_completed", interrupted)
    args = ("run-staging", "--target", target.key, "--new-run")
    with pytest.raises(RuntimeError, match="startup interruption"):
        app.cli(*args)
    active = store.outbox("staging").read("targeted")
    intent = store.run(active["run_id"]).read_completed(staging_target.INTENT)
    assert not app.cloud.invocations
    monkeypatch.setattr(RecordStore, "save_completed", original)
    assert app.cli(*args, "--after-run", active["run_id"]) == 2
    assert "staging_target_prior_unfinished" in capsys.readouterr().err
    monkeypatch.setattr(runner, "source_revision", lambda _: "b" * 40)
    assert app.cli(*args) == 2
    assert "staging_target_resume_source_changed" in capsys.readouterr().err
    assert len(app.port_calls) == 1
    monkeypatch.setattr(runner, "source_revision", lambda _: "a" * 40)
    assert app.cli(*args, day=date(2026, 9, 5)) == 0
    result, _ = last_json(capsys)
    assert result["run_id"] == active["run_id"] and result["report_date"] == fake.DAY.isoformat()
    assert store.staging_index.read(target.key)["work_key"] == intent["work_key"]
    assert len(app.cloud.invocations) == 20


@pytest.mark.parametrize("damage", ["date", "target", "selection", "source", "work"])
def test_target_resume_conflicts_fail_before_providers(app, monkeypatch, capsys, damage):
    store, target = staging(app, monkeypatch)
    args = ("run-staging", "--target", target.key, "--new-run")
    assert app.cli(*args) == 0
    value, _ = last_json(capsys)
    records = store.run(value["run_id"])
    active = store.outbox("staging").read("targeted")
    if damage == "date":
        with store.ownership():
            store.outbox("staging").save_progress("targeted", {**active, "report_date": "2026-09-05"})
    elif damage == "target":
        args = ("run-staging", "--target", app.catalog.targets[0].key, "--new-run")
    elif damage == "source":
        monkeypatch.setattr(runner, "source_revision", lambda _: "b" * 40)
    elif damage == "work":
        with store.ownership():
            bound = records.read(f"targets/{target.key}/source")
            records.save_progress(f"targets/{target.key}/source", {**bound, "work_key": "targets/" + target.key + "/work-wrong"})
    else:
        records._path("completed", "selection").write_text(json.dumps({"targets": []}))
    count = len(app.port_calls)
    assert app.cli(*args) == 2
    assert "staging_" in capsys.readouterr().err and len(app.port_calls) == count


@pytest.mark.parametrize("unresolved", ["create", "session", "invoke", "insights", "assessment", "unbound"])
def test_retake_refuses_unresolved_prior_operations(app, monkeypatch, capsys, unresolved):
    store, target = staging(app, monkeypatch)
    assert app.cli("run-staging") == 0
    value, _ = last_json(capsys)
    records = store.run(value["run_id"])
    bound = store.staging_index.read(target.key)
    key = bound["work_key"]
    with store.ownership():
        if unresolved == "create":
            saved = records.read(key + "/deployment")
            records.save_progress(key + "/deployment", {**saved, "details": {"provisioning_state": "submitting"}})
        elif unresolved == "session":
            records.save_progress(key + "/traffic/attempt-01/session", {"status": "unknown"})
        elif unresolved == "invoke":
            path = records._path("completed", key + "/traffic/attempt-01/probe")
            saved = json.loads(path.read_text())
            path.write_text(json.dumps({**saved, "status": "unknown"}))
        elif unresolved == "insights":
            records.save_progress(key + "/insights/start", {"status": "submitting"})
        elif unresolved == "assessment":
            saved = records.read(f"targets/{target.key}/assessment")
            records.save_progress(f"targets/{target.key}/assessment", {**saved, "status": "pending"})
        else:
            # Startup can publish its immutable selection before creating a binding.
            pending = store.run("selected-unbound")
            pending.save_completed("selection", {"targets": [{"key": target.key}]})
            store.outbox("staging").save_progress("full", {
                "run_id": "selected-unbound", "source_revision": "a" * 40,
                "report_date": fake.DAY.isoformat(), "full": True, "completed": False,
            })
    count = len(app.port_calls), len(app.cloud.invocations)
    assert app.cli("run-staging", "--target", target.key, "--new-run") == 2
    assert "staging_target_prior_unfinished" in capsys.readouterr().err
    assert (len(app.port_calls), len(app.cloud.invocations)) == count
    assert store.outbox("staging").read("targeted", missing_ok=True) is None


def test_target_request_does_not_displace_active_writer(app, monkeypatch, capsys):
    store, target = staging(app, monkeypatch)
    with store.ownership():
        assert app.cli("run-staging", "--target", target.key, "--new-run") == 2
    assert "state_owned" in capsys.readouterr().err
    assert not app.port_calls and store.outbox("staging").read("targeted", missing_ok=True) is None


@pytest.mark.parametrize("code,terminal", [
    ("invocation_response_incomplete", True),
    ("invocation_response_pending", False),
    (None, False),
])
def test_native_terminal_incomplete_is_not_a_pending_response(app, monkeypatch, capsys, code, terminal):
    store, target = staging(app, monkeypatch)
    assert app.cli("run-staging") == 0
    value, _ = last_json(capsys)
    bound = store.staging_index.read(target.key)
    path = store.run(value["run_id"])._path("completed", bound["work_key"] + "/traffic/attempt-01/probe")
    receipt = json.loads(path.read_text())
    path.write_text(json.dumps({**receipt, "status": "incomplete", "error_code": code}))
    assert staging_target.measurement_terminal(store, target, bound) is terminal
    calls = len(app.cloud.invocations)
    app.clock.value += timedelta(seconds=1)
    assert app.cli("run-staging", "--target", target.key, "--new-run") == (0 if terminal else 2)
    if terminal:
        assert len(app.cloud.invocations) == calls + 20
    else:
        assert "staging_target_prior_unfinished" in capsys.readouterr().err
        assert len(app.cloud.invocations) == calls


def test_unresolved_targeted_request_resumes_without_allocating_more_traffic(app, monkeypatch, capsys):
    store, target = staging(app, monkeypatch)
    app.cloud.ready_count = 7
    def unknown(deployment, step, request_id, session, persist):
        if step.body["input"] == "synthetic probe 1":
            return fake.Invocation(
                request_id, None, session, app.clock.now().isoformat(),
                app.clock.now().isoformat(), "unknown", error_code="invocation_outcome_unresolved",
            )
    app.cloud.invoke_hook = unknown
    args = ("run-staging", "--target", target.key, "--new-run")
    assert app.cli(*args) == 2
    first, _ = last_json(capsys)
    assert not first["measurement_terminal"]
    calls = len(app.cloud.invocations), len(app.sol.calls)
    identity = store.run(first["run_id"]).read_completed(staging_target.INTENT)
    assert app.cli(*args, "--after-run", first["run_id"]) == 2
    assert "staging_target_prior_unfinished" in capsys.readouterr().err
    assert app.cli(*args, day=date(2026, 9, 5)) == 2
    second, _ = last_json(capsys)
    assert second["run_id"] == first["run_id"]
    assert store.run(second["run_id"]).read_completed(staging_target.INTENT) == identity
    assert (len(app.cloud.invocations), len(app.sol.calls)) == calls


@pytest.mark.parametrize("failure", ["setup", "session"])
def test_definitively_rejected_steps_and_unsubmitted_continuations_are_terminal(app, monkeypatch, capsys, failure):
    if failure == "session":
        app.catalog = fake.replace(app.catalog, targets=tuple(
            fake.replace(target, agent_type="hosted_code") for target in app.catalog.targets
        ))
    store, target = staging(app, monkeypatch)
    rejections = 0
    def reject(*args):
        nonlocal rejections
        rejections += 1
        if rejections <= 3:
            raise fake.QualityError("synthetic_definitive_rejection", request_accepted=False)
    if failure == "session":
        def session(deployment, request_id, persist):
            reject()
            persist("session-" + request_id)
            return "session-" + request_id
        app.cloud.session_hook = session
    else:
        def invoke(deployment, step, request_id, session, persist):
            if step.phase == "setup":
                reject()
        app.cloud.invoke_hook = invoke
    args = ("run-staging", "--target", target.key, "--new-run")
    assert app.cli(*args) == 2
    first, _ = last_json(capsys)
    assert first["measurement_terminal"] and first["statuses"]["INCOMPLETE"] == 1
    bound = store.staging_index.read(target.key)
    assert store.run(first["run_id"]).read_completed(bound["work_key"] + "/traffic-done", missing_ok=True) is None
    calls = len(app.cloud.invocations), len(app.sol.calls), len(app.port_calls)
    assert app.cli(*args) == 2
    last_json(capsys)
    assert (len(app.cloud.invocations), len(app.sol.calls), len(app.port_calls)) == calls
    app.clock.value += timedelta(seconds=1)
    assert app.cli(*args, "--after-run", first["run_id"]) == 0
    second, _ = last_json(capsys)
    assert second["run_id"] != first["run_id"] and len(app.cloud.invocations) == calls[0] + 20


def test_targeted_history_remains_visible_to_incremental_and_supersession(app, monkeypatch, capsys):
    store, target = staging(app, monkeypatch)
    assert app.cli("run-staging", "--target", target.key, "--new-run") == 0
    first, _ = last_json(capsys)
    index = store.staging_index.read(target.key)
    store.staging_index._path("progress", target.key).unlink()
    store.outbox("staging")._path("progress", f"targets/{target.key}").unlink()
    with store.ownership():
        runner.reconcile_staging_work(app.catalog, store)
    assert store.outbox("staging").read(f"targets/{target.key}") == staging_target.reference(index)
    choices = choose_staging(app.catalog, store, changes_since=lambda _: runner.SourceChanges(()))
    assert target.key not in [item.target.key for item in choices]
    app.clock.value += timedelta(seconds=1)
    assert app.cli("run-staging", "--full") == 0
    last_json(capsys)
    count = len(app.port_calls)
    assert app.cli("run-staging", "--target", target.key, "--new-run") == 2
    assert "staging_target_superseded" in capsys.readouterr().err and len(app.port_calls) == count
    assert store.run(first["run_id"]).read("staging-result")["selected"] == 1
