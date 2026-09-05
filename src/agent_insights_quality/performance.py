"""Private process-segment observations; never a score, scheduler or retry policy."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager, contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
import json
import math
import re
import secrets
import sys
import time

from .contracts import Invocation
from .errors import QualityError
from .state import RecordStore, StateError

_BINDINGS: ContextVar[dict] = ContextVar("performance_bindings", default={})
_SCOPES: ContextVar[tuple] = ContextVar("performance_scopes", default=())
_SESSION: ContextVar[list | None] = ContextVar("performance_session", default=None)
_PORT_METHODS = frozenset({
    "ensure_deployment", "activate", "create_session", "invoke", "query",
    "ensure_monitor", "reset_monitor", "start_insights", "get_insights_run", "list_insights",
})
_SOL_KINDS = frozenset({"sol_http", "sol_recovery_queue", "sol_cooldown", "sol_usage"})
_SOL_FIELDS = frozenset({
    "kind", "elapsed_seconds", "status", "http_status", "request_accepted",
    "input_tokens", "output_tokens", "usage_status", "requested_seconds",
})


class RunMetrics:
    """Bounded private measurements for one process invocation of a durable run.

    Stage/port/HTTP intervals nest and overlap. Their sums are NOT total run wall
    time or inferred service time. A resumed segment never rewrites old segments.
    Persistence failures disable the metrics sink only and emit a code-only warning.
    """

    def __init__(
        self, records: RecordStore, *, monotonic: Callable[[], float] = time.monotonic,
        segment_id: str | None = None, max_records: int = 20_000, checkpoint_every: int = 50,
    ) -> None:
        if type(max_records) is not int or max_records < 1 or type(checkpoint_every) is not int or checkpoint_every < 1:
            raise ValueError("Invalid performance limits")
        self.records, self.clock = records, monotonic
        self.segment_id = segment_id or "segment-" + secrets.token_hex(12)
        if not isinstance(self.segment_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", self.segment_id) is None:
            raise ValueError("Invalid performance segment identity")
        self.artifact_key = f"performance/{self.segment_id}/performance"
        self.max_records, self.checkpoint_every = max_records, checkpoint_every
        self.started = self.clock()
        self.observations: list[dict] = []
        self.active: Counter = Counter()
        self.peak: Counter = Counter()
        self.totals: dict[str, dict] = defaultdict(lambda: {
            "count": 0, "measured_seconds": 0.0, "reused": 0, "skipped": 0, "statuses": Counter(),
        })
        self.dropped = 0
        self._warnings: set[str] = set()
        self._disabled = False
        self._finalized = False
        self._count = 0
        self._persisted_observations = 0
        self._batch_number = 0
        self.configuration: dict[str, int] = {}
        self.tokens = {
            "response_count": 0, "known_input_count": 0, "known_output_count": 0,
            "input_tokens": None, "output_tokens": None,
        }
        self._persist("progress", f"performance/{self.segment_id}", self.summary("running"))

    @property
    def health_warnings(self) -> tuple[str, ...]:
        return tuple(sorted(self._warnings))

    @property
    def artifact_path(self) -> str | None:
        if not self._finalized:
            return None
        try:
            return str(self.records._path("artifacts", self.artifact_key))
        except (StateError, OSError):
            self._warn()
            return None

    def _warn(self) -> None:
        code = "performance_persistence_failed"
        if code not in self._warnings:
            self._warnings.add(code)
            try:
                sys.stderr.write("WARNING " + code + "\n")
            except (OSError, ValueError):
                pass
        self._disabled = True

    def _persist(self, collection: str, key: str, value: Mapping) -> None:
        if self._disabled:
            return
        try:
            getattr(self.records, "save_" + collection)(key, value)
        except (StateError, OSError):
            self._warn()

    @contextmanager
    def bind(self, **fields):
        if set(fields) - {"unit", "lane", "attempt", "turn"}:
            raise ValueError("Unsupported performance binding")
        token = _BINDINGS.set({**_BINDINGS.get(), **fields})
        try:
            yield
        finally:
            _BINDINGS.reset(token)

    @contextmanager
    def span(self, kind: str, name: str, **fields):
        started = self.clock()
        key = kind + ":" + name
        record = {
            "kind": kind, "name": name, **_BINDINGS.get(), **fields, "status": "completed",
            "start_offset_seconds": started - self.started,
        }
        self.active[key] += 1
        self.peak[key] = max(self.peak[key], self.active[key])
        token = _SCOPES.set((*_SCOPES.get(), (self, record)))
        try:
            yield record
        except BaseException as error:
            record["status"] = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
            if isinstance(error, QualityError):
                record["error_code"] = error.code
                record["request_accepted"] = error.request_accepted
                record["http_status"] = error.status
            raise
        finally:
            self.active[key] -= 1
            _SCOPES.reset(token)
            elapsed = self.clock() - started
            record["end_offset_seconds"] = started - self.started + elapsed
            record["wall_elapsed_seconds"] = elapsed
            record["elapsed_seconds"] = None if record["status"] in {"reused", "skipped"} else elapsed
            self.record(record)

    def reuse(self, kind: str, name: str, *, skipped: bool = False, **fields) -> None:
        status = "skipped" if skipped else "reused"
        for metrics, record in reversed(_SCOPES.get()):
            if metrics is self and record["kind"] == kind and record["name"] == name:
                record.update(status=status, **fields)
                return
        self.record({
            "kind": kind, "name": name, **_BINDINGS.get(), **fields,
            "status": status, "elapsed_seconds": None,
        })

    def increment_scope(self, kind: str, name: str, field: str) -> None:
        for metrics, record in reversed(_SCOPES.get()):
            if metrics is self and record["kind"] == kind and record["name"] == name:
                record[field] = record.get(field, 0) + 1
                return

    def record(self, value: Mapping) -> None:
        record = dict(value)
        duration = record.get("elapsed_seconds")
        if duration is not None and (
            isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or duration < 0
        ):
            raise ValueError("Invalid measured duration")
        key = record["kind"] + ":" + record["name"]
        total = self.totals[key]
        total["count"] += 1
        total["measured_seconds"] += duration or 0
        total["statuses"][record["status"]] += 1
        if record["status"] in {"reused", "skipped"}:
            total[record["status"]] += 1
        self._count += 1
        record["sequence"] = self._count
        if len(self.observations) < self.max_records:
            self.observations.append(record)
        else:
            self.dropped += 1
        if self._count % self.checkpoint_every == 0:
            batch = self.observations[self._persisted_observations:]
            if batch:
                self._batch_number += 1
                self._persist("artifact", f"performance/{self.segment_id}/batches/batch-{self._batch_number:06d}", {
                    "schema_version": "1.0", "segment_id": self.segment_id, "observations": batch,
                })
                if not self._disabled:
                    self._persisted_observations = len(self.observations)
            self._persist("progress", f"performance/{self.segment_id}", self.summary("running"))

    def observe_sol(self, event: Mapping) -> None:
        if event.get("kind") not in _SOL_KINDS or set(event) - _SOL_FIELDS:
            raise ValueError("Unsupported Sol performance event")
        key = event["kind"] + ":" + event["kind"]
        if event["status"] == "started":
            self.active[key] += 1
            self.peak[key] = max(self.peak[key], self.active[key])
            return
        if event["kind"] != "sol_usage":
            self.active[key] -= 1
        else:
            self.tokens["response_count"] += 1
            for name in ("input", "output"):
                value = event[name + "_tokens"]
                if value is not None:
                    self.tokens["known_" + name + "_count"] += 1
                    self.tokens[name + "_tokens"] = (self.tokens[name + "_tokens"] or 0) + value
        self.record({
            "name": event["kind"], **_BINDINGS.get(), **event,
            "elapsed_seconds": event.get("elapsed_seconds"),
        })

    async def await_call(self, kind: str, name: str, operation, **fields):
        with self.span(kind, name, **fields) as span:
            result = await operation
            if isinstance(result, Invocation):
                accepted = (
                    False if result.http_status is not None and 400 <= result.http_status < 500
                    and result.http_status != 408 else True if result.response_id else None
                )
                span.update(
                    status=result.status, http_status=result.http_status,
                    request_accepted=accepted,
                )
            elif name == "start_insights" and isinstance(result, Mapping):
                span["request_accepted"] = True if result.get("id") else None
            return result

    def summary(self, status: str) -> dict:
        return {
            "schema_version": "1.0", "segment_id": self.segment_id, "status": status,
            "run_id": self.records.directory.name, "profile": self.records._runtime.environment,
            "configuration": dict(self.configuration),
            "wall_elapsed_seconds": self.clock() - self.started,
            "semantics": {
                "wall": "Elapsed monotonic time in this process segment, not accumulated prior work.",
                "summed": "Inclusive overlapping intervals; never sum nested categories as run wall or service time.",
                "port_call": "Awaited adapter wall time; includes adapter work, not necessarily one HTTP request.",
                "queue": "Time awaiting the existing semaphore or recovery lock.",
                "reuse": "No fresh call measured; elapsed_seconds is null, never a zero-latency success.",
                "tokens": "Observed Sol response usage only; absent or invalid fields remain unknown.",
                "unit": "daily_lane covers that version's lane interval; final assessment is a separate unit stage after all lanes.",
                "start": "CLI segment begins after source/plan resolves the run ID; earlier startup is in command-status logs.",
            },
            "totals": {
                key: {**value, "statuses": dict(value["statuses"]),
                      "active": self.active[key], "peak_active": self.peak[key]}
                for key, value in sorted(self.totals.items())
            },
            "active": {key: value for key, value in self.active.items() if value},
            "peak_active": dict(self.peak), "dropped_records": self.dropped,
            "persisted_observation_count": self._persisted_observations,
            "warnings": list(self.health_warnings),
            "sol_usage": dict(self.tokens),
        }

    def finalize(self, status: str = "completed") -> str | None:
        if self._finalized:
            return self.artifact_path
        value = {**self.summary(status), "observations": self.observations}
        self._persist("artifact", self.artifact_key, value)
        if not self._disabled:
            self._finalized = True
            self._persist("progress", "performance/latest", {
                "segment_id": self.segment_id, "artifact": self.artifact_key,
            })
        return self.artifact_path


def measure(kind: str, name: str):
    """Observe an unchanged Runner coroutine body and its natural await boundaries."""
    def decorate(function):
        @wraps(function)
        async def wrapped(self, *args, **kwargs):
            metrics = self.metrics
            if metrics is None:
                return await function(self, *args, **kwargs)
            target = args[0] if args and hasattr(args[0], "unit_id") else None
            fields = {"unit": target.key, "lane": target.unit_id.agent} if target else {}
            if kind == "turn":
                fields.update(attempt=args[3].index, turn=args[4].step_id)
            with metrics.bind(**fields):
                with metrics.span(kind, name) as record:
                    result = await function(self, *args, **kwargs)
                    if isinstance(result, Invocation):
                        record["receipt_status"] = result.status
                        if record["status"] not in {"reused", "skipped"}:
                            record["status"] = result.status
                        metrics.increment_scope(
                            "attempt", "traffic", "reused_turns" if record["status"] == "reused" else "fresh_turns",
                        )
                    return result
        return wrapped
    return decorate


def scope(metrics: RunMetrics | None, kind: str, name: str, **fields):
    return metrics.span(kind, name, **fields) if metrics is not None else nullcontext({})


def binding(metrics: RunMetrics | None, **fields):
    return metrics.bind(**fields) if metrics is not None else nullcontext()


@asynccontextmanager
async def limited(metrics: RunMetrics | None, semaphore, name: str):
    with scope(metrics, "queue", name):
        await semaphore.acquire()
    try:
        with scope(metrics, "execution", name):
            yield
    finally:
        semaphore.release()


def observe(metrics: RunMetrics | None, kind: str, name: str, fields: Callable):
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            with binding(metrics, **fields(*args, **kwargs)), scope(metrics, kind, name):
                return await function(*args, **kwargs)
        return wrapped
    return decorate


class ObservedCloud:
    def __init__(self, cloud, metrics: RunMetrics) -> None:
        self.environment = cloud.environment
        self.cloud, self.metrics = cloud, metrics

    def __getattr__(self, name):
        method = getattr(self.cloud, name)
        if name not in _PORT_METHODS:
            return method
        async def observed(*args, **kwargs):
            return await self.metrics.await_call("port_call", name, method(*args, **kwargs))
        return observed


class ObservedSol:
    def __init__(self, sol, metrics: RunMetrics) -> None:
        self.sol, self.metrics = sol, metrics

    async def complete_json(self, *, instructions, payload, schema):
        size = len(json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8"))
        return await self.metrics.await_call(
            "model_call", "sol", self.sol.complete_json(instructions=instructions, payload=payload, schema=schema),
            input_payload_bytes=size, input_tokens=None, output_tokens=None,
            usage_status="see_observed_response_usage_or_unknown",
        )


@contextmanager
def metric_session(factory=RunMetrics):
    """CLI lifecycle holder: _run supplies its real run store once known."""
    session = [None, factory]
    token = _SESSION.set(session)
    status = "completed"
    try:
        yield session
    except BaseException as error:
        status = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
        raise
    finally:
        try:
            if session[0] is not None:
                session[0].finalize(status)
        finally:
            _SESSION.reset(token)


def begin_metrics(records: RecordStore) -> RunMetrics | None:
    session = _SESSION.get()
    if session is None:
        return None
    if session[0] is None and session[1] is not None:
        session[0] = session[1](records)
    return session[0]


def current_metrics() -> RunMetrics | None:
    session = _SESSION.get()
    return session[0] if session is not None else None
