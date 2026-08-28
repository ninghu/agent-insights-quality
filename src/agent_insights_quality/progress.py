from __future__ import annotations

import atexit
import os
import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator


@dataclass
class ProgressOutcome:
    succeeded: bool = True

    def fail(self) -> None:
        self.succeeded = False


class _ProgressWriter:
    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[ProgressReporter, str]] = queue.Queue(
            maxsize=1024
        )
        self._enabled = True
        self._thread = threading.Thread(target=self._write_messages, daemon=True)
        try:
            self._thread.start()
        except RuntimeError:
            self._enabled = False

    def submit(self, reporter: ProgressReporter, value: str) -> None:
        if not self._enabled or not reporter._enabled:
            return
        try:
            self._queue.put_nowait((reporter, value))
        except queue.Full:
            pass

    def _write_messages(self) -> None:
        while self._enabled:
            reporter, value = self._queue.get()
            try:
                if reporter._enabled:
                    os.write(2, (value + "\n").encode("utf-8", errors="replace"))
            except Exception:
                reporter._enabled = False
            finally:
                self._queue.task_done()

    def flush(self, timeout_seconds: float = 0.2) -> None:
        deadline = time.monotonic() + timeout_seconds
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)


class ProgressReporter:
    def __init__(
        self,
        prefix: str,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._prefix = prefix
        self._monotonic = monotonic
        self._started = monotonic()
        self._enabled = True

    def emit(self, message: str) -> None:
        if not self._enabled:
            return
        elapsed = self._monotonic() - self._started
        _WRITER.submit(
            self,
            f"[{self._prefix} +{elapsed:07.1f}s] {message}",
        )

    @contextmanager
    def heartbeat(
        self,
        message: str,
        *,
        interval_seconds: float = 60,
    ) -> Iterator[ProgressOutcome]:
        self.emit(f"{message}: started")
        stopped = threading.Event()
        outcome = ProgressOutcome()

        def pulse() -> None:
            while not stopped.wait(interval_seconds):
                self.emit(f"{message}: still running")

        thread = threading.Thread(target=pulse, daemon=True)
        started = False
        try:
            thread.start()
            started = True
        except RuntimeError:
            pass
        try:
            yield outcome
        except BaseException:
            outcome.fail()
            raise
        finally:
            if started:
                stopped.set()
                try:
                    thread.join(timeout=max(1.0, interval_seconds))
                except RuntimeError:
                    pass
            self.emit(
                f"{message}: {'finished' if outcome.succeeded else 'failed'}"
            )


_WRITER = _ProgressWriter()
atexit.register(_WRITER.flush)
