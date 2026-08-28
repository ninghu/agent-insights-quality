from __future__ import annotations

import os
import subprocess
import sys
import time
import threading

from agent_insights_quality.progress import ProgressReporter
from agent_insights_quality.util import ROOT


def test_progress_output_failure_never_aborts_operation(monkeypatch) -> None:
    def fail_write(*_args, **_kwargs):
        raise BrokenPipeError("synthetic closed output")

    monkeypatch.setattr("agent_insights_quality.progress.os.write", fail_write)
    reporter = ProgressReporter("test")
    reporter.emit("first")
    reporter.emit("second")


def test_reporters_share_one_writer_thread() -> None:
    before = len(threading.enumerate())
    reporters = [ProgressReporter(f"test-{index}") for index in range(20)]
    assert len(reporters) == 20
    assert len(threading.enumerate()) == before


def test_blocked_output_never_blocks_operation(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()

    def block_write(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=1)

    monkeypatch.setattr("agent_insights_quality.progress.os.write", block_write)
    reporter = ProgressReporter("test")
    reporter.emit("first")
    assert entered.wait(timeout=1)
    started = time.monotonic()
    reporter.emit("second")
    assert time.monotonic() - started < 0.1
    release.set()


def test_heartbeat_reports_start_activity_and_finish(monkeypatch) -> None:
    messages = []

    def capture(_descriptor, value):
        messages.append(value.decode("utf-8"))
        return len(value)

    monkeypatch.setattr("agent_insights_quality.progress.os.write", capture)
    reporter = ProgressReporter("test")
    with reporter.heartbeat("synthetic operation", interval_seconds=0.01):
        time.sleep(0.03)
    time.sleep(0.02)
    assert any("synthetic operation: started" in value for value in messages)
    assert any("synthetic operation: still running" in value for value in messages)
    assert any("synthetic operation: finished" in value for value in messages)


def test_heartbeat_setup_failure_does_not_block_operation(monkeypatch) -> None:
    executed = False
    monkeypatch.setattr(
        "agent_insights_quality.progress.os.write",
        lambda _descriptor, value: len(value),
    )
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
        "agent_insights_quality.progress.os.write",
        lambda _descriptor, value: messages.append(value.decode("utf-8"))
        or len(value),
    )
    reporter = ProgressReporter("test")
    with reporter.heartbeat("synthetic command") as outcome:
        outcome.fail()
    time.sleep(0.02)
    assert any("synthetic command: failed" in value for value in messages)
    assert not any("synthetic command: finished" in value for value in messages)


def test_unread_stdout_pipe_does_not_block_process_shutdown() -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from agent_insights_quality.progress import ProgressReporter;"
                "r=ProgressReporter('test');"
                "[r.emit('x'*1000) for _ in range(5000)];"
                "print('result')"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    try:
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
