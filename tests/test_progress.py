from __future__ import annotations

import time

from agent_insights_quality.progress import ProgressReporter


def test_progress_output_failure_never_aborts_operation(monkeypatch) -> None:
    def fail_print(*_args, **_kwargs):
        raise BrokenPipeError("synthetic closed output")

    monkeypatch.setattr("builtins.print", fail_print)
    reporter = ProgressReporter("test")
    reporter.emit("first")
    reporter.emit("second")


def test_heartbeat_reports_start_activity_and_finish(monkeypatch) -> None:
    messages = []

    def capture(*args, **_kwargs):
        messages.append(str(args[0]))

    monkeypatch.setattr("builtins.print", capture)
    reporter = ProgressReporter("test")
    with reporter.heartbeat("synthetic operation", interval_seconds=0.01):
        time.sleep(0.03)
    assert any("synthetic operation: started" in value for value in messages)
    assert any("synthetic operation: still running" in value for value in messages)
    assert any("synthetic operation: finished" in value for value in messages)


def test_heartbeat_setup_failure_does_not_block_operation(monkeypatch) -> None:
    executed = False
    monkeypatch.setattr(
        "agent_insights_quality.progress.threading.Thread.start",
        lambda _thread: (_ for _ in ()).throw(RuntimeError("synthetic thread failure")),
    )
    reporter = ProgressReporter("test")
    with reporter.heartbeat("synthetic operation"):
        executed = True
    assert executed is True


def test_heartbeat_outcome_can_report_failed_return_code(monkeypatch) -> None:
    messages = []
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **_kwargs: messages.append(str(args[0])),
    )
    reporter = ProgressReporter("test")
    with reporter.heartbeat("synthetic command") as outcome:
        outcome.fail()
    assert any("synthetic command: failed" in value for value in messages)
    assert not any("synthetic command: finished" in value for value in messages)
