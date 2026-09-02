from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_lifecycle import (
    LocalValidationLock,
    validation_runtime_root,
)
from agent_insights_quality.validation_quota import CapacityPlan


class CrossProcessTelemetryLease:
    def __init__(
        self,
        *,
        run_id: str,
        capacity: CapacityPlan,
        fence: Callable[[], None],
        root: Path | None = None,
        poll_seconds: float = 0.25,
    ) -> None:
        if capacity.telemetry_query_concurrency != 8:
            raise ContractError(
                "Telemetry lease requires the reviewed eight-slot capacity"
            )
        if not run_id.startswith("validation-") or poll_seconds <= 0:
            raise ContractError("Telemetry lease binding is invalid")
        self._fence = fence
        self._poll_seconds = poll_seconds
        self._root = (
            root or validation_runtime_root() / "telemetry-leases"
        ) / run_id / capacity.plan_digest.removeprefix("sha256:")
        self._lock: LocalValidationLock | None = None

    @property
    def slot(self) -> int | None:
        if self._lock is None:
            return None
        return int(self._lock.path.stem.removeprefix("slot-"))

    def try_acquire(self) -> bool:
        if self._lock is not None:
            raise ContractError("Telemetry lease is already held")
        self._fence()
        for slot in range(1, 9):
            lock = LocalValidationLock(self._root / f"slot-{slot:02d}.lock")
            try:
                lock.acquire()
            except ContractError:
                continue
            try:
                self._fence()
            except BaseException:
                lock.release()
                raise
            self._lock = lock
            return True
        return False

    def acquire(self) -> None:
        while not self.try_acquire():
            time.sleep(self._poll_seconds)

    def release(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def __enter__(self) -> CrossProcessTelemetryLease:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
