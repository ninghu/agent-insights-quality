import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import io
import json

import pytest

from agent_insights_quality import events, state
from agent_insights_quality.errors import QualityError
from agent_insights_quality.events import RunLogger, project_event
from agent_insights_quality.results import UnitId
from agent_insights_quality.state import CheckpointError, RuntimeStore


UNIT = UnitId("weather", "v0")
NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def logger(directory, **kwargs):
    return RunLogger(
        directory, allowed_units=(UNIT,), clock=lambda: NOW,
        monotonic=lambda: 10.0, console=kwargs.pop("console", io.StringIO()),
        stderr=kwargs.pop("stderr", io.StringIO()), **kwargs,
    )


def read_events(directory):
    return [
        json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()
    ]


def test_restart_appends_both_logs_and_emits_safe_console(tmp_path):
    console = io.StringIO()
    with logger(tmp_path, console=console) as first:
        assert first.emit("started")
    with logger(tmp_path, console=console) as resumed:
        assert resumed.emit("resume", stage="traffic", unit=UNIT, attempt=1, turn=1)
        assert resumed.health_warnings == ()
    rows = read_events(tmp_path)
    assert [row["kind"] for row in rows] == ["started", "resume"]
    assert rows[1]["utc"] == "2026-09-05T12:00:00.000Z"
    assert rows[1]["elapsed_ms"] == 0
    assert rows[1]["agent"] == "weather"
    assert rows[1]["logical_version"] == "v0"
    assert len((tmp_path / "runner.log").read_text().splitlines()) == 2
    assert "resume traffic ok" in console.getvalue()


def test_interrupted_log_tail_is_preserved_without_corrupting_next_event(tmp_path):
    truncated = '{"kind":"started"'
    (tmp_path / "events.jsonl").write_text(truncated, encoding="utf-8")
    log = logger(tmp_path)
    assert log.emit("resume")
    assert read_events(tmp_path)[0]["kind"] == "resume"
    assert (tmp_path / "events.jsonl.1").read_text() == truncated
    assert log.health_warnings == ("logging_tail_incomplete",)


def test_all_lifecycle_events_and_fake_elapsed_time(tmp_path):
    ticks = iter([10.0, 11.25, 12.0])
    log = RunLogger(
        tmp_path, clock=lambda: NOW, monotonic=lambda: next(ticks),
        console=io.StringIO(), stderr=io.StringIO(),
    )
    assert log.emit("heartbeat", counters={"completed_count": 2})
    assert log.emit("retry", code="rate_limited", counters={"retry_count": 1})
    assert [row["elapsed_ms"] for row in read_events(tmp_path)] == [1250, 2000]
    other = logger(tmp_path)
    for kind in ("checkpoint", "completed", "failure", "warning"):
        assert other.emit(kind)


def test_threaded_instances_produce_complete_noninterleaved_json(tmp_path):
    first, second = logger(tmp_path), logger(tmp_path)

    def append(index):
        return (first if index % 2 else second).emit(
            "heartbeat", counters={"completed_count": index},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(append, range(100)))
    records = read_events(tmp_path)
    assert sorted(record["counters"]["completed_count"] for record in records) == list(range(100))
    assert len((tmp_path / "runner.log").read_text().splitlines()) == 100


def test_async_callers_can_append_without_awaiting_a_lock(tmp_path):
    log = logger(tmp_path)

    async def append(index):
        return log.emit("heartbeat", counters={"completed_count": index})

    async def run():
        return await asyncio.gather(*(append(index) for index in range(20)))

    assert all(asyncio.run(run()))
    assert len(read_events(tmp_path)) == 20


def test_rotation_retains_only_named_logs_not_evidence(tmp_path):
    evidence = tmp_path / "artifacts" / "raw.json"
    evidence.parent.mkdir()
    evidence.write_text('{"synthetic":"preserved"}', encoding="utf-8")
    unrelated = tmp_path / "events.jsonl.99"
    unrelated.write_text("preserved", encoding="utf-8")
    first = logger(tmp_path, max_bytes=1024, backup_count=2)
    second = logger(tmp_path, max_bytes=1024, backup_count=2)
    for index in range(40):
        assert (first if index % 2 else second).emit(
            "heartbeat", counters={"completed_count": index},
        )
    assert evidence.read_text() == '{"synthetic":"preserved"}'
    assert unrelated.read_text() == "preserved"
    assert (tmp_path / "events.jsonl.1").exists()
    assert (tmp_path / "events.jsonl.2").exists()
    assert not (tmp_path / "events.jsonl.3").exists()
    for filename in ("events.jsonl", "events.jsonl.1", "events.jsonl.2"):
        assert (tmp_path / filename).stat().st_size <= 1024
        for line in (tmp_path / filename).read_text().splitlines():
            assert json.loads(line)["kind"] == "heartbeat"


@pytest.mark.parametrize(
    "fields",
    [
        {"message": "synthetic-secret"}, {"exception": "https://synthetic.invalid"},
        {"provider_id": "synthetic-secret"}, {"payload": {"secret": "synthetic-secret"}},
        {"stage": "https://synthetic.invalid"}, {"code": "synthetic-secret"},
        {"code": []}, {"counters": {"provider_id": 1}},
        {"counters": {"retry_count": "synthetic-secret"}}, {"counters": {"retry_count": True}},
        {"counters": {"retry_count": -1}}, {"unit": "synthetic-secret"},
        {"unit": UnitId("synthetic-provider-name", "v0")}, {"attempt": 1},
        {"unit": UNIT, "attempt": 11}, {"unit": UNIT, "turn": False},
    ],
)
def test_unsafe_fields_rejected_without_echoing_content(tmp_path, fields):
    stderr, console, outbox = io.StringIO(), io.StringIO(), []
    log = logger(tmp_path, stderr=stderr, console=console, outbox=outbox.append)
    assert not log.emit("failure", **fields)
    assert log.health_warnings == ("logging_event_rejected",)
    assert stderr.getvalue() == "WARNING logging_event_rejected\n"
    assert console.getvalue() == ""
    assert not (tmp_path / "events.jsonl").exists()
    assert not outbox


def test_projection_revalidates_values_and_has_no_payloads(tmp_path):
    outbox = []
    log = logger(tmp_path, outbox=outbox.append)
    assert log.emit("completed", unit=UNIT, counters={"completed_count": 1})
    event = outbox[0]
    assert event == read_events(tmp_path)[0]
    for changed in (
        {**event, "url": "https://synthetic.invalid"},
        {**event, "agent": "synthetic-provider-name"},
        {**event, "code": "synthetic-secret"},
        {**event, "utc": "2026-09-05T12:00:00+05:00"},
        {**event, "elapsed_ms": True},
    ):
        with pytest.raises((TypeError, ValueError)):
            project_event(changed, allowed_units=(UNIT,))


def test_new_code_owned_quality_errors_are_preserved_in_all_sinks(tmp_path):
    outbox, console = [], io.StringIO()
    log = logger(tmp_path, outbox=outbox.append, console=console)
    error = QualityError("collector_scope_ambiguous")
    assert log.emit("failure", stage="evidence", code=error.code, unit=UNIT)
    assert log.health_warnings == ()
    assert read_events(tmp_path)[0]["code"] == error.code
    assert error.code in (tmp_path / "runner.log").read_text()
    assert error.code in console.getvalue()
    assert outbox[0]["code"] == error.code
    assert project_event(outbox[0], allowed_units=(UNIT,)) == outbox[0]


@pytest.mark.parametrize(
    "code", ["", "Uppercase", "with-dash", "with space", "a" * 81,
             "https://synthetic.invalid", "a\npayload", None, 42],
)
def test_error_codes_use_the_same_quality_error_contract(tmp_path, code):
    with pytest.raises(ValueError):
        QualityError(code)
    log = logger(tmp_path)
    assert not log.emit("failure", code=code)
    assert log.health_warnings == ("logging_event_rejected",)
    assert not (tmp_path / "events.jsonl").exists()


def test_logging_failure_is_visible_and_does_not_mask_checkpoint_failure(tmp_path, monkeypatch):
    stderr = io.StringIO()
    log = logger(tmp_path / "logs", stderr=stderr)

    def fail_open(handler):
        raise OSError("synthetic secret must never be logged")

    monkeypatch.setattr(events._DurableHandler, "_open", fail_open)
    assert not log.emit("started")
    assert log.health_warnings == ("logging_write_failed",)
    assert stderr.getvalue() == "WARNING logging_write_failed\n"
    with RuntimeStore("daily", root=tmp_path / "state").ownership() as runtime:
        runtime.run("synthetic-r1").save_completed("safe-work", {"done": True})

        def fail_checkpoint(source, destination):
            raise OSError("synthetic checkpoint failure")

        monkeypatch.setattr(state, "_replace", fail_checkpoint)
        with pytest.raises(CheckpointError):
            runtime.run("synthetic-r1").save_progress("next-step", {"started": True})
        assert runtime.run("synthetic-r1").read_completed("safe-work") == {"done": True}


def test_one_failed_log_does_not_suppress_the_other_or_console(tmp_path, monkeypatch):
    log = logger(tmp_path)
    original = events._DurableHandler._open

    def selective_failure(handler):
        if handler.baseFilename.endswith("runner.log"):
            raise OSError("synthetic")
        return original(handler)

    monkeypatch.setattr(events._DurableHandler, "_open", selective_failure)
    assert not log.emit("started")
    assert read_events(tmp_path)[0]["kind"] == "started"


def test_outbox_failure_is_independent_and_test_run_suppresses_callback(tmp_path):
    calls = []

    def fail_outbox(event):
        calls.append(event)
        raise CheckpointError()

    log = logger(tmp_path, outbox=fail_outbox)
    assert log.emit("started")
    assert log.health_warnings == ("logging_outbox_failed",)
    assert len(calls) == 1
    private_test = logger(tmp_path, outbox=fail_outbox, test_run=True)
    assert private_test.emit("resume")
    assert private_test.health_warnings == ()
    assert len(calls) == 1


def test_stderr_failure_remains_visible_in_health(tmp_path):
    class BrokenStream(io.StringIO):
        def write(self, message):
            raise OSError("synthetic")

    log = logger(tmp_path, stderr=BrokenStream())
    assert not log.emit("failure", message="synthetic-secret")
    assert set(log.health_warnings) == {"logging_event_rejected", "logging_stderr_failed"}


def test_backward_or_naive_clocks_reject_without_writing(tmp_path):
    log = RunLogger(
        tmp_path, clock=lambda: NOW.replace(tzinfo=None), monotonic=lambda: 10,
        console=io.StringIO(), stderr=io.StringIO(),
    )
    assert not log.emit("started")
    assert not (tmp_path / "events.jsonl").exists()
    ticks = iter([10.0, 9.0])
    backwards = RunLogger(
        tmp_path, clock=lambda: NOW, monotonic=lambda: next(ticks),
        console=io.StringIO(), stderr=io.StringIO(),
    )
    assert not backwards.emit("started")
    assert not (tmp_path / "events.jsonl").exists()
