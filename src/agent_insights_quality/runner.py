from __future__ import annotations

import asyncio
import json
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from agent_insights_quality.catalogs import Catalog
from agent_insights_quality.contracts import (
    Attempt, CloudPort, Deployment, Invocation, SolPort, Target,
)
from agent_insights_quality.errors import QualityError
from agent_insights_quality.events import RunLogger
from agent_insights_quality.registry import DeploymentRegistry
from agent_insights_quality.results import (
    CardVerdict, Contribution, CoreVerdict, DiagnosticVerdict, ExclusionReason,
    PlannedUnit, QualityResult, UnitId, UnitResult, aggregate_results,
)
from agent_insights_quality.selection import (
    LastTest, Selection, deployment_inputs, git_source_changes, select_staging,
)
from agent_insights_quality.settings import RuntimeSettings
from agent_insights_quality.state import RuntimeStore
from agent_insights_quality.telemetry import Snapshot, collect_snapshot
from agent_insights_quality.traffic import load_attempts

_PENDING_DEPLOYMENTS = {
    "deployment_pending", "deployment_propagation_pending",
    "deployment_create_identity_pending", "deployment_not_ready",
}
_INTEGRITY_ERRORS = {
    "deployment_identity_mismatch", "deployment_version_mismatch",
    "session_version_mismatch", "duplicate_endpoint_response",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def _date_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise QualityError("timestamp_invalid")
    return result.astimezone(UTC)


def source_revision(catalog: Catalog) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=catalog.root,
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise QualityError("source_revision_unavailable")
    return result.stdout.strip()


def deployment_revision(catalog: Catalog, target: Target) -> str:
    paths = [str(path.relative_to(catalog.root)) for path in deployment_inputs(target)]
    result = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", *paths],
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
    return UnitResult(
        UnitId(**value["unit_id"]),
        cards=tuple(CardVerdict(
            card_alias=item["card_alias"], core=CoreVerdict(item["core"]),
            root_cause_alias=item.get("root_cause_alias"),
            contribution=Contribution(item["contribution"]),
            severity=DiagnosticVerdict(item["severity"]),
            proposed_fix=DiagnosticVerdict(item["proposed_fix"]),
            summary=item.get("summary"),
        ) for item in value["cards"]),
        exclusion_reasons=tuple(ExclusionReason(item) for item in value["exclusion_reasons"]),
        summary=value.get("summary"),
    )


def choose_staging(catalog: Catalog, store: RuntimeStore, *, full: bool = False):
    previous: dict[str, LastTest] = {}
    for target in catalog.targets:
        record = store.staging_index.read(target.key, missing_ok=True)
        if record is not None:
            previous[target.key] = LastTest(
                record["source_revision"], record["status"], record["tested_at"],
            )
    changes = {
        revision: git_source_changes(catalog.root, revision)
        for revision in {item.source_revision for item in previous.values()}
    }
    return select_staging(
        catalog, last_tests=previous, changes_by_revision=changes, full=full,
        evaluation_paths=(
            catalog.root / "src" / "agent_insights_quality" / "assessment.py",
            catalog.root / "src" / "agent_insights_quality" / "telemetry.py",
            catalog.root / "src" / "agent_insights_quality" / "prompts",
        ),
    )


class Runner:
    """One code-driven run; the caller holds RuntimeStore.ownership for writes."""

    def __init__(
        self,
        catalog: Catalog,
        runtime: RuntimeStore,
        run_id: str,
        cloud: CloudPort,
        sol: SolPort,
        registry: DeploymentRegistry,
        *,
        settings: RuntimeSettings | None = None,
        test_run: bool = False,
        now: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.catalog, self.runtime = catalog, runtime
        self.run_id, self.run = run_id, runtime.run(run_id)
        self.cloud, self.sol, self.registry = cloud, sol, registry
        self.settings = settings or RuntimeSettings()
        self.now, self.sleep = now, sleep
        self.logger = RunLogger(
            self.run.directory, allowed_units=[target.unit_id for target in catalog.targets],
            test_run=test_run,
        )
        self.deploy_limit = asyncio.Semaphore(self.settings.deployment_workers)
        self.query_limit = asyncio.Semaphore(self.settings.query_workers)
        self.assessment_limit = asyncio.Semaphore(self.settings.assessment_workers)
        self.integrity_failure = False

    def _event(self, kind: str, target: Target | None = None, **fields) -> None:
        self.logger.emit(kind, unit=target.unit_id if target else None, **fields)

    def initialize(self, targets: tuple[Target, ...], report_date: date, *, kind: str) -> None:
        expected = {
            "kind": kind, "report_date": report_date.isoformat(),
            "source_revision": source_revision(self.catalog),
            "targets": [target.key for target in targets],
            "settings": self.settings.to_dict(),
        }
        existing = self.run.read_completed("run", missing_ok=True)
        if existing is None:
            self.run.save_completed("run", {
                **expected, "started_at": self.now().isoformat(),
            })
        elif existing["kind"] != kind or existing["report_date"] != expected["report_date"]:
            raise QualityError("run_identity_mismatch")
        elif existing["targets"] != expected["targets"]:
            raise QualityError("run_selection_changed")
        self._event("resume" if existing else "started")

    async def _deployment(self, target: Target) -> Deployment:
        key = f"targets/{target.key}/deployment"
        previous = self.run.read(key, missing_ok=True)
        existing = Deployment(**previous) if previous else self.registry.get(target.key)
        revision = deployment_revision(self.catalog, target)
        deadline = time.monotonic() + self.settings.poll_timeout_seconds
        async with self.deploy_limit:
            while True:
                latest: list[Deployment] = []

                def persist(value: Deployment) -> None:
                    latest.append(value)
                    self.run.save_progress(key, asdict(value))

                try:
                    deployment = await self.cloud.ensure_deployment(
                        target, revision, existing, persist,
                    )
                    await self.registry.save(deployment)
                    self._event("completed", target, stage="deployment")
                    return deployment
                except QualityError as error:
                    if latest:
                        existing = latest[-1]
                    pending = error.code in _PENDING_DEPLOYMENTS or (
                        existing is not None
                        and existing.details.get("provisioning_state") == "pending"
                        and error.retryable
                    )
                    if not pending or time.monotonic() >= deadline:
                        raise
                    self._event("heartbeat", target, stage="deployment", code=error.code)
                    await self.sleep(self.settings.poll_interval_seconds)

    async def _session(self, target: Target, deployment: Deployment, index: int) -> str | None:
        if target.is_prompt:
            return None
        key = f"targets/{target.key}/traffic/attempt-{index:02d}/session"
        saved = self.run.read(key, missing_ok=True)
        if saved and saved.get("status") == "ready":
            return saved["session_id"]
        if saved and saved.get("status") not in {"rejected", "ready"}:
            raise QualityError("session_outcome_unresolved", request_accepted=None)
        request_id = saved.get("request_id") if saved else uuid.uuid4().hex
        for retry in range(self.settings.retry_limit + 1):
            value = {"request_id": request_id, "status": "submitting"}
            self.run.save_progress(key, value)

            def persist(session_id: str) -> None:
                value["session_id"] = session_id
                self.run.save_progress(key, value)

            try:
                session_id = await self.cloud.create_session(deployment, request_id, persist)
                self.run.save_completed(key, {
                    "request_id": request_id, "session_id": session_id, "status": "ready",
                })
                return session_id
            except QualityError as error:
                value.update(
                    status="rejected" if error.request_accepted is False else "unknown",
                    error_code=error.code,
                )
                self.run.save_progress(key, value)
                if (
                    error.request_accepted is not False
                    or error.status not in {404, 409, 429}
                    or retry == self.settings.retry_limit
                ):
                    raise
                await self.sleep(min(
                    self.settings.retry_backoff_seconds * 2**retry,
                    self.settings.retry_max_backoff_seconds,
                ))
        raise QualityError("session_unavailable")

    async def _traffic(
        self, target: Target, deployment: Deployment, attempts: tuple[Attempt, ...],
    ) -> dict[tuple[int, str], Invocation]:
        await self.cloud.activate(deployment)
        invocations: dict[tuple[int, str], Invocation] = {}
        for attempt in attempts:
            previous_response = None
            try:
                session = await self._session(target, deployment, attempt.index)
            except QualityError as error:
                self._failure(target, error, "traffic")
                continue
            for step in attempt.steps:
                key = f"targets/{target.key}/traffic/attempt-{attempt.index:02d}/{step.step_id}"
                saved = self.run.read(key, missing_ok=True)
                if saved and saved.get("status") in {"completed", "failed", "incomplete", "unknown"}:
                    result = Invocation(**saved)
                elif saved:
                    result = Invocation(
                        saved["request_id"], None, session, saved["started_at"],
                        self.now().isoformat(), "unknown", error_code="invocation_outcome_unknown",
                    )
                    self.run.save_completed(key, asdict(result))
                else:
                    request_id = uuid.uuid4().hex
                    latest: list[Invocation] = []

                    def persist(value: Invocation) -> None:
                        latest.append(value)
                        self.run.save_progress(key, asdict(value))

                    try:
                        result = await self.cloud.invoke(
                            deployment, step, request_id=request_id,
                            session_id=session,
                            previous_response_id=previous_response if target.is_prompt else None,
                            persist=persist,
                        )
                    except QualityError as error:
                        self._failure(target, error, "traffic")
                        result = latest[-1] if latest else Invocation(
                            request_id, None, session, self.now().isoformat(),
                            self.now().isoformat(), "failed", error_code=error.code,
                        )
                        if result.status == "submitting":
                            result = Invocation(
                                result.request_id, None, session, result.started_at,
                                self.now().isoformat(), "unknown", error_code=error.code,
                            )
                    self.run.save_completed(key, asdict(result))
                invocations[(attempt.index, step.step_id)] = result
                self._event("checkpoint", target, stage="traffic", attempt=attempt.index)
                if result.status != "completed" or not result.response_id:
                    break
                previous_response = result.response_id
        return invocations

    def _ready_attempts(
        self, attempts: tuple[Attempt, ...],
        invocations: Mapping[tuple[int, str], Invocation], snapshot: Snapshot,
    ) -> int:
        ready = snapshot.attributable_responses
        return sum(all(
            (receipt := invocations.get((attempt.index, step.step_id))) is not None
            and receipt.status == "completed" and receipt.response_id in ready
            for step in attempt.steps if step.phase == "probe"
        ) for attempt in attempts)

    async def _evidence(
        self, target: Target, deployment: Deployment, attempts: tuple[Attempt, ...],
        invocations: Mapping[tuple[int, str], Invocation],
    ) -> Snapshot:
        key = f"targets/{target.key}/evidence"
        previous = self.run.read_artifact(key, missing_ok=True)
        if previous:
            return Snapshot.from_private_dict(previous)
        if not invocations:
            raise QualityError("endpoint_evidence_missing")
        completed = max(_date_time(item.completed_at) for item in invocations.values())
        deadline = completed + timedelta(seconds=self.settings.hydration_seconds)
        last_signature = None
        while True:
            async with self.query_limit:
                snapshot = await collect_snapshot(self.cloud, deployment, invocations.values())
            self.run.save_progress(f"{key}/latest", snapshot.to_private_dict())
            signature = json.dumps(snapshot.records, sort_keys=True)
            ready = self._ready_attempts(attempts, invocations, snapshot)
            at_deadline = self.now() >= deadline
            if snapshot.query_complete and ready >= self.settings.readiness_attempts:
                if signature == last_signature or at_deadline:
                    self.run.save_artifact(key, snapshot.to_private_dict())
                    return snapshot
            if at_deadline:
                self.run.save_artifact(key, snapshot.to_private_dict())
                return snapshot
            last_signature = signature
            self._event("heartbeat", target, stage="evidence", counters={
                "completed_count": ready, "attempt_count": len(attempts),
            })
            await self.sleep(self.settings.poll_interval_seconds)

    async def _insights(
        self, target: Target, deployment: Deployment, invocations: Mapping,
    ) -> tuple[tuple[dict, ...], tuple[dict, ...], str]:
        base = f"targets/{target.key}/insights"
        completed = self.run.read_completed(base, missing_ok=True)
        if completed:
            return tuple(completed["before"]), tuple(completed["after"]), completed["started_at"]
        agent_base = f"agents/{target.unit_id.agent}"
        monitor = self.run.read_completed(f"{agent_base}/monitor", missing_ok=True)
        if monitor is None:
            monitor = {"id": await self.cloud.ensure_monitor(deployment.agent_name)}
            self.run.save_completed(f"{agent_base}/monitor", monitor)
        reset = self.run.read(f"{agent_base}/reset", missing_ok=True)
        if reset is None:
            self.run.save_progress(f"{agent_base}/reset", {"status": "submitting"})
            await self.cloud.reset_monitor(monitor["id"])
            self.run.save_completed(f"{agent_base}/reset", {"status": "completed"})
        elif reset["status"] != "completed":
            raise QualityError("monitor_reset_unresolved")
        before = self.run.read_artifact(f"{base}/before", missing_ok=True)
        if before is None:
            before = {"cards": list(await self.cloud.list_insights(monitor["id"]))}
            self.run.save_artifact(f"{base}/before", before)
        intent = self.run.read(f"{base}/start", missing_ok=True)
        if intent is None:
            first = min(_date_time(item.started_at) for item in invocations.values())
            start = self.now()
            intent = {
                "operation_id": uuid.uuid4().hex, "monitor_id": monitor["id"],
                "request_body": {
                    "lookback_hours": max((start - first).total_seconds() + 5, 0.001) / 3600,
                },
                "started_at": start.isoformat(),
            }
            self.run.save_progress(f"{base}/start", intent)
        started_at = intent.get("started_at") or self.now().isoformat()

        def persist(value: dict) -> None:
            intent.update(value)
            intent["started_at"] = started_at
            self.run.save_progress(f"{base}/start", intent)

        if not intent.get("id"):
            for retry in range(self.settings.retry_limit + 1):
                try:
                    value = await self.cloud.start_insights(
                        monitor["id"], intent["request_body"]["lookback_hours"],
                        intent["operation_id"], persist,
                    )
                    persist(value)
                    break
                except QualityError as error:
                    if not error.retryable or retry == self.settings.retry_limit:
                        raise
                    await self.sleep(self.settings.retry_backoff_seconds * (retry + 1))
        if not intent.get("id"):
            raise QualityError("insights_start_unresolved")
        deadline = time.monotonic() + self.settings.poll_timeout_seconds
        while True:
            result = await self.cloud.get_insights_run(monitor["id"], intent["id"])
            status = str(result.get("status") or "").lower()
            self.run.save_progress(f"{base}/poll", result)
            if status == "succeeded":
                break
            if status in {"failed", "canceled", "cancelled"}:
                raise QualityError("insights_run_failed")
            if time.monotonic() >= deadline:
                raise QualityError("insights_poll_timeout", request_accepted=True)
            self._event("heartbeat", target, stage="insights")
            await self.sleep(self.settings.poll_interval_seconds)
        after = tuple(await self.cloud.list_insights(monitor["id"]))
        self.run.save_completed(base, {
            "before": before["cards"], "after": list(after),
            "started_at": started_at, "run": result,
        })
        return tuple(before["cards"]), after, started_at

    def _failure(self, target: Target, error: QualityError, stage: str) -> None:
        if error.code in _INTEGRITY_ERRORS:
            self.integrity_failure = True
        self._event("failure", target, stage=stage, code=error.code)
        self.run.save_progress(f"targets/{target.key}/failure", {
            "stage": stage, "code": error.code, "at": self.now().isoformat(),
        })

    async def run_staging(self, selections: tuple[Selection, ...]) -> dict[str, Any]:
        from agent_insights_quality.assessment import assess_staging

        semaphore = asyncio.Semaphore(self.settings.staging_workers)

        async def one(selection: Selection):
            target = selection.target
            async with semaphore:
                try:
                    deployment = await self._deployment(target)
                    attempts = load_attempts(target)
                    invocations = await self._traffic(target, deployment, attempts)
                    snapshot = await self._evidence(target, deployment, attempts, invocations)
                    async with self.assessment_limit:
                        result = await assess_staging(target, attempts, invocations, snapshot, self.sol)
                    self.run.save_artifact(f"targets/{target.key}/assessment", result.to_private_dict())
                    record = {
                        "source_revision": source_revision(self.catalog),
                        "status": result.status, "tested_at": self.now().isoformat(),
                        "run_id": self.run_id, "passing_attempts": result.passing_attempts,
                    }
                except QualityError as error:
                    self._failure(target, error, "assessment")
                    record = {
                        "source_revision": source_revision(self.catalog), "status": "INCOMPLETE",
                        "tested_at": self.now().isoformat(), "run_id": self.run_id,
                        "passing_attempts": 0, "reason": error.code,
                    }
                self.runtime.staging_index.save_progress(target.key, record)
                self.run.save_progress(f"targets/{target.key}/result", record)
                return {"unit": target.key, **record}

        records = await asyncio.gather(*(one(item) for item in selections))
        return {
            "profile": "staging", "selected": len(selections),
            "results": records, "integrity_failure": self.integrity_failure,
        }

    async def run_daily(self, targets: tuple[Target, ...]) -> QualityResult:
        from agent_insights_quality.assessment import assess_daily

        prepared: dict[str, tuple] = {}
        outcomes: dict[str, UnitResult] = {}
        lane_limit = asyncio.Semaphore(self.settings.daily_lanes)

        async def lane(agent: str):
            async with lane_limit:
                for target in (item for item in targets if item.unit_id.agent == agent):
                    saved = self.run.read_completed(f"targets/{target.key}/unit-result", missing_ok=True)
                    if saved:
                        outcomes[target.key] = restore_unit(saved)
                        continue
                    try:
                        deployment = await self._deployment(target)
                        attempts = load_attempts(target)
                        invocations = await self._traffic(target, deployment, attempts)
                        snapshot = await self._evidence(target, deployment, attempts, invocations)
                        if (
                            not snapshot.query_complete
                            or self._ready_attempts(attempts, invocations, snapshot)
                            < self.settings.readiness_attempts
                        ):
                            raise QualityError("trace_readiness_insufficient")
                        before, after, started = await self._insights(target, deployment, invocations)
                        prepared[target.key] = (target, attempts, invocations, snapshot, before, after, started)
                    except QualityError as error:
                        self._failure(target, error, "traffic")
                        outcomes[target.key] = UnitResult(
                            target.unit_id, exclusion_reasons=(ExclusionReason.INCOMPLETE_EXECUTION,),
                        )

        await asyncio.gather(*(lane(agent) for agent in self.catalog.agents))

        async def assess(values):
            target, attempts, invocations, snapshot, before, after, started = values
            async with self.assessment_limit:
                try:
                    result = await assess_daily(
                        target, attempts, invocations, snapshot, self.sol,
                        before_cards=before, after_cards=after,
                        engine_started_at=started, visible_snapshot=snapshot,
                    )
                    self.run.save_artifact(f"targets/{target.key}/assessment", result.to_private_dict())
                    outcomes[target.key] = result.unit_result
                    if not result.unit_result.exclusion_reasons:
                        self.run.save_completed(
                            f"targets/{target.key}/unit-result", asdict(result.unit_result),
                        )
                except QualityError as error:
                    self._failure(target, error, "assessment")
                    outcomes[target.key] = UnitResult(
                        target.unit_id, exclusion_reasons=(ExclusionReason.INCOMPLETE_ASSESSMENT,),
                    )

        await asyncio.gather(*(assess(value) for value in prepared.values()))
        result = aggregate_results(
            planned_units(targets), tuple(outcomes.values()),
            integrity_failure=self.integrity_failure,
        )
        self.run.save_artifact("quality-result", result.to_dict())
        return result
