from __future__ import annotations

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
        self._lock = threading.Lock()
        self._enabled = True

    def emit(self, message: str) -> None:
        if not self._enabled:
            return
        elapsed = self._monotonic() - self._started
        with self._lock:
            if not self._enabled:
                return
            try:
                print(
                    f"[{self._prefix} +{elapsed:07.1f}s] {message}",
                    flush=True,
                )
            except Exception:
                self._enabled = False

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
