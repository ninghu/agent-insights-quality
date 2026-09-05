"""Resumable qualification, with no live dependencies at import time."""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .catalogs import Catalog
from .contracts import Attempt, CloudPort, Deployment, Invocation, SolPort, Step, Target
from .errors import QualityError
from .events import RunLogger
from .registry import DeploymentRegistry
from .results import (
    CardVerdict, Contribution, CoreVerdict, DiagnosticVerdict, ExclusionReason,
    PlannedUnit, QualityResult, UnitId, UnitResult, aggregate_results,
)
from .selection import (
    LastTest, Selection, SourceChanges, deployment_inputs, git_source_changes, select_staging,
)
from .settings import RuntimeSettings
from .state import RecordStore, RuntimeStore, StateError
from .telemetry import Snapshot, _flat, collect_snapshot
from .traffic import load_attempts

_PENDING = {
    "deployment_pending", "deployment_propagation_pending",
    "deployment_create_identity_pending", "deployment_not_ready",
    "acr_build_pending", "acr_image_pending",
}
_INTEGRITY = {
    "deployment_identity_mismatch", "deployment_version_mismatch",
    "deployment_metadata_mismatch", "session_version_mismatch",
    "duplicate_endpoint_response", "assessment_response_reused",
}
_EVALUATION = (
    "src/agent_insights_quality/assessment.py", "src/agent_insights_quality/telemetry.py",
    "src/agent_insights_quality/prompts", "src/agent_insights_quality/runner.py",
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Timezone required")
        return parsed.astimezone(UTC)
    except (TypeError, AttributeError, ValueError) as error:
        raise StateError("checkpoint_timestamp_invalid") from error


def source_revision(catalog: Catalog) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=catalog.root,
        capture_output=True, text=True, check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise QualityError("source_revision_unavailable")
    return result.stdout.strip()


def deployment_revision(catalog: Catalog, target: Target) -> str:
    result = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--",
         *(str(path.relative_to(catalog.root)) for path in deployment_inputs(target))],
        cwd=catalog.root, capture_output=True, text=True, check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise QualityError("deployment_source_revision_unavailable")
    return result.stdout.strip()


def planned_units(targets: tuple[Target, ...]) -> tuple[PlannedUnit, ...]:
    return tuple(
        PlannedUnit(target.unit_id, None if target.is_baseline else target.unit_id.logical_version)
        for target in targets
    )


def restore_unit(value: Mapping[str, Any]) -> UnitResult:
    try:
        return UnitResult(
            UnitId(**value["unit_id"]),
            tuple(CardVerdict(
                item["card_alias"], CoreVerdict(item["core"]), item.get("root_cause_alias"),
                Contribution(item["contribution"]), DiagnosticVerdict(item["severity"]),
                DiagnosticVerdict(item["proposed_fix"]), item.get("summary"),
            ) for item in value["cards"]),
            tuple(ExclusionReason(item) for item in value["exclusion_reasons"]),
            value.get("summary"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise StateError("unit_checkpoint_invalid") from error


def choose_staging(
    catalog: Catalog, store: RuntimeStore, *, full: bool = False,
    changes_since: Callable[[str], SourceChanges] | None = None,
) -> tuple[Selection, ...]:
    previous = {}
    for target in catalog.targets:
        record = store.staging_index.read(target.key, missing_ok=True)
        if record is not None:
            try:
                previous[target.key] = LastTest(
                    record["source_revision"], record["status"], record["tested_at"],
                )
            except (KeyError, TypeError, ValueError) as error:
                raise StateError("staging_history_invalid") from error
    compare = changes_since or (lambda revision: git_source_changes(catalog.root, revision))
    changes = {} if full else {
        revision: compare(revision) for revision in {item.source_revision for item in previous.values()}
    }
    return select_staging(
        catalog, last_tests=previous, changes_by_revision=changes, full=full,
        evaluation_paths=_EVALUATION,
    )


@dataclass
class _Work:
    records: RecordStore
    binding: dict[str, Any]

    @property
    def key(self) -> str:
        return self.binding["work_key"]


class Runner:
    """The caller holds environment ownership, including while awaiting providers.

    A target binding points directly to retained traffic. Reassessment gets a new
    immutable artifact, not a new traffic identity. No approval or digest graph.
    """

    def __init__(
        self, catalog: Catalog, runtime: RuntimeStore, run_id: str, cloud: CloudPort,
        sol: SolPort, registry: DeploymentRegistry, *,
        settings: RuntimeSettings | None = None, test_run: bool = False, rerun: int = 0,
        revision: str | None = None, reuse_run_id: str | None = None,
        now: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        attempts: Callable[[Target], tuple[Attempt, ...]] = load_attempts,
        deployment_source: Callable[[Target], str] | None = None,
        changes_since: Callable[[str], SourceChanges] | None = None,
        event_outbox: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if runtime.environment != cloud.environment.profile:
            raise QualityError("runner_environment_mismatch")
        if type(test_run) is not bool or type(rerun) is not int or (
            rerun < 1 if test_run else rerun != 0
        ) or test_run and runtime.environment != "daily":
            raise QualityError("runner_test_identity_invalid")
        if reuse_run_id and (not test_run or reuse_run_id == run_id):
            raise QualityError("runner_reuse_invalid")
        self.catalog, self.runtime, self.run_id = catalog, runtime, run_id
        self.run = runtime.run(run_id)
        self.cloud, self.sol, self.registry = cloud, sol, registry
        self.settings = settings or RuntimeSettings()
        self.test_run, self.rerun, self.reuse_run_id = test_run, rerun, reuse_run_id
        self.revision = revision or source_revision(catalog)
        self.now, self.monotonic, self.sleep = now, monotonic, sleep
        self.attempts = attempts
        self.deployment_source = deployment_source or (lambda target: deployment_revision(catalog, target))
        self.changes_since = changes_since or (
            lambda previous: git_source_changes(catalog.root, previous, self.revision)
        )
        self.logger = RunLogger(
            self.run.directory, allowed_units=[target.unit_id for target in catalog.targets],
            max_bytes=self.settings.log_max_bytes, backup_count=self.settings.log_backup_count,
            clock=now, monotonic=monotonic, test_run=test_run,
            outbox=event_outbox,
        )
        self.deploy_limit = asyncio.Semaphore(self.settings.deployment_workers)
        self.query_limit = asyncio.Semaphore(self.settings.query_workers)
        self.assessment_limit = asyncio.Semaphore(self.settings.assessment_workers)
        self.integrity_failure = False
        self._stopped = False
        self._initialized = False
        self.lanes = self.run

    def _check(self) -> None:
        if self._stopped:
            raise StateError("runner_stopped")

    def _save(self, records: RecordStore, kind: str, key: str, value: Mapping) -> None:
        self._check()
        try:
            getattr(records, f"save_{kind}")(key, value)
        except StateError:
            self._stopped = True
            raise

    def _event(self, kind: str, target: Target | None = None, **fields: Any) -> None:
        self.logger.emit(kind, unit=target.unit_id if target else None, **fields)

    def initialize(self, targets: tuple[Target, ...], report_date: date, *, kind: str) -> None:
        if kind != self.runtime.environment:
            raise QualityError("run_profile_mismatch")
        if not targets and kind == "daily":
            raise QualityError("run_plan_empty")
        if len({target.key for target in targets}) != len(targets) or any(
            target not in self.catalog.targets for target in targets
        ):
            raise QualityError("run_plan_invalid")
        expected = {
            "kind": kind, "report_date": report_date.isoformat(),
            "test_run": self.test_run, "rerun": self.rerun,
            "targets": [target.key for target in targets],
        }
        existing = self.run.read_completed("run", missing_ok=True)
        if existing is not None:
            if any(existing.get(key) != value for key, value in expected.items()):
                raise QualityError("run_identity_mismatch")
            if not self.test_run and kind == "daily" and existing["source_revision"] != self.revision:
                raise QualityError("official_resume_source_changed")
            self.reuse_run_id = existing.get("reuse_run_id")
        if self.reuse_run_id:
            previous = self.runtime.run(self.reuse_run_id).read_completed("run")
            if (
                previous.get("test_run") is not True
                or previous.get("report_date") != expected["report_date"]
                or previous.get("targets") != expected["targets"]
            ):
                raise QualityError("runner_reuse_invalid")
            self.lanes = self.runtime.run(previous.get("lane_run_id", self.reuse_run_id))
        self._save(self.run, "completed", "environment", asdict(self.cloud.environment))
        if existing is None:
            self._save(self.run, "completed", "run", {
                **expected, "source_revision": self.revision, "started_at": self.now().isoformat(),
                "reuse_run_id": self.reuse_run_id,
                "lane_run_id": self.lanes.directory.name,
            })
        self._save(self.run, "progress", "source", {
            "source_revision": self.revision, "settings": self.settings.to_dict(),
        })
        self.targets = targets
        self._initialized = True
        self._event("resume" if existing else "started")

    async def _gather(self, operations) -> list:
        tasks = [asyncio.create_task(operation) for operation in operations]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            # Structured cancellation: siblings cannot continue side effects after
            # their owner unwinds and releases the environment lock.
            self._stopped = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def _binding(self, target: Target, selection: Selection | None = None) -> _Work:
        key = f"targets/{target.key}/source"
        old = self.run.read(key, missing_ok=True)
        own = old is not None
        if not own and any(
            any((self.run.directory / collection / "targets" / target.key).glob("work-*"))
            for collection in ("progress", "completed", "artifacts")
        ):
            raise StateError("target_checkpoint_missing")
        if old is None and selection is not None and "full" not in selection.reasons:
            old = self.runtime.staging_index.read(target.key, missing_ok=True)
        if old is None and self.reuse_run_id:
            old = self.runtime.run(self.reuse_run_id).read(key, missing_ok=True)
        changed, evaluate, recollect = False, False, False
        if old:
            if not {"source_revision", "traffic_source_revision", "traffic_run_id", "work_key"} <= old.keys():
                raise StateError("target_checkpoint_invalid")
            if not isinstance(old["work_key"], str) or not old["work_key"].startswith(f"targets/{target.key}/work-"):
                raise StateError("target_checkpoint_invalid")
            if old["source_revision"] != self.revision:
                changes = self.changes_since(old["source_revision"])
                recollect = any(
                    (self.catalog.root / path).resolve()
                    == self.catalog.root / "src" / "agent_insights_quality" / "telemetry.py"
                    for path in changes.paths
                )
                choice = next((item for item in select_staging(
                    self.catalog,
                    last_tests={target.key: LastTest(old["source_revision"], "PASS", old["tested_at"])},
                    changed_paths=changes,
                    evaluation_paths=_EVALUATION,
                ) if item.target.key == target.key), None)
                changed = choice is not None and choice.action == "traffic"
                evaluate = choice is not None
            if selection and not own:
                changed |= bool({"full", "deployment_changed", "traffic_changed"} & set(selection.reasons))
                evaluate |= selection.action == "reassess"
        if old and not changed:
            binding = dict(old)
            if evaluate:
                binding.pop("result", None)
                binding.pop("assessment", None)
                binding["refresh_evidence"] = recollect
            if evaluate:
                binding["source_revision"] = self.revision
        else:
            if old:
                records = self.runtime.run(old["traffic_run_id"])
                base = old["work_key"] + "/insights"
                poll = records.read(base + "/poll", missing_ok=True)
                terminal = poll and str(poll.get("status")).casefold() in {"failed", "canceled", "cancelled"}
                if records.read(base + "/start", missing_ok=True) and not records.read_completed(base, missing_ok=True) and not terminal:
                    raise QualityError("prior_insights_unresolved")
            binding = {
                "source_revision": self.revision, "traffic_source_revision": self.revision,
                "traffic_run_id": self.run_id,
                "work_key": f"targets/{target.key}/work-{uuid.uuid4().hex}",
                "tested_at": self.now().isoformat(),
            }
            if selection and selection.action == "reassess":
                raise QualityError("retained_evidence_missing")
        self._save(self.run, "progress", key, binding)
        return _Work(self.runtime.run(binding["traffic_run_id"]), binding)

    def _prior_result(self, work: _Work) -> dict | None:
        result = work.binding.get("result")
        if result is not None:
            reference = work.binding.get("assessment")
            if not isinstance(reference, dict):
                raise StateError("assessment_checkpoint_missing")
            artifact = self.runtime.run(reference["run_id"]).read_artifact(reference["artifact"])
            if any(artifact.get(key) != value for key, value in result.items()):
                raise StateError("assessment_checkpoint_conflict")
        return result

    def _update(self, target: Target, work: _Work, **fields: Any) -> None:
        work.binding.update(fields)
        self._save(self.run, "progress", f"targets/{target.key}/source", work.binding)

    def _deadline(self, records: RecordStore, key: str, seconds: int) -> datetime:
        value = records.read(key, missing_ok=True)
        if value is None:
            value = {"until": (self.now() + timedelta(seconds=seconds)).isoformat()}
            self._save(records, "completed", key, value)
        return _datetime(value["until"])

    async def _wait(self, target: Target, stage: str, deadline: datetime, start: float) -> bool:
        remaining = min(
            (deadline - self.now()).total_seconds(),
            self.settings.poll_timeout_seconds - (self.monotonic() - start),
        )
        if remaining <= 0:
            return False
        self._event("heartbeat", target, stage=stage)
        await self.sleep(min(self.settings.poll_interval_seconds, remaining))
        self._check()
        return True

    async def _retry(self, work: _Work, key: str, target: Target, stage: str) -> bool:
        value = work.records.read(key, missing_ok=True) or {"retries": 0}
        count = value["retries"]
        if count >= self.settings.retry_limit:
            self._save(work.records, "progress", key, {**value, "exhausted": True})
            return False
        self._save(work.records, "progress", key, {"retries": count + 1})
        self._event("retry", target, stage=stage, counters={"retry_count": count + 1})
        await self.sleep(min(
            self.settings.retry_backoff_seconds * 2**count,
            self.settings.retry_max_backoff_seconds,
        ))
        self._check()
        return True

    async def _deployment(self, target: Target, work: _Work) -> Deployment:
        key = work.key + "/deployment"
        if work.records.read_completed(work.key + "/traffic-done", missing_ok=True):
            return Deployment(**work.records.read(key))
        self._event("started", target, stage="deployment")
        raw = work.records.read(key, missing_ok=True)
        existing = Deployment(**raw) if raw else self.registry.get(target.key)
        revision = self.deployment_source(target)
        deadline = self._deadline(work.records, key + "/deadline", self.settings.poll_timeout_seconds)
        start = self.monotonic()
        async with self.deploy_limit:
            while True:
                def persist(value: Deployment) -> None:
                    nonlocal existing
                    self._save(work.records, "progress", key, asdict(value))
                    existing = value
                self._check()
                try:
                    value = await self.cloud.ensure_deployment(target, revision, existing, persist)
                    persist(value)
                    await self.registry.save(value)
                    self._event("completed", target, stage="deployment")
                    return value
                except QualityError as error:
                    self._fatal(error)
                    if error.code in _PENDING:
                        if await self._wait(target, "deployment", deadline, start):
                            continue
                    elif error.retryable and (
                        existing is not None or error.request_accepted is False
                    ) and self.now() < deadline:
                        if await self._retry(work, key + "/retry", target, "deployment"):
                            continue
                    raise

    def _fatal(self, error: QualityError) -> None:
        if isinstance(error, StateError) or error.code == "provider_checkpoint_failed":
            self._stopped = True
            raise error

    @staticmethod
    def _snapshot(work: _Work, key: str) -> Snapshot:
        value = work.records.read_artifact(key)
        try:
            return Snapshot.from_private_dict(value)
        except QualityError as error:
            raise StateError("evidence_checkpoint_invalid") from error

    async def _session(self, target: Target, work: _Work, deployment: Deployment, index: int) -> str | None:
        if target.is_prompt:
            return None
        key = work.key + f"/traffic/attempt-{index:02d}/session"
        saved = work.records.read(key, missing_ok=True)
        if saved and saved["status"] == "ready":
            return saved["session_id"]
        if saved and saved["status"] != "rejected":
            raise QualityError("session_outcome_unresolved")
        retry = work.records.read(key + "/retry", missing_ok=True)
        if saved and retry and retry.get("exhausted"):
            raise QualityError("session_retry_exhausted", request_accepted=False)
        request_id = saved["request_id"] if saved else uuid.uuid4().hex
        while True:
            value = {"request_id": request_id, "status": "submitting"}
            self._save(work.records, "progress", key, value)
            def persist(session_id: str) -> None:
                value["session_id"] = session_id
                self._save(work.records, "progress", key, value)
            try:
                session_id = await self.cloud.create_session(deployment, request_id, persist)
                value.update(session_id=session_id, status="ready")
                self._save(work.records, "completed", key, value)
                return session_id
            except QualityError as error:
                self._fatal(error)
                value.update(
                    status="rejected" if error.request_accepted is False else "unknown",
                    error_code=error.code,
                )
                self._save(work.records, "progress", key, value)
                if error.request_accepted is False and (error.retryable or error.status in {404, 409, 429}):
                    if await self._retry(work, key + "/retry", target, "traffic"):
                        continue
                raise

    async def _invoke(
        self, target: Target, work: _Work, deployment: Deployment, attempt: Attempt,
        step: Step, session: str | None, previous: str | None,
    ) -> Invocation:
        key = work.key + f"/traffic/attempt-{attempt.index:02d}/{step.step_id}"
        raw = work.records.read(key, missing_ok=True)
        if raw and raw["status"] != "blocked":
            receipt = Invocation(**raw)
            if receipt.status == "submitting":
                receipt = replace(receipt, status="unknown", completed_at=self.now().isoformat(),
                                  error_code="invocation_outcome_unresolved")
            self._save(work.records, "completed", key, asdict(receipt))
            return receipt
        while True:
            receipt = Invocation(
                uuid.uuid4().hex, None, session, self.now().isoformat(), "", "submitting",
            )
            def persist(value: Invocation) -> None:
                nonlocal receipt
                self._save(work.records, "progress", key, asdict(value))
                receipt = value
            persist(receipt)
            try:
                receipt = await self.cloud.invoke(
                    deployment, step, request_id=receipt.request_id, session_id=session,
                    previous_response_id=previous if target.is_prompt else None, persist=persist,
                )
            except QualityError as error:
                self._fatal(error)
                self._failure(target, error, "traffic")
                if receipt.status == "submitting":
                    receipt = replace(
                        receipt, status="failed" if error.request_accepted is False else "unknown",
                        completed_at=self.now().isoformat(), error_code=error.code,
                        http_status=error.status,
                    )
                if error.request_accepted is False and error.retryable:
                    self._save(work.records, "artifact", key + "/rejections/" + uuid.uuid4().hex, asdict(receipt))
                    if await self._retry(work, key + "/retry", target, "traffic"):
                        continue
            self._save(work.records, "completed", key, asdict(receipt))
            return receipt

    def _load_traffic(
        self, target: Target, work: _Work, attempts: tuple[Attempt, ...], *, required: bool = True,
    ) -> dict:
        invocations = {}
        for attempt in attempts:
            for step in attempt.steps:
                raw = work.records.read(
                    work.key + f"/traffic/attempt-{attempt.index:02d}/{step.step_id}",
                    missing_ok=True,
                )
                if raw is not None:
                    if required and raw.get("status") == "submitting":
                        raise StateError("traffic_checkpoint_incomplete")
                    invocations[(attempt.index, step.step_id)] = Invocation(**raw)
                elif required:
                    raise StateError("traffic_checkpoint_missing")
        return invocations

    def _plan(self, target: Target, work: _Work) -> tuple[Attempt, ...]:
        attempts = self.attempts(target)
        if [item.index for item in attempts] != list(range(1, 11)):
            raise QualityError("runner_attempt_plan_invalid")
        if work.binding.get("evidence_key") and work.records.read_completed(work.key + "/plan", missing_ok=True) is None:
            raise StateError("traffic_plan_missing")
        execution = {"attempts": [
            {"index": item.index, "parameters": dict(item.parameters), "steps": [
                {"step_id": step.step_id, "phase": step.phase, "body": dict(step.body)}
                for step in item.steps
            ]} for item in attempts
        ]}
        self._save(work.records, "completed", work.key + "/plan", execution)
        return attempts

    async def _traffic(
        self, target: Target, work: _Work, deployment: Deployment, attempts: tuple[Attempt, ...],
    ) -> dict:
        if work.records.read_completed(work.key + "/traffic-done", missing_ok=True):
            invocations = self._load_traffic(target, work, attempts)
            if len(invocations) != sum(len(item.steps) for item in attempts):
                raise StateError("traffic_checkpoint_missing")
            return invocations
        self._event("started", target, stage="traffic")
        self._check()
        await self.cloud.activate(deployment)
        invocations = {}
        for attempt in attempts:
            previous, session, blocked = None, None, None
            try:
                session = await self._session(target, work, deployment, attempt.index)
            except QualityError as error:
                self._fatal(error)
                self._failure(target, error, "traffic")
                if error.code in _INTEGRITY:
                    raise
                blocked = error.code
            for step in attempt.steps:
                if blocked:
                    key = work.key + f"/traffic/attempt-{attempt.index:02d}/{step.step_id}"
                    saved = work.records.read(key, missing_ok=True)
                    receipt = Invocation(**saved) if saved else Invocation(
                        uuid.uuid4().hex, None, session, self.now().isoformat(),
                        self.now().isoformat(), "blocked", error_code=blocked,
                    )
                    self._save(work.records, "progress", key, asdict(receipt))
                else:
                    receipt = await self._invoke(target, work, deployment, attempt, step, session, previous)
                invocations[(attempt.index, step.step_id)] = receipt
                self._event("checkpoint", target, stage="traffic", attempt=attempt.index)
                if receipt.response is None or target.is_prompt and not receipt.response_id:
                    blocked = "conversation_continuation_unavailable"
                if receipt.error_code == "invocation_response_pending":
                    blocked = "invocation_outcome_unresolved"
                previous = receipt.response_id
        if all(item.status != "blocked" for item in invocations.values()):
            self._save(work.records, "completed", work.key + "/traffic-done", {"completed": True})
        return invocations

    @staticmethod
    def _ready_attempts(attempts: tuple[Attempt, ...], invocations: Mapping, snapshot: Snapshot) -> int:
        return sum(any(
            step.phase == "probe"
            and (receipt := invocations.get((attempt.index, step.step_id))) is not None
            and receipt.response is not None
            and receipt.response_id in snapshot.attributable_responses
            for step in attempt.steps
        ) for attempt in attempts)

    async def _evidence(
        self, target: Target, work: _Work, deployment: Deployment,
        attempts: tuple[Attempt, ...], invocations: Mapping,
    ) -> tuple[Snapshot, str]:
        key = work.key + "/evidence"
        self._event("started", target, stage="evidence")
        deadline = self._deadline(work.records, key + "/deadline", self.settings.hydration_seconds)
        start, extra_poll = self.monotonic(), False
        while True:
            self._check()
            try:
                async with self.query_limit:
                    snapshot = await collect_snapshot(
                        self.cloud, deployment, invocations.values(), observed_at=self.now(),
                    )
            except QualityError as error:
                self._fatal(error)
                if error.retryable and await self._wait(target, "evidence", deadline, start):
                    continue
                raise
            artifact = work.key + "/snapshots/" + uuid.uuid4().hex
            self._save(work.records, "artifact", artifact, snapshot.to_private_dict())
            self._save(work.records, "progress", key, {"artifact": artifact})
            ready = self._ready_attempts(attempts, invocations, snapshot)
            if extra_poll and snapshot.query_complete or not await self._wait(target, "evidence", deadline, start):
                return snapshot, artifact
            extra_poll = ready >= self.settings.readiness_attempts and snapshot.query_complete

    def _traffic_window(self, target: Target, work: _Work, invocations: Mapping) -> str | None:
        key = f"agents/{target.unit_id.agent}/last-traffic"
        previous = self.lanes.read(key, missing_ok=True)
        if previous and previous["work_key"] == work.key:
            return previous["prior_end"]
        prior_end = previous["end"] if previous else None
        self._save(self.lanes, "progress", key, {
            "work_key": work.key, "prior_end": prior_end,
            "end": max(_datetime(item.completed_at) for item in invocations.values()).isoformat(),
        })
        return prior_end

    async def _monitor(self, target: Target) -> str:
        base = "agents/" + target.unit_id.agent
        saved = self.lanes.read(base + "/monitor", missing_ok=True)
        if saved is None or saved["status"] == "rejected":
            self._save(self.lanes, "progress", base + "/monitor", {"status": "submitting"})
            try:
                identity = await self.cloud.ensure_monitor(target.runtime_name("daily"))
                saved = {"status": "ready", "id": identity}
                self._save(self.lanes, "completed", base + "/monitor", saved)
            except QualityError as error:
                self._fatal(error)
                self._save(self.lanes, "progress", base + "/monitor", {
                    "status": "rejected" if error.request_accepted is False else "unknown",
                })
                raise
        if saved["status"] != "ready":
            raise QualityError("monitor_creation_unresolved")
        reset_key = base + "/reset"
        reset = self.lanes.read(reset_key, missing_ok=True)
        if reset and reset["status"] not in {"completed", "rejected"}:
            raise QualityError("monitor_reset_unresolved")
        if reset is None or reset["status"] == "rejected":
            work = _Work(self.lanes, {"work_key": base})
            while True:
                self._save(self.lanes, "progress", reset_key, {"status": "submitting"})
                try:
                    await self.cloud.reset_monitor(saved["id"])
                    self._save(self.lanes, "completed", reset_key, {"status": "completed"})
                    break
                except QualityError as error:
                    self._fatal(error)
                    self._save(self.lanes, "progress", reset_key, {
                        "status": "rejected" if error.request_accepted is False else "unknown",
                        "code": error.code,
                    })
                    if error.request_accepted is False and error.retryable:
                        if await self._retry(work, reset_key + "/retry", target, "insights"):
                            continue
                    raise
        return saved["id"]

    async def _insights(
        self, target: Target, work: _Work, monitor: str, invocations: Mapping,
        evidence_key: str, prior_end: str | None,
    ) -> dict:
        base = work.key + "/insights"
        completed = work.records.read_completed(base, missing_ok=True)
        if completed:
            return completed
        self._event("started", target, stage="insights")
        before = work.records.read_artifact(base + "/before", missing_ok=True)
        if before is None:
            before = {"cards": list(await self.cloud.list_insights(monitor))}
            self._save(work.records, "artifact", base + "/before", before)
        intent = work.records.read(base + "/start", missing_ok=True)
        def new_intent() -> dict:
            first = min(_datetime(item.started_at) for item in invocations.values())
            start = self.now()
            allowance = 5.0
            if prior_end:
                gap = (first - _datetime(prior_end)).total_seconds()
                if gap <= 0:
                    raise QualityError("insights_window_not_isolated")
                allowance = min(allowance, gap / 2)
            boundary = self.lanes.read(
                f"agents/{target.unit_id.agent}/successful-window", missing_ok=True,
            )
            value = {
                "operation_id": uuid.uuid4().hex, "monitor_id": monitor,
                "request_body": {"lookback_hours": max((start - first).total_seconds() + allowance, 0.0000001) / 3600},
                "started_at": start.isoformat(), "visible_snapshot": evidence_key,
                "requested_window_start": (first - timedelta(seconds=allowance)).isoformat(),
                "previous_successful_end": boundary["end_latest"] if boundary else None,
                "submission_state": "prepared", "submission_timing": {"possibly_accepted": False},
            }
            self._save(work.records, "progress", base + "/start", value)
            return value
        if intent is None:
            intent = new_intent()
        if intent["monitor_id"] != monitor:
            raise StateError("insights_monitor_changed")
        work.records.read_artifact(intent["visible_snapshot"])
        def persist(value: dict) -> None:
            for field in ("operation_id", "monitor_id", "request_body"):
                if field in value and value[field] != intent[field]:
                    raise StateError("insights_submission_changed")
            local = {key: intent[key] for key in (
                "submission_timing", "started_at", "visible_snapshot",
                "requested_window_start", "previous_successful_end",
            ) if key in intent}
            intent.update(value)
            intent.update(local)
            if value.get("id") or value.get("submission_state") == "accepted":
                intent["submission_timing"].setdefault("response_at", self.now().isoformat())
            self._save(work.records, "progress", base + "/start", intent)
        while not intent.get("id"):
            self._check()
            timing = intent.setdefault("submission_timing", {
                "possibly_accepted": True, "first_post_at": intent["started_at"],
            })
            if intent.get("submission_state") == "rejected" and not timing["possibly_accepted"]:
                self._save(work.records, "artifact", base + "/rejections/" + intent["operation_id"], intent)
                if not intent.get("retryable") or not await self._retry(work, base + "/retry", target, "insights"):
                    raise QualityError(intent["error_code"], request_accepted=False)
                intent = new_intent()
                timing = intent["submission_timing"]
            previously_uncertain = timing["possibly_accepted"]
            submitted = self.now().isoformat()
            timing.setdefault("first_post_at", submitted)
            timing.update(last_post_at=submitted, possibly_accepted=True)
            intent["submission_state"] = "submitting"
            self._save(work.records, "progress", base + "/start", intent)
            try:
                value = await self.cloud.start_insights(
                    monitor, intent["request_body"]["lookback_hours"], intent["operation_id"], persist,
                )
                persist(value)
                if not intent.get("id"):
                    raise QualityError("insights_start_unresolved")
            except QualityError as error:
                self._fatal(error)
                if intent.get("id"):
                    break
                if error.request_accepted is False and not previously_uncertain and "response_at" not in timing:
                    timing["possibly_accepted"] = False
                    intent.update(submission_state="rejected", retryable=error.retryable, error_code=error.code)
                    self._save(work.records, "progress", base + "/start", intent)
                    continue
                if error.request_accepted is True:
                    timing.setdefault("response_at", self.now().isoformat())
                intent["submission_state"] = "unknown" if error.request_accepted is not True else "accepted"
                self._save(work.records, "progress", base + "/start", intent)
                if error.retryable and await self._retry(work, base + "/retry", target, "insights"):
                    continue
                raise
        deadline = self._deadline(work.records, base + "/deadline", self.settings.poll_timeout_seconds)
        start = self.monotonic()
        while True:
            self._check()
            try:
                result = await self.cloud.get_insights_run(monitor, intent["id"])
                self._save(work.records, "progress", base + "/poll", result)
            except QualityError as error:
                self._fatal(error)
                if error.retryable and await self._wait(target, "insights", deadline, start):
                    continue
                raise
            status = str(result.get("status", "")).casefold()
            if status == "succeeded":
                break
            if status in {"failed", "canceled", "cancelled"}:
                raise QualityError("insights_run_failed", request_accepted=True)
            if not await self._wait(target, "insights", deadline, start):
                raise QualityError("insights_poll_timeout", request_accepted=True)
        after = work.records.read_artifact(base + "/after", missing_ok=True)
        if after is None:
            after = {"cards": list(await self.cloud.list_insights(monitor))}
            self._save(work.records, "artifact", base + "/after", after)
        completed = {
            "before": before["cards"], "after": after["cards"], "run": result,
            "visible_snapshot": intent["visible_snapshot"],
            "engine_window": self._engine_window(intent, result),
        }
        completed["started_at"] = completed["engine_window"]["admission_earliest"]
        self._save(self.lanes, "progress", f"agents/{target.unit_id.agent}/successful-window", {
            "work_key": work.key, "end_latest": completed["engine_window"]["end_latest"],
        })
        self._save(work.records, "completed", base, completed)
        return completed

    def _engine_window(self, intent: dict, result: dict) -> dict:
        timing = intent.get("submission_timing", {})
        first = _datetime(timing.get("first_post_at", intent["started_at"]))
        response = _datetime(timing.get("response_at", self.now().isoformat()))
        lookback = timedelta(hours=intent["request_body"]["lookback_hours"])
        previous = intent.get("previous_successful_end")
        latest_start = response - lookback
        if previous:
            latest_start = max(latest_start, _datetime(previous))
        window = {
            "basis": "bounded_submission", "admission_earliest": first.isoformat(),
            "admission_latest": response.isoformat(), "start_latest": latest_start.isoformat(),
            "end_earliest": first.isoformat(), "end_latest": response.isoformat(),
            "previous_successful_end": previous, "reasons": [],
        }
        # Window fields are optional metadata, never a required wire contract.
        # With only id/status, the response bounds the admission-relative window.
        provider = intent.get("provider_response", intent)
        metadata = {**provider, **result}
        supplied = {key: metadata[key] for key in ("window_start", "window_end") if key in metadata}
        if supplied:
            try:
                lower = _datetime(supplied["window_start"])
                upper = _datetime(supplied["window_end"])
                if lower >= upper or previous and lower < _datetime(previous):
                    raise ValueError("Window ordering")
            except (KeyError, StateError, ValueError):
                window["reasons"].append("insights_window_metadata_invalid")
            else:
                window.update(
                    basis="provider_window", admission_earliest=upper.isoformat(),
                    admission_latest=upper.isoformat(), start_latest=lower.isoformat(),
                    end_earliest=upper.isoformat(), end_latest=upper.isoformat(),
                )
        if response < first:
            window["reasons"].append("insights_submission_clock_invalid")
        return window

    def _engine_visible(
        self, attempts: tuple[Attempt, ...], invocations: Mapping,
        snapshot: Snapshot, insight: dict,
    ) -> tuple[Snapshot, dict]:
        window = dict(insight.get("engine_window") or {})
        if not window:
            window = {
                "basis": "unavailable", "coverage_proven": False,
                "reasons": ["insights_window_unavailable"],
            }
            covered = set()
        elif window["reasons"]:
            covered = set()
        else:
            lower, upper = _datetime(window["start_latest"]), _datetime(window["end_earliest"])
            covered = {
                value.response_id for value in invocations.values()
                if value.response is not None and value.response_id
                and lower <= _datetime(value.started_at) <= _datetime(value.completed_at) <= upper
            }
        refs = set()
        if covered:
            for row in snapshot.records:
                raw = _flat(row["raw"])
                timestamp = raw.get("timestamp", raw.get("TimeGenerated", raw.get("Timestamp")))
                try:
                    if lower <= _datetime(timestamp) <= upper:
                        refs.add(row["ref"])
                except StateError:
                    # A missing timestamp cannot prove this record was in the
                    # admitted window; other attributable anchors may still do so.
                    continue
        scopes = tuple(
            replace(
                scope,
                anchor_refs=tuple(ref for ref in scope.anchor_refs if ref in refs),
                evidence_refs=tuple(ref for ref in scope.evidence_refs if ref in refs),
                reasons=scope.reasons if scope.response_id in covered else (
                    *scope.reasons, "outside_proven_insights_window",
                ),
            )
            for scope in snapshot.scopes
        )
        visible = replace(snapshot, scopes=scopes)
        window["attributable_probe_attempts"] = self._ready_attempts(attempts, invocations, visible)
        window["coverage_proven"] = window["attributable_probe_attempts"] >= self.settings.readiness_attempts
        if not window["coverage_proven"]:
            window["reasons"] = list(dict.fromkeys([*window["reasons"], "insights_window_coverage_unproven"]))
        return visible, window

    def _failure(self, target: Target, error: QualityError, stage: str) -> None:
        self._fatal(error)
        if error.code in _INTEGRITY:
            self.integrity_failure = True
            self._save(self.run, "completed", "integrity-failure", {"failed": True})
        self._event("failure", target, stage=stage, code=error.code)
        value = {"stage": stage, "code": error.code, "at": self.now().isoformat()}
        detail = getattr(error, "private_detail", None)
        if detail is not None:
            self._save(self.run, "artifact", f"targets/{target.key}/errors/{uuid.uuid4().hex}", detail)
        self._save(self.run, "progress", f"targets/{target.key}/failure", value)

    def _apply_assessment(self, target: Target, work: _Work, reference: dict, result: dict) -> None:
        if self.runtime.environment == "staging":
            fields = {
                "result": {key: result[key] for key in ("status", "passing_attempts", "reasons")},
                "status": result["status"],
            }
        else:
            restore_unit(result["unit_result"])
            fields = {"result": {key: result[key] for key in ("unit_result", "reasons")}}
        self._update(
            target, work, **fields, assessment={"run_id": self.run_id, "artifact": reference["artifact"]},
            assessed_at=self.now().isoformat(),
        )
        self._save(self.run, "progress", f"targets/{target.key}/assessment", {
            **reference, "status": "applied",
        })

    def _recover_assessment(self, target: Target, work: _Work) -> bool:
        reference = self.run.read(f"targets/{target.key}/assessment", missing_ok=True)
        if not reference or (
            reference["source_revision"] != self.revision
            or reference.get("work_key", work.key) != work.key
            or reference["status"] not in {"pending", "saved"}
        ):
            return False
        result = self.run.read_artifact(reference["artifact"], missing_ok=reference["status"] == "pending")
        if result is None:
            return False
        self._apply_assessment(target, work, reference, result)
        return True

    async def _assess(self, target: Target, work: _Work, operation: Callable[[], Awaitable]) -> dict:
        pending_key = f"targets/{target.key}/assessment"
        pending = self.run.read(pending_key, missing_ok=True)
        if pending and (
            pending["source_revision"] == self.revision
            and pending.get("work_key", work.key) == work.key
            and pending["status"] in {"pending", "saved"}
        ):
            artifact = pending["artifact"]
            saved = self.run.read_artifact(artifact, missing_ok=True)
        else:
            artifact = f"targets/{target.key}/assessments/{uuid.uuid4().hex}"
            saved = None
        reference = {"source_revision": self.revision, "work_key": work.key, "artifact": artifact}
        self._save(self.run, "progress", pending_key, {**reference, "status": "pending"})
        if saved is None:
            self._event("started", target, stage="assessment")
            async with self.assessment_limit:
                self._check()
                result = await operation()
                saved = result.to_private_dict()
            self._save(self.run, "artifact", artifact, saved)
        self._save(self.run, "progress", pending_key, {**reference, "status": "saved"})
        self._apply_assessment(target, work, reference, saved)
        return saved

    async def run_staging(self, selections: tuple[Selection, ...]) -> dict[str, Any]:
        from .assessment import assess_staging

        if not self._initialized or self.runtime.environment != "staging":
            raise QualityError("runner_not_initialized")
        semaphore = asyncio.Semaphore(self.settings.staging_workers)
        async def one(selection: Selection) -> dict:
            target = selection.target
            async with semaphore:
                work = None
                stage = "deployment"
                try:
                    work = self._binding(target, selection)
                    attempts = self._plan(target, work)
                    recovered = self._recover_assessment(target, work)
                    prior = self._prior_result(work)
                    if recovered or prior and prior["status"] in {"PASS", "FAIL"}:
                        self._load_traffic(target, work, attempts)
                        self._snapshot(work, work.binding["evidence_key"])
                        self._save(self.runtime.staging_index, "progress", target.key, work.binding)
                        return {"unit": target.key, **work.binding}
                    if selection.action == "reassess":
                        stage = "evidence"
                        invocations = self._load_traffic(target, work, attempts)
                        if work.binding.get("refresh_evidence"):
                            deployment = Deployment(**work.records.read(work.key + "/deployment"))
                            snapshot, artifact = await self._evidence(
                                target, work, deployment, attempts, invocations,
                            )
                            self._update(target, work, evidence_key=artifact, refresh_evidence=False)
                        else:
                            snapshot = self._snapshot(work, work.binding["evidence_key"])
                    else:
                        deployment = await self._deployment(target, work)
                        stage = "traffic"
                        invocations = await self._traffic(target, work, deployment, attempts)
                        stage = "evidence"
                        snapshot, artifact = await self._evidence(target, work, deployment, attempts, invocations)
                        self._update(target, work, evidence_key=artifact)
                    stage = "assessment"
                    await self._assess(target, work, lambda: assess_staging(
                        target, attempts, invocations, snapshot, self.sol,
                    ))
                except QualityError as error:
                    self._failure(target, error, stage)
                    if work is None:
                        raise
                    self._update(target, work, status="INCOMPLETE", reason=error.code)
                self._save(self.runtime.staging_index, "progress", target.key, work.binding)
                return {"unit": target.key, **work.binding}
        records = await self._gather(one(item) for item in selections)
        summary = {
            "profile": "staging", "selected": len(selections), "results": records,
            "integrity_failure": self.integrity_failure,
        }
        self._save(self.run, "progress", "staging-result", summary)
        self._event("completed")
        return summary

    async def run_daily(self, targets: tuple[Target, ...]) -> QualityResult:
        from .assessment import assess_daily

        if not self._initialized or self.runtime.environment != "daily" or targets != self.targets:
            raise QualityError("runner_not_initialized")
        agents = tuple(dict.fromkeys(target.unit_id.agent for target in targets))
        if any(not next(target for target in targets if target.unit_id.agent == agent).is_baseline for agent in agents):
            raise QualityError("daily_baseline_must_be_first")
        prepared, outcomes = {}, {}
        lane_limit = asyncio.Semaphore(self.settings.daily_lanes)
        async def lane(agent: str) -> None:
            async with lane_limit:
                blocked = False
                for target in (item for item in targets if item.unit_id.agent == agent):
                    work = None
                    prior = None
                    stage = "deployment"
                    try:
                        work = self._binding(target)
                        attempts = self._plan(target, work)
                        recovered = self._recover_assessment(target, work)
                        prior = self._prior_result(work)
                        if recovered or prior and not prior["unit_result"]["exclusion_reasons"]:
                            self._load_traffic(target, work, attempts)
                            work.records.read_completed(work.key + "/insights")
                            self._snapshot(work, work.binding["evidence_key"])
                            outcomes[target.key] = restore_unit(prior["unit_result"])
                            continue
                        insight = work.records.read_completed(work.key + "/insights", missing_ok=True)
                        if insight:
                            invocations = self._load_traffic(target, work, attempts)
                            if work.binding.get("refresh_evidence") or prior and (
                                ExclusionReason.INCOMPLETE_EVIDENCE.value
                                in prior["unit_result"]["exclusion_reasons"]
                            ):
                                deployment = Deployment(**work.records.read(work.key + "/deployment"))
                                snapshot, artifact = await self._evidence(
                                    target, work, deployment, attempts, invocations,
                                )
                                self._update(target, work, evidence_key=artifact, refresh_evidence=False)
                            else:
                                snapshot = self._snapshot(
                                    work, work.binding.get("evidence_key", insight["visible_snapshot"]),
                                )
                        else:
                            if blocked:
                                outcomes[target.key] = restore_unit(prior["unit_result"]) if prior else UnitResult(
                                    target.unit_id, exclusion_reasons=(ExclusionReason.INCOMPLETE_EXECUTION,),
                                )
                                continue
                            deployment = await self._deployment(target, work)
                            stage = "insights"
                            monitor = await self._monitor(target)
                            stage = "traffic"
                            invocations = await self._traffic(target, work, deployment, attempts)
                            prior_end = self._traffic_window(target, work, invocations)
                            if any(
                                item.status == "unknown" or item.error_code == "invocation_response_pending"
                                for item in invocations.values()
                            ):
                                raise QualityError("invocation_outcome_unresolved")
                            intent = work.records.read(work.key + "/insights/start", missing_ok=True)
                            stage = "evidence"
                            if intent:
                                evidence_key = intent["visible_snapshot"]
                                snapshot = self._snapshot(work, evidence_key)
                            else:
                                snapshot, evidence_key = await self._evidence(
                                    target, work, deployment, attempts, invocations,
                                )
                            self._update(target, work, evidence_key=evidence_key)
                            if self._ready_attempts(attempts, invocations, snapshot) < self.settings.readiness_attempts:
                                raise QualityError("trace_readiness_insufficient")
                            stage = "insights"
                            insight = await self._insights(
                                target, work, monitor, invocations, evidence_key, prior_end,
                            )
                        visible, window = self._engine_visible(
                            attempts, invocations, self._snapshot(work, insight["visible_snapshot"]), insight,
                        )
                        prepared[target.key] = (target, work, attempts, invocations, snapshot, visible, insight, window)
                    except QualityError as error:
                        self._failure(target, error, stage)
                        previous = restore_unit(prior["unit_result"]) if prior else UnitResult(target.unit_id)
                        reason = (
                            ExclusionReason.INCOMPLETE_EVIDENCE if stage == "evidence"
                            else ExclusionReason.INCOMPLETE_EXECUTION
                        )
                        outcomes[target.key] = replace(
                            previous, exclusion_reasons=tuple(sorted(
                                {*previous.exclusion_reasons, reason}, key=lambda item: item.value,
                            )),
                        )
                        # Terminal failures/missing traces can be excluded. The next
                        # exact lookback must start after their retained traffic.
                        active_start = (
                            work.records.read(work.key + "/insights/start", missing_ok=True) if work else None
                        )
                        blocked = (
                            error.code in {
                                "monitor_creation_unresolved", "monitor_reset_unresolved",
                                "insights_reset_pending", "deployment_route_unconfirmed",
                                "prior_insights_unresolved", "invocation_outcome_unresolved",
                            }
                            or active_start is not None and error.code != "insights_run_failed"
                            or error.code in _INTEGRITY
                            or self._lane_unresolved(agent)
                        )
        await self._gather(lane(agent) for agent in agents)
        async def assess(values) -> None:
            target, work, attempts, invocations, snapshot, visible, insight, window = values
            try:
                result = await self._assess(target, work, lambda: assess_daily(
                    target, attempts, invocations, snapshot, self.sol,
                    before_cards=tuple(insight["before"]), after_cards=tuple(insight["after"]),
                    engine_started_at=insight["started_at"], visible_snapshot=visible, engine_window=window,
                ))
                outcomes[target.key] = restore_unit(result["unit_result"])
            except QualityError as error:
                self._failure(target, error, "assessment")
                prior = self._prior_result(work)
                previous = restore_unit(prior["unit_result"]) if prior else UnitResult(target.unit_id)
                outcomes[target.key] = replace(
                    previous, exclusion_reasons=tuple(sorted(
                        {*previous.exclusion_reasons, ExclusionReason.INCOMPLETE_ASSESSMENT},
                        key=lambda item: item.value,
                    )),
                )
        await self._gather(assess(value) for value in prepared.values())
        self.integrity_failure |= self.run.read_completed("integrity-failure", missing_ok=True) is not None
        result = aggregate_results(
            planned_units(targets), (outcomes[target.key] for target in targets),
            integrity_failure=self.integrity_failure,
        )
        artifact = "results/" + uuid.uuid4().hex
        self._save(self.run, "artifact", artifact, result.to_dict())
        self._save(self.run, "progress", "quality-result", {
            "artifact": artifact, "source_revision": self.revision,
        })
        self._event("completed", stage="report")
        return result

    def _lane_unresolved(self, agent: str) -> bool:
        for stage, final in (("monitor", "ready"), ("reset", "completed")):
            value = self.lanes.read(f"agents/{agent}/{stage}", missing_ok=True)
            if value and value["status"] not in {final, "rejected"}:
                return True
        return False
