"""Public-safe progress and private, append-only local operational logs.

No raw exception strings, free text, provider identifiers, or payloads are event
fields. The caller supplies reviewed catalog UnitIds, not runtime provider names.
The optional outbox callback persists only the validated projection locally.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, TextIO
import weakref

from .errors import QualityError
from .results import UnitId
from .state import _inside, _mkdir, _sync_directory


EVENT_KINDS = frozenset(
    {"started", "heartbeat", "retry", "checkpoint", "resume", "completed", "failure", "warning"}
)
STAGES = frozenset(
    {"run", "deployment", "traffic", "evidence", "insights", "assessment", "report", "delivery", "outbox"}
)
SAFE_CODES = frozenset(
    {
        "ok", "operation_failed", "checkpoint_failed", "logging_failed", "remote_timeout",
        "rate_limited", "retry_exhausted", "deadline_expired", "incomplete_evidence",
        "ambiguous_outcome", "settings_invalid", "record_conflict", "ownership_busy",
        "cancelled",
    }
)
COUNTERS = frozenset(
    {"attempt_count", "completed_count", "pending_count", "retry_count", "failed_count", "unit_count"}
)
_LOCKS: weakref.WeakValueDictionary[Path, threading.RLock] = weakref.WeakValueDictionary()
_LOCKS_GUARD = threading.Lock()


def _directory_lock(directory: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(directory)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[directory] = lock
        return lock


def _bounded_count(value: object, maximum: int = 1_000_000_000) -> bool:
    return type(value) is int and 0 <= value <= maximum


def project_event(
    event: Mapping[str, Any], *, allowed_units: Iterable[UnitId] = ()
) -> dict[str, Any]:
    """Validate both field names and closed-vocabulary values for an ADX outbox."""
    required = {"utc", "elapsed_ms", "kind", "stage", "code", "counters"}
    optional = {"agent", "logical_version", "attempt", "turn"}
    if not isinstance(event, Mapping) or not required <= event.keys():
        raise ValueError("Invalid event")
    if set(event) - required - optional:
        raise ValueError("Invalid event fields")
    if (
        not isinstance(event["kind"], str) or event["kind"] not in EVENT_KINDS
        or not isinstance(event["stage"], str) or event["stage"] not in STAGES
        or not isinstance(event["code"], str) or event["code"] not in SAFE_CODES
        or not _bounded_count(event["elapsed_ms"], 1_000_000_000_000)
    ):
        raise ValueError("Invalid event values")
    timestamp = event["utc"]
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise ValueError("Invalid event time")
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z") != timestamp:
        raise ValueError("Invalid event time")
    counters = event["counters"]
    if (
        not isinstance(counters, Mapping)
        or set(counters) - COUNTERS
        or any(not _bounded_count(value) for value in counters.values())
    ):
        raise ValueError("Invalid event counters")
    has_unit = "agent" in event or "logical_version" in event
    if has_unit:
        unit = UnitId(event.get("agent"), event.get("logical_version"))
        if unit not in frozenset(allowed_units):
            raise ValueError("Unknown event unit")
    for field, maximum in (("attempt", 10), ("turn", 10_000)):
        if field in event and (
            not has_unit or not _bounded_count(event[field], maximum) or event[field] < 1
        ):
            raise ValueError("Invalid event position")
    return {**event, "counters": dict(counters)}


class _EventFormatter(logging.Formatter):
    def __init__(self, *, structured: bool) -> None:
        super().__init__()
        self.structured = structured

    def format(self, record: logging.LogRecord) -> str:
        event = record.msg
        if not isinstance(event, dict):
            raise ValueError("Invalid log record")
        if self.structured:
            return json.dumps(event, sort_keys=True, allow_nan=False)
        context = {
            key: value for key, value in event.items()
            if key not in ("utc", "elapsed_ms", "kind", "stage", "code")
        }
        return (
            f"{event['utc']} +{event['elapsed_ms']}ms "
            f"{event['kind']} {event['stage']} {event['code']} "
            + json.dumps(context, sort_keys=True)
        )


class _DurableHandler(RotatingFileHandler):
    recovered_tail = False

    def _open(self) -> TextIO:
        descriptor = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        return os.fdopen(descriptor, "a", encoding="utf-8", newline="\n")

    def emit(self, record: logging.LogRecord) -> None:
        path = Path(self.baseFilename)
        for name in [path, *(Path(f"{path}.{index}") for index in range(1, self.backupCount + 1))]:
            _inside(path.parent, name)
        _mkdir(path.parent)
        message = self.format(record) + "\n"
        size = path.stat().st_size if path.exists() else 0
        incomplete = False
        if size:
            with path.open("rb") as stream:
                stream.seek(-1, os.SEEK_END)
                incomplete = stream.read(1) != b"\n"
        if incomplete or (size and size + len(message.encode("utf-8")) > self.maxBytes):
            self.doRollover()
            self.recovered_tail = self.recovered_tail or incomplete
        # Closing after every append also makes restart/multiple logger instances
        # safe across rotation on Windows; environment ownership excludes processes.
        with self._open() as stream:
            stream.write(message)
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory(path.parent)


class _ConsoleHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        self.stream.write(self.format(record) + "\n")
        self.flush()


class RunLogger:
    """Synchronous small appends serialize threads and async callers without awaits.

    Hold RuntimeStore.ownership() for the process lifetime. A health warning is
    not a failed Agent attempt; a separate CheckpointError is fatal to unsafe work.
    No callback is invoked in explicit test mode, even if one was supplied.
    """

    def __init__(
        self,
        directory: Path,
        *,
        allowed_units: Iterable[UnitId] = (),
        max_bytes: int = 2 * 1024 * 1024,
        backup_count: int = 3,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        console: TextIO | None = None,
        stderr: TextIO | None = None,
        outbox: Callable[[dict[str, Any]], None] | None = None,
        test_run: bool = False,
    ) -> None:
        if (
            type(max_bytes) is not int or not 1024 <= max_bytes <= 64 * 1024 * 1024
            or type(backup_count) is not int or not 1 <= backup_count <= 20
            or type(test_run) is not bool
        ):
            raise ValueError("Invalid logging settings")
        self.directory = directory.absolute()
        _inside(self.directory, self.directory)
        self.allowed_units = frozenset(allowed_units)
        if any(not isinstance(unit, UnitId) for unit in self.allowed_units):
            raise ValueError("Invalid event units")
        self._lock = _directory_lock(self.directory)
        self._clock = clock
        self._monotonic = monotonic
        self._started = monotonic()
        self._warnings: set[str] = set()
        self._stderr = sys.stderr if stderr is None else stderr
        self._outbox = None if test_run else outbox
        self._logger = logging.Logger("agent_insights_quality.run", logging.INFO)
        self._logger.propagate = False
        self._handlers = []
        for filename, structured in (("runner.log", False), ("events.jsonl", True)):
            handler = _DurableHandler(
                self.directory / filename, maxBytes=max_bytes, backupCount=backup_count,
                encoding="utf-8", delay=True,
            )
            handler.setFormatter(_EventFormatter(structured=structured))
            self._handlers.append(handler)
        self._console = _ConsoleHandler(sys.stdout if console is None else console)
        self._console.setFormatter(_EventFormatter(structured=False))

    @property
    def health_warnings(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._warnings))

    def _warn(self, code: str) -> None:
        if code not in self._warnings:
            self._warnings.add(code)
            try:
                self._stderr.write(f"WARNING {code}\n")
                self._stderr.flush()
            except (OSError, ValueError):
                self._warnings.add("logging_stderr_failed")

    def emit(self, kind: str, **fields: Any) -> bool:
        """Return whether both local logs were saved; reject unsafe fields intact."""
        with self._lock:
            try:
                if set(fields) - {"stage", "code", "unit", "attempt", "turn", "counters"}:
                    raise ValueError("Unknown event field")
                now = self._clock()
                elapsed = self._monotonic() - self._started
                if now.tzinfo is None or not math.isfinite(elapsed) or elapsed < 0:
                    raise ValueError("Invalid event clock")
                event = {
                    "utc": now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "elapsed_ms": int(elapsed * 1000),
                    "kind": kind,
                    "stage": fields.get("stage", "run"),
                    "code": fields.get("code", "ok"),
                    "counters": fields.get("counters", {}),
                }
                unit = fields.get("unit")
                if unit is not None:
                    if not isinstance(unit, UnitId):
                        raise ValueError("Invalid event unit")
                    event.update(unit.to_dict())
                event.update({key: fields[key] for key in ("attempt", "turn") if key in fields})
                event = project_event(event, allowed_units=self.allowed_units)
            except (TypeError, ValueError, OverflowError):
                self._warn("logging_event_rejected")
                return False
            record = self._logger.makeRecord(
                self._logger.name, logging.INFO, "", 0, event, (), None,
            )
            saved = True
            for handler in self._handlers:
                try:
                    handler.handle(record)
                    if handler.recovered_tail:
                        self._warn("logging_tail_incomplete")
                except (OSError, QualityError, ValueError):
                    saved = False
                    self._warn("logging_write_failed")
            try:
                self._console.handle(record)
            except (OSError, ValueError):
                self._warn("logging_console_failed")
            if self._outbox is not None:
                try:
                    self._outbox(project_event(event, allowed_units=self.allowed_units))
                except (OSError, QualityError, ValueError):
                    self._warn("logging_outbox_failed")
            return saved

    def close(self) -> None:
        with self._lock:
            for handler in (*self._handlers, self._console):
                handler.close()

    def __enter__(self) -> RunLogger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
