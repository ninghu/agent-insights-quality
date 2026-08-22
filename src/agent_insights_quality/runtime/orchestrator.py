from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .errors import RuntimeFailure
from .receipts import read_receipt, write_receipt


@dataclass(frozen=True, slots=True)
class AnalysisWindow:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None or self.start >= self.end:
            raise RuntimeFailure("invalid_plan_window", "Plan window must be a half-open timezone-aware interval.")


@dataclass(frozen=True, slots=True)
class PlannedWindow:
    start_identity: str
    end_identity: str

    def __post_init__(self) -> None:
        start_match = re.fullmatch(
            r"window://(?P<run>run-[0-9]{2}-aiq-[0-9]{3}-[a-z][a-z0-9-]*)/"
            r"(?P<phase>[a-z-]+)/start-inclusive",
            self.start_identity,
        )
        end_match = re.fullmatch(
            r"window://(?P<run>run-[0-9]{2}-aiq-[0-9]{3}-[a-z][a-z0-9-]*)/"
            r"(?P<phase>[a-z-]+)/end-exclusive",
            self.end_identity,
        )
        if (
            start_match is None
            or end_match is None
            or start_match.group("run") != end_match.group("run")
            or start_match.group("phase") != end_match.group("phase")
        ):
            raise RuntimeFailure(
                "invalid_plan_window",
                "Plan window must preserve one exact symbolic half-open identity.",
            )

    @property
    def run_id(self) -> str:
        return self.start_identity.removeprefix("window://").split("/", 1)[0]

    @property
    def phase(self) -> str:
        return self.start_identity.rsplit("/", 2)[1]

    def public_dict(self) -> dict[str, str]:
        return {"start": self.start_identity, "end": self.end_identity}


@dataclass(frozen=True, slots=True)
class VersionWork:
    agent_id: str
    agent_name: str
    version_reference: str
    window: PlannedWindow | AnalysisWindow
    assignments: tuple[Mapping[str, Any], ...]
    agent_type: str = ""
    wave: int = 0
    phase: str = "healthy"
    version_key: str = "current"
    sequence_index: int = 0

    @property
    def key(self) -> str:
        return f"{self.agent_id}:{self.version_reference}:{self.run_id}:{self.phase}"

    @property
    def run_id(self) -> str:
        if isinstance(self.window, PlannedWindow):
            return self.window.run_id
        assignment = self.assignments[0] if self.assignments else {}
        return str(assignment.get("run_id") or f"{self.agent_id}-{self.version_reference}")


@dataclass(frozen=True, slots=True)
class PlanInput:
    plan_id: str
    project_name: str
    agents: Mapping[str, tuple[VersionWork, ...]]
    plan_digest: str = ""
    report_date: str = ""
    catalog_hash: str = ""
    project_expires_on: str = ""
    engine_build: str = ""
    generator_model: str = ""

    @property
    def reference(self) -> str:
        if self.plan_digest:
            return self.plan_digest
        return _opaque(
            {
                "plan_id": self.plan_id,
                "project_name": self.project_name,
                "agents": {
                    agent_id: [
                        {
                            "key": work.key,
                            "window": (
                                work.window.public_dict()
                                if isinstance(work.window, PlannedWindow)
                                else {
                                    "start": work.window.start.isoformat(),
                                    "end": work.window.end.isoformat(),
                                }
                            ),
                            "wave": work.wave,
                            "phase": work.phase,
                            "sequence_index": work.sequence_index,
                            "assignments": list(work.assignments),
                        }
                        for work in versions
                    ]
                    for agent_id, versions in sorted(self.agents.items())
                },
            }
        )

    @classmethod
    def from_daily_plan(cls, payload: Mapping[str, Any]) -> PlanInput:
        plan_id = str(payload.get("plan_id") or "")
        project = payload.get("project")
        assignments = payload.get("assignments")
        if not plan_id or not isinstance(project, Mapping) or not isinstance(assignments, list):
            raise RuntimeFailure("invalid_plan", "Daily plan is missing required orchestration fields.")
        grouped: dict[
            tuple[str, str, str],
            list[tuple[Mapping[str, Any], Mapping[str, Any], int]],
        ] = {}
        for raw in assignments:
            if not isinstance(raw, Mapping):
                raise RuntimeFailure("invalid_plan", "Plan assignment must be an object.")
            agent_id = str(raw.get("agent_id") or "")
            sequence = raw.get("version_sequence")
            if not agent_id or not isinstance(sequence, list) or not sequence:
                raise RuntimeFailure("invalid_plan", "Plan assignment is missing agent/version identity.")
            for sequence_index, version in enumerate(sequence):
                if not isinstance(version, Mapping):
                    raise RuntimeFailure("invalid_plan", "Plan version sequence entry must be an object.")
                version_reference = str(version.get("digest") or "")
                if not version_reference:
                    raise RuntimeFailure("invalid_plan", "Plan version sequence entry has no digest.")
                window = version.get("window")
                window_start = str(window.get("start") or "") if isinstance(window, Mapping) else ""
                grouped.setdefault((agent_id, version_reference, window_start), []).append(
                    (raw, version, sequence_index)
                )
        agents: dict[str, list[VersionWork]] = {}
        for (agent_id, version_reference, _window_start), entries in grouped.items():
            group = [entry[0] for entry in entries]
            versions = [entry[1] for entry in entries]
            sequence_indexes = {entry[2] for entry in entries}
            windows = [version.get("window") for version in versions]
            if not all(isinstance(window, Mapping) for window in windows):
                raise RuntimeFailure("invalid_plan", "Plan assignment window was invalid.")
            parsed = [
                PlannedWindow(
                    str(window.get("start") or ""),
                    str(window.get("end") or ""),
                )
                for window in windows
            ]
            if any(window != parsed[0] for window in parsed[1:]):
                raise RuntimeFailure(
                    "invalid_plan",
                    "Assignments sharing one agent version must use the same analysis window.",
                )
            names = {str(item.get("agent_name") or "") for item in group}
            if len(names) != 1 or "" in names:
                raise RuntimeFailure("invalid_plan", "Agent version assignments disagree on agent name.")
            agent_types = {str(item.get("agent_type") or "") for item in group}
            waves = {item.get("wave") for item in group}
            phases = {str(version.get("phase") or "") for version in versions}
            version_keys = {str(version.get("version_key") or "") for version in versions}
            if (
                len(agent_types) != 1
                or "" in agent_types
                or len(waves) != 1
                or not all(isinstance(wave, int) and wave >= 0 for wave in waves)
                or len(phases) != 1
                or "" in phases
                or len(version_keys) != 1
                or "" in version_keys
                or len(sequence_indexes) != 1
            ):
                raise RuntimeFailure(
                    "invalid_plan",
                    "Assignments sharing one agent version disagree on execution identity.",
                )
            agents.setdefault(agent_id, []).append(
                VersionWork(
                    agent_id=agent_id,
                    agent_name=next(iter(names)),
                    version_reference=version_reference,
                    window=parsed[0],
                    assignments=tuple(group),
                    agent_type=next(iter(agent_types)),
                    wave=next(iter(waves)),
                    phase=next(iter(phases)),
                    version_key=next(iter(version_keys)),
                    sequence_index=next(iter(sequence_indexes)),
                )
            )
        ordered: dict[str, tuple[VersionWork, ...]] = {}
        for agent_id, versions in agents.items():
            versions.sort(key=lambda item: (item.wave, item.sequence_index, item.version_reference))
            for previous, current in zip(versions, versions[1:], strict=False):
                if current.wave < previous.wave or (
                    current.wave == previous.wave
                    and (
                        current.run_id != previous.run_id
                        or current.sequence_index != previous.sequence_index + 1
                    )
                ):
                    raise RuntimeFailure(
                        "invalid_plan_version_order",
                        "Sequential versions for one agent are not ordered by wave and version sequence.",
                    )
            ordered[agent_id] = tuple(versions)
        engine = payload.get("engine")
        return cls(
            plan_id,
            str(project.get("name") or ""),
            ordered,
            plan_digest=str(payload.get("plan_digest") or ""),
            report_date=str(payload.get("report_date") or ""),
            catalog_hash=str(payload.get("catalog_hash") or ""),
            project_expires_on=str(project.get("expires_on") or ""),
            engine_build=str(engine.get("build") or "") if isinstance(engine, Mapping) else "",
            generator_model=(
                str(engine.get("generator_model") or "") if isinstance(engine, Mapping) else ""
            ),
        )


class RuntimeHooks(Protocol):
    def preflight(self, plan: PlanInput, *, dry_run: bool) -> Mapping[str, Any]: ...

    def ensure_project(self, plan: PlanInput, *, idempotency_key: str) -> Mapping[str, Any]: ...

    def deploy(self, work: VersionWork, *, idempotency_key: str) -> Mapping[str, Any]: ...

    def invoke(
        self,
        work: VersionWork,
        deployment: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def wait_ingestion(
        self,
        work: VersionWork,
        invocation: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def run_insights(
        self,
        work: VersionWork,
        telemetry: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def assemble_evidence(
        self,
        work: VersionWork,
        insight_run: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def recover(self, key: str, checkpoint: str) -> Mapping[str, Any]: ...

    def cancel(self, work: VersionWork) -> None: ...

    def finalize_failure(self, failure: RuntimeFailure, state: Mapping[str, Any]) -> None: ...


def _opaque(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(slots=True)
class RunState:
    plan_id: str
    plan_reference: str
    status: str = "pending"
    phase: str = "created"
    checkpoints: dict[str, str] = field(default_factory=dict)
    execution_windows: dict[str, dict[str, str]] = field(default_factory=dict)
    failed_phase: str | None = None
    failure: dict[str, Any] | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "plan_id": self.plan_id,
            "plan_reference": self.plan_reference,
            "status": self.status,
            "phase": self.phase,
            "checkpoints": self.checkpoints,
            "execution_windows": self.execution_windows,
            "failed_phase": self.failed_phase,
            "failure": self.failure,
        }

    @classmethod
    def from_receipt(
        cls,
        payload: Mapping[str, Any],
        plan_id: str,
        plan_reference: str,
    ) -> RunState:
        if payload.get("plan_id") != plan_id or payload.get("plan_reference") != plan_reference:
            raise RuntimeFailure("resume_plan_mismatch", "Receipt belongs to a different plan.")
        checkpoints = payload.get("checkpoints")
        execution_windows = payload.get("execution_windows", {})
        if not isinstance(checkpoints, Mapping) or not isinstance(execution_windows, Mapping):
            raise RuntimeFailure("invalid_receipt", "Receipt checkpoints were invalid.")
        return cls(
            plan_id=plan_id,
            plan_reference=plan_reference,
            status=str(payload.get("status") or "pending"),
            phase=str(payload.get("phase") or "created"),
            checkpoints={str(key): str(value) for key, value in checkpoints.items()},
            execution_windows={
                str(key): {str(boundary): str(timestamp) for boundary, timestamp in value.items()}
                for key, value in execution_windows.items()
                if isinstance(value, Mapping)
            },
            failed_phase=str(payload["failed_phase"]) if payload.get("failed_phase") else None,
            failure=dict(payload["failure"]) if isinstance(payload.get("failure"), Mapping) else None,
        )


class ProductionOrchestrator:
    def __init__(
        self,
        hooks: RuntimeHooks,
        receipt_path: Path,
        *,
        max_parallel_agents: int = 5,
        retry_attempts: int = 3,
        cancellation_wait_seconds: float = 30,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_parallel_agents <= 0 or retry_attempts <= 0 or cancellation_wait_seconds < 0:
            raise RuntimeFailure(
                "invalid_orchestrator_settings",
                "Concurrency and retries must be positive and cancellation wait must be non-negative.",
            )
        self._hooks = hooks
        self._receipt_path = receipt_path
        self._parallel = max_parallel_agents
        self._attempts = retry_attempts
        self._cancellation_wait = cancellation_wait_seconds
        self._sleep = sleep
        self._write_lock = threading.RLock()
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def _save(self, state: RunState) -> None:
        with self._write_lock:
            write_receipt(self._receipt_path, state.public_dict())

    def _retry(self, operation: Callable[[], Mapping[str, Any]]) -> Mapping[str, Any]:
        for attempt in range(1, self._attempts + 1):
            if self._cancelled.is_set():
                raise RuntimeFailure("run_cancelled", "Runtime cancellation was requested.")
            try:
                return operation()
            except RuntimeFailure as error:
                if not error.transient or attempt == self._attempts:
                    raise
                if self._cancelled.is_set():
                    raise RuntimeFailure("run_cancelled", "Runtime cancellation was requested.")
                self._sleep(min(2 ** (attempt - 1), 8))
        raise AssertionError("unreachable")

    def _step(
        self,
        state: RunState,
        key: str,
        phase: str,
        operation: Callable[[], Mapping[str, Any]],
        *,
        replay_existing: bool = False,
    ) -> Mapping[str, Any]:
        with self._write_lock:
            if self._cancelled.is_set():
                raise RuntimeFailure("run_cancelled", "Runtime cancellation was requested.")
            checkpoint = state.checkpoints.get(key)
            state.phase = phase
        if checkpoint is not None:
            try:
                value = (
                    self._retry(operation)
                    if replay_existing
                    else self._hooks.recover(key, checkpoint)
                )
            except RuntimeFailure as error:
                error.details.setdefault("phase", phase)
                raise
            if self._cancelled.is_set():
                raise RuntimeFailure("run_cancelled", "Runtime cancellation was requested.")
            if _opaque(value) != checkpoint:
                raise RuntimeFailure(
                    "checkpoint_drift",
                    "An idempotent resume step no longer matches its public checkpoint.",
                )
            return value
        try:
            value = self._retry(operation)
        except RuntimeFailure as error:
            error.details.setdefault("phase", phase)
            raise
        with self._write_lock:
            if self._cancelled.is_set():
                raise RuntimeFailure("run_cancelled", "Runtime cancellation was requested.")
            existing = state.checkpoints.get(key)
            reference = _opaque(value)
            if existing is not None and existing != reference:
                raise RuntimeFailure(
                    "checkpoint_drift",
                    "Concurrent idempotent step produced a different checkpoint.",
                )
            state.checkpoints[key] = reference
            self._save(state)
        return value

    def _run_agent(self, state: RunState, versions: Sequence[VersionWork]) -> None:
        for work in versions:
            prefix = work.key
            deployment = self._step(
                state,
                f"{prefix}:deploy",
                "deploy",
                lambda work=work: self._hooks.deploy(work, idempotency_key=f"{prefix}:deploy"),
            )
            invocation = self._step(
                state,
                f"{prefix}:invoke",
                "invoke",
                lambda work=work, deployment=deployment: self._hooks.invoke(
                    work, deployment, idempotency_key=f"{prefix}:invoke"
                ),
            )
            self._bind_execution_window(state, versions, work, invocation)
            telemetry = self._step(
                state,
                f"{prefix}:ingestion",
                "ingestion",
                lambda work=work, invocation=invocation: self._hooks.wait_ingestion(
                    work, invocation, idempotency_key=f"{prefix}:ingestion"
                ),
            )
            insight_run = self._step(
                state,
                f"{prefix}:insights",
                "insights",
                lambda work=work, telemetry=telemetry: self._hooks.run_insights(
                    work, telemetry, idempotency_key=f"{prefix}:insights"
                ),
            )
            self._step(
                state,
                f"{prefix}:evidence",
                "evidence",
                lambda work=work, insight_run=insight_run: self._hooks.assemble_evidence(
                    work, insight_run, idempotency_key=f"{prefix}:evidence"
                ),
            )

    def _bind_execution_window(
        self,
        state: RunState,
        versions: Sequence[VersionWork],
        work: VersionWork,
        invocation: Mapping[str, Any],
    ) -> None:
        value = invocation.get("window_binding")
        if not isinstance(value, Mapping) or set(value) != {
            "planned_start",
            "planned_end",
            "realized_start",
            "realized_end",
        }:
            raise RuntimeFailure(
                "invalid_realized_window",
                "Invocation receipt did not bind the symbolic and realized windows.",
            )
        if isinstance(work.window, PlannedWindow) and (
            value["planned_start"] != work.window.start_identity
            or value["planned_end"] != work.window.end_identity
        ):
            raise RuntimeFailure(
                "invalid_realized_window",
                "Invocation receipt changed the immutable symbolic window identity.",
            )
        try:
            start = datetime.fromisoformat(str(value["realized_start"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(value["realized_end"]).replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeFailure(
                "invalid_realized_window",
                "Invocation receipt contains invalid realized timestamps.",
            ) from error
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise RuntimeFailure(
                "invalid_realized_window",
                "Invocation receipt realized window is not a timezone-aware half-open interval.",
            )
        position = versions.index(work)
        with self._write_lock:
            if position:
                prior = state.execution_windows.get(versions[position - 1].key)
                if prior is None:
                    raise RuntimeFailure(
                        "missing_prior_window",
                        "Sequential execution is missing its prior realized window.",
                    )
                prior_end = datetime.fromisoformat(
                    prior["realized_end"].replace("Z", "+00:00")
                )
                if start < prior_end:
                    raise RuntimeFailure(
                        "overlapping_realized_windows",
                        "Sequential versions produced overlapping realized windows.",
                    )
            state.execution_windows[work.key] = {
                str(key): str(timestamp) for key, timestamp in value.items()
            }
            self._save(state)

    def _cancel_plan(self, plan: PlanInput) -> list[str]:
        failures: list[str] = []
        for versions in plan.agents.values():
            for work in versions:
                try:
                    self._hooks.cancel(work)
                except RuntimeFailure as error:
                    failures.append(error.code)
        return sorted(failures)

    def run(self, plan: PlanInput, *, resume: bool = False, dry_run: bool = False) -> RunState:
        cancellation_sent = False
        if resume:
            state = RunState.from_receipt(
                read_receipt(self._receipt_path),
                plan.plan_id,
                plan.reference,
            )
            if state.status == "succeeded":
                return state
        else:
            state = RunState(plan.plan_id, plan.reference)
            self._save(state)
        try:
            self._step(
                state,
                "preflight",
                "preflight",
                lambda: self._hooks.preflight(plan, dry_run=dry_run),
                replay_existing=True,
            )
            if dry_run:
                state.status = "dry_run"
                state.phase = "complete"
                self._save(state)
                return state
            self._step(
                state,
                f"{plan.plan_id}:project",
                "project",
                lambda: self._hooks.ensure_project(plan, idempotency_key=f"{plan.plan_id}:project"),
            )
            state.status = "running"
            self._save(state)
            pool = ThreadPoolExecutor(max_workers=min(self._parallel, max(1, len(plan.agents))))
            futures: list[Future[None]] = [
                pool.submit(self._run_agent, state, versions)
                for versions in plan.agents.values()
            ]
            done, pending = wait(futures, return_when=FIRST_EXCEPTION)
            failure: RuntimeFailure | None = None
            for future in done:
                try:
                    future.result()
                except RuntimeFailure as error:
                    failure = error
                    break
                except Exception as error:
                    failure = RuntimeFailure(
                        "unexpected_runtime_failure",
                        "A runtime hook raised an unexpected exception.",
                    )
                    failure.__cause__ = error
                    break
            if failure is not None:
                self._cancelled.set()
                cancellation_failures = self._cancel_plan(plan)
                cancellation_sent = True
                if cancellation_failures:
                    failure.details["cancellation_failures"] = cancellation_failures
                running: list[Future[None]] = []
                for future in pending:
                    if not future.cancel():
                        running.append(future)
                pool.shutdown(wait=False, cancel_futures=True)
                if running:
                    wait(running, timeout=self._cancellation_wait)
                raise failure
            pool.shutdown(wait=True)
            state.status = "succeeded"
            state.phase = "complete"
            state.failed_phase = None
            state.failure = None
            self._save(state)
            return state
        except RuntimeFailure as failure:
            self._cancelled.set()
            if not cancellation_sent:
                cancellation_failures = self._cancel_plan(plan)
                if cancellation_failures:
                    failure.details["cancellation_failures"] = cancellation_failures
            state.status = "inconclusive"
            state.failed_phase = str(failure.details.get("phase") or state.phase)
            state.failure = {
                "code": failure.code,
                "message": failure.message,
                "transient": failure.transient,
            }
            self._save(state)
            self._hooks.finalize_failure(failure, state.public_dict())
            raise
