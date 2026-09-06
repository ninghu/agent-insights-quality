"""Resumable qualification, with no live dependencies at import time."""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field as dataclass_field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .catalogs import Catalog
from .contracts import Attempt, CloudPort, Deployment, Invocation, SolPort, Step, Target
from .errors import QualityError
from .events import RunLogger
from .performance import ObservedCloud, ObservedSol, RunMetrics, binding, limited, measure, observe, scope
from .registry import DeploymentRegistry
from .results import (
    CardVerdict, Contribution, CoreVerdict, DiagnosticVerdict, ExclusionReason,
    PlannedUnit, QualityResult, UnitId, UnitResult, aggregate_results,
)
from .selection import (
    LastTest, Selection, SourceChanges, deployment_inputs, git_source_changes, select_staging,
)
from .settings import AssessmentSettings, RuntimeSettings
from .state import RecordStore, RuntimeStore, StateError
from .staging_policy import STAGING_POLICY, StagingPolicyMigration
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


def daily_traffic_intent(
    records: RecordStore, *, test_run: bool, rerun: int, revision: str,
    fresh_traffic: bool | None = None, reuse_run_id: str | None = None,
) -> dict:
    """Freeze new-run intent before provider construction, including interrupted startup."""
    if fresh_traffic is not None and type(fresh_traffic) is not bool or (
        fresh_traffic and (not test_run or type(rerun) is not int or rerun < 1)
    ):
        raise QualityError("fresh_traffic_requires_test_rerun")
    existing = records.read_completed("run", missing_ok=True)
    frozen = records.read_completed("traffic-intent", missing_ok=True)
    if frozen is None and existing is not None:
        frozen = {
            "fresh_traffic": existing.get("fresh_traffic", False),
            "reuse_run_id": existing.get("reuse_run_id"),
            "source_revision": existing["source_revision"],
        }
    if frozen is not None:
        if set(frozen) != {"fresh_traffic", "reuse_run_id", "source_revision"} or (
            type(frozen["fresh_traffic"]) is not bool
            or not isinstance(frozen["source_revision"], str)
            or frozen["reuse_run_id"] is not None and not isinstance(frozen["reuse_run_id"], str)
            or frozen["fresh_traffic"] and (not test_run or rerun < 1 or frozen["reuse_run_id"] is not None)
        ):
            raise StateError("traffic_intent_invalid")
        if fresh_traffic is not None and fresh_traffic != frozen["fresh_traffic"] or (
            reuse_run_id is not None and reuse_run_id != frozen["reuse_run_id"]
        ):
            raise QualityError("run_traffic_intent_mismatch")
        if frozen["fresh_traffic"] and frozen["source_revision"] != revision:
            raise QualityError("fresh_resume_source_changed")
    else:
        if fresh_traffic and reuse_run_id is not None:
            raise QualityError("run_traffic_intent_mismatch")
        frozen = {
            "fresh_traffic": bool(fresh_traffic), "reuse_run_id": reuse_run_id,
            "source_revision": revision,
        }
    if existing is not None and (
        existing.get("fresh_traffic", False) != frozen["fresh_traffic"]
        or existing.get("reuse_run_id") != frozen["reuse_run_id"]
    ):
        raise StateError("traffic_intent_invalid")
    records.save_completed("traffic-intent", frozen)
    return frozen


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


def _staging_history(
    catalog: Catalog, store: RuntimeStore, targets: tuple[Target, ...] | None = None,
) -> dict[str, dict]:
    """Read current target bindings, not just the last completed test index."""
    targets = catalog.targets if targets is None else targets
    control = store.outbox("staging")
    active = []
    for mode in ("full", "incremental"):
        value = control.read(mode, missing_ok=True)
        if value is not None:
            if not isinstance(value.get("run_id"), str):
                raise StateError("staging_resume_invalid")
            active.append(value["run_id"])
    metadata, history, times = {}, {}, {}

    def consider(target: Target, run_id: str, *, required: bool, work_key: str | None = None) -> None:
        records = store.run(run_id)
        binding = records.read(f"targets/{target.key}/source", missing_ok=not required)
        if binding is None:
            return
        if run_id not in metadata:
            metadata[run_id] = (
                records.read_completed("run"), records.read_completed("environment"),
            )
        run, environment = metadata[run_id]
        if run.get("kind") != "staging" or environment.get("profile") != "staging" or target.key not in run.get("targets", ()):
            raise StateError("staging_binding_scope_invalid")
        if not {
            "source_revision", "traffic_source_revision", "traffic_run_id", "work_key", "tested_at",
        } <= binding.keys() or not isinstance(binding["work_key"], str) or not binding["work_key"].startswith(f"targets/{target.key}/work-"):
            raise StateError("target_checkpoint_invalid")
        if work_key is not None and binding["work_key"] != work_key:
            raise StateError("staging_target_reference_mismatch")
        traffic_environment = store.run(binding["traffic_run_id"]).read_completed("environment")
        if traffic_environment != environment:
            raise StateError("staging_binding_environment_mismatch")
        stamp = _datetime(run["started_at"])
        previous = history.get(target.key)
        if previous:
            if stamp < times[target.key]:
                return
            if stamp == times[target.key] and binding["work_key"] != previous["work_key"]:
                raise StateError("staging_binding_order_ambiguous")
        history[target.key] = {
            **binding, "binding_run_id": run_id,
            "status": binding["status"] if binding.get("result") else "INCOMPLETE",
        }
        times[target.key] = stamp

    for target in targets:
        indexed = store.staging_index.read(target.key, missing_ok=True)
        if indexed is not None:
            owner = indexed.get("binding_run_id") or indexed.get("failure_run_id") or (
                indexed.get("assessment", {}).get("run_id")
            ) or indexed.get("traffic_run_id")
            if not isinstance(owner, str):
                raise StateError("staging_history_invalid")
            consider(target, owner, required=True)
        for run_id in active:
            consider(target, run_id, required=False)
        latest = control.read(f"targets/{target.key}", missing_ok=True)
        if latest is not None:
            if set(latest) != {"run_id", "work_key"} or not all(
                isinstance(latest[field], str) for field in ("run_id", "work_key")
            ):
                raise StateError("staging_target_reference_invalid")
            consider(target, latest["run_id"], required=True, work_key=latest["work_key"])
    return history


def reconcile_staging_work(catalog: Catalog, store: RuntimeStore) -> None:
    """Retain discovered unfinished work before replacing an active run pointer."""
    for key, record in _staging_history(catalog, store).items():
        store.outbox("staging").save_progress(f"targets/{key}", {
            "run_id": record["binding_run_id"], "work_key": record["work_key"],
        })


def choose_staging(
    catalog: Catalog, store: RuntimeStore, *, full: bool = False,
    changes_since: Callable[[str], SourceChanges] | None = None,
) -> tuple[Selection, ...]:
    history = _staging_history(catalog, store)
    try:
        previous = {
            key: LastTest(record["source_revision"], record["status"], record["tested_at"])
            for key, record in history.items()
        }
    except (KeyError, TypeError, ValueError) as error:
        raise StateError("staging_history_invalid") from error
    compare = changes_since or (lambda revision: git_source_changes(catalog.root, revision))
    changes = {} if full else {
        revision: compare(revision) for revision in {item.source_revision for item in previous.values()}
    }
    selections = select_staging(
        catalog, last_tests=previous, changes_by_revision=changes, full=full,
        evaluation_paths=_EVALUATION,
    )
    result = []
    collectors = {
        catalog.root / "src" / "agent_insights_quality" / "telemetry.py",
        catalog.root / "src" / "agent_insights_quality" / "providers" / "telemetry.py",
    }
    for selection in selections:
        record = history.get(selection.target.key)
        if not full and record and record.get("assessment") and record.get("evidence_key") and (
            _policy_only_change(catalog, changes[record["source_revision"]])
        ):
            selection = replace(
                selection, action="reassess", reasons=(*selection.reasons, "staging_policy_changed"),
            )
        elif selection.reasons == ("incomplete",) and not any(
            (catalog.root / path).resolve() in collectors
            for path in changes[record["source_revision"]].paths
        ) and _retained_assessment_evidence(store, selection.target, record):
            selection = replace(selection, action="reassess", reasons=(*selection.reasons, "assessment_retry"))
        result.append(selection)
    return tuple(result)


def _policy_only_change(catalog: Catalog, changes: SourceChanges) -> bool:
    return {(catalog.root / path).resolve() for path in changes.paths} == {
        catalog.root / "src" / "agent_insights_quality" / "staging_policy.py",
    }


def _retained_assessment_evidence(store: RuntimeStore, target: Target, record: dict) -> bool:
    """An assessment transport/schema failure must not replace its input packet."""
    if not record.get("reason") or not record.get("evidence_key"):
        return False
    records = store.run(record["traffic_run_id"])
    failure_records = store.run(record.get("failure_run_id", record["traffic_run_id"]))
    failure = failure_records.read(f"targets/{target.key}/failure", missing_ok=True)
    if not failure or failure.get("stage") != "assessment" or failure.get("code") != record["reason"]:
        return False
    key = record["work_key"]
    if not records.read_completed(key + "/traffic-done", missing_ok=True):
        return False
    try:
        snapshot = Snapshot.from_private_dict(records.read_artifact(record["evidence_key"]))
    except QualityError as error:
        raise StateError("evidence_checkpoint_invalid") from error
    if not snapshot.query_complete:
        return False
    plan = records.read_completed(key + "/plan")
    ready = 0
    for attempt in plan["attempts"]:
        attributable_probe = False
        for step in attempt["steps"]:
            invocation = Invocation(**records.read_completed(
                key + f"/traffic/attempt-{attempt['index']:02d}/{step['step_id']}",
            ))
            if step["phase"] == "probe" and invocation.response is not None:
                attributable_probe |= invocation.response_id in snapshot.attributable_responses
        ready += attributable_probe
    return ready >= RuntimeSettings().readiness_attempts


@dataclass
class _Work:
    records: RecordStore
    binding: dict[str, Any]

    @property
    def key(self) -> str:
        return self.binding["work_key"]


@dataclass
class _PreparedAttempt:
    attempt: Attempt
    prepared: asyncio.Event = dataclass_field(default_factory=asyncio.Event)
    execute: asyncio.Event = dataclass_field(default_factory=asyncio.Event)
    finished: asyncio.Event = dataclass_field(default_factory=asyncio.Event)
    session_ready: bool = False


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
        fresh_traffic: bool | None = None,
        now: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        attempts: Callable[[Target], tuple[Attempt, ...]] = load_attempts,
        deployment_source: Callable[[Target], str] | None = None,
        changes_since: Callable[[str], SourceChanges] | None = None,
        event_outbox: Callable[[dict[str, Any]], None] | None = None,
        staging_policy_migration: StagingPolicyMigration | None = None,
        metrics: RunMetrics | None = None,
        assessment_settings: AssessmentSettings | None = None,
    ) -> None:
        if runtime.environment != cloud.environment.profile:
            raise QualityError("runner_environment_mismatch")
        if type(test_run) is not bool or type(rerun) is not int or (
            rerun < 1 if test_run else rerun != 0
        ) or test_run and runtime.environment != "daily":
            raise QualityError("runner_test_identity_invalid")
        if reuse_run_id and (not test_run or reuse_run_id == run_id):
            raise QualityError("runner_reuse_invalid")
        if staging_policy_migration is not None and (
            runtime.environment != "staging"
            or not isinstance(staging_policy_migration, StagingPolicyMigration)
            or staging_policy_migration.destination_policy != STAGING_POLICY
        ):
            raise QualityError("staging_policy_migration_invalid")
        self.staging_policy_migration = staging_policy_migration
        self.catalog, self.runtime, self.run_id = catalog, runtime, run_id
        self.run = runtime.run(run_id)
        frozen_assessor = self.run.read_completed("assessment-settings", missing_ok=True)
        if assessment_settings is not None and not isinstance(assessment_settings, AssessmentSettings):
            raise QualityError("assessment_settings_invalid")
        self.assessment_settings = assessment_settings or (
            AssessmentSettings.from_dict(frozen_assessor) if frozen_assessor is not None else AssessmentSettings()
        )
        if frozen_assessor is not None and AssessmentSettings.from_dict(frozen_assessor) != self.assessment_settings:
            raise QualityError("run_assessor_mismatch")
        if getattr(sol, "deployment", self.assessment_settings.deployment_name) != self.assessment_settings.deployment_name:
            raise QualityError("assessor_deployment_mismatch")
        self.metrics = metrics
        self.cloud = ObservedCloud(cloud, metrics) if metrics is not None else cloud
        self.sol = ObservedSol(sol, metrics) if metrics is not None else sol
        self.registry = registry
        self.settings = settings or RuntimeSettings()
        if self.metrics:
            self.metrics.configuration = self.settings.to_dict()
        self.test_run, self.rerun, self.reuse_run_id = test_run, rerun, reuse_run_id
        self.fresh_traffic = fresh_traffic
        self.revision = revision or source_revision(catalog)
        self.now, self.monotonic, self.sleep = now, monotonic, sleep
        self.attempts = attempts
        self.deployment_source = deployment_source or (lambda target: deployment_revision(catalog, target))
        self.changes_since = changes_since or (
            lambda previous: git_source_changes(catalog.root, previous, self.revision)
        )
        self._source_comparisons: dict[str, SourceChanges] = {}
        self.logger = RunLogger(
            self.run.directory, allowed_units=[target.unit_id for target in catalog.targets],
            max_bytes=self.settings.log_max_bytes, backup_count=self.settings.log_backup_count,
            clock=now, monotonic=monotonic, test_run=test_run,
            outbox=event_outbox,
        )
        self.deploy_limit = asyncio.Semaphore(self.settings.deployment_workers)
        self.query_limit = asyncio.Semaphore(self.settings.query_workers)
        self.assessment_limit = asyncio.Semaphore(self.settings.assessment_workers)
        self.attempt_limit = asyncio.Semaphore(self.settings.daily_attempt_budget)
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
        if kind == "daily":
            intent = daily_traffic_intent(
                self.run, test_run=self.test_run, rerun=self.rerun, revision=self.revision,
                fresh_traffic=self.fresh_traffic, reuse_run_id=self.reuse_run_id,
            )
            self.fresh_traffic, self.reuse_run_id = intent["fresh_traffic"], intent["reuse_run_id"]
        elif self.fresh_traffic is not None:
            raise QualityError("fresh_traffic_requires_test_rerun")
        migration = self.staging_policy_migration.to_dict() if self.staging_policy_migration else None
        saved_migration = self.run.read_completed("staging-policy-migration", missing_ok=True)
        if saved_migration is not None and saved_migration != migration:
            raise QualityError("staging_policy_migration_mismatch")
        if migration is not None:
            self._save(self.run, "completed", "staging-policy-migration", migration)
        expected = {
            "kind": kind, "report_date": report_date.isoformat(),
            "test_run": self.test_run, "rerun": self.rerun,
            "targets": [target.key for target in targets],
        }
        existing = self.run.read_completed("run", missing_ok=True)
        if existing is not None:
            if self.run.read_completed("assessment-settings", missing_ok=True) is None:
                raise StateError("assessment_settings_missing")
            if any(existing.get(key) != value for key, value in expected.items()):
                raise QualityError("run_identity_mismatch")
            if not self.test_run and kind == "daily" and existing["source_revision"] != self.revision:
                raise QualityError("official_resume_source_changed")
            self.reuse_run_id = existing.get("reuse_run_id")
        if kind == "daily":
            policy = self.run.read_completed("daily-session-lookahead", missing_ok=True)
            if policy is None:
                policy = {"travel_sessions_ahead": (
                    0 if existing is not None else self.settings.daily_travel_session_lookahead
                )}
            if set(policy) != {"travel_sessions_ahead"} or (
                type(policy["travel_sessions_ahead"]) is not int
                or policy["travel_sessions_ahead"] not in (0, 1)
            ):
                raise StateError("session_lookahead_policy_invalid")
            self._save(self.run, "completed", "daily-session-lookahead", policy)
            self.settings = replace(
                self.settings, daily_travel_session_lookahead=policy["travel_sessions_ahead"],
            )
            if self.metrics:
                self.metrics.configuration = self.settings.to_dict()
        if self.reuse_run_id:
            previous = self.runtime.run(self.reuse_run_id).read_completed("run")
            if (
                previous.get("test_run") is not True
                or previous.get("report_date") != expected["report_date"]
                or previous.get("targets") != expected["targets"]
            ):
                raise QualityError("runner_reuse_invalid")
            self.lanes = self.runtime.run(previous.get("lane_run_id", self.reuse_run_id))
        self._save(self.run, "completed", "assessment-settings", self.assessment_settings.to_dict())
        self._save(self.run, "completed", "environment", asdict(self.cloud.environment))
        if existing is None:
            self._save(self.run, "completed", "run", {
                **expected, "source_revision": self.revision, "started_at": self.now().isoformat(),
                "reuse_run_id": self.reuse_run_id,
                **({"fresh_traffic": self.fresh_traffic} if kind == "daily" else {}),
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

    def _changes_since(self, revision: str) -> SourceChanges:
        if revision not in self._source_comparisons:
            self._source_comparisons[revision] = self.changes_since(revision)
        return self._source_comparisons[revision]

    def _binding(self, target: Target, selection: Selection | None = None) -> _Work:
        key = f"targets/{target.key}/source"
        old = self.run.read(key, missing_ok=True)
        own = old is not None
        if not own and any(
            any((self.run.directory / collection / "targets" / target.key).glob("work-*"))
            for collection in ("progress", "completed", "artifacts")
        ):
            raise StateError("target_checkpoint_missing")
        if self.runtime.environment == "staging":
            latest = _staging_history(self.catalog, self.runtime, (target,)).get(target.key)
            if latest and latest["binding_run_id"] != self.run_id:
                current = self.run.read_completed("run")
                newer = self.runtime.run(latest["binding_run_id"]).read_completed("run")
                if own or _datetime(current["started_at"]) < _datetime(newer["started_at"]):
                    raise StateError("staging_target_superseded")
            if old is None and selection is not None and "full" not in selection.reasons:
                old = latest
        if old is None and self.reuse_run_id:
            old = self.runtime.run(self.reuse_run_id).read(key, missing_ok=True)
        changed, evaluate, recollect, policy_only = False, False, False, False
        assessor_changed = False
        if old:
            if not {"source_revision", "traffic_source_revision", "traffic_run_id", "work_key"} <= old.keys():
                raise StateError("target_checkpoint_invalid")
            if not isinstance(old["work_key"], str) or not old["work_key"].startswith(f"targets/{target.key}/work-"):
                raise StateError("target_checkpoint_invalid")
            if old["source_revision"] != self.revision:
                changes = self._changes_since(old["source_revision"])
                policy_only = self.runtime.environment == "staging" and _policy_only_change(self.catalog, changes)
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
            approved_migration = (
                self.staging_policy_migration is not None
                and old["source_revision"] == self.staging_policy_migration.source_revision
                and selection is not None and selection.action == "reassess"
                and not recollect
            )
            policy_only |= approved_migration
            if self.runtime.environment == "daily" and old.get("assessment"):
                previous_assessor = self._configured_assessor(old["assessment"], old.get("configured_assessor"))
                assessor_changed = previous_assessor != self.assessment_settings.to_dict()
        if old and not changed:
            if self.runtime.run(old["traffic_run_id"]).read_completed("environment") != asdict(self.cloud.environment):
                raise StateError("retained_environment_mismatch")
            binding = dict(old)
            if evaluate:
                binding.pop("policy_reassessment", None)
                if policy_only and old.get("assessment") and old.get("evidence_key"):
                    binding["policy_reassessment"] = {
                        **old["assessment"], "assessment_source_revision": old["source_revision"],
                        "judgment_source_revision": old.get("judgment_source_revision", old["source_revision"]),
                        "policy_source_revision": self.revision,
                    }
                    if approved_migration:
                        binding["policy_reassessment"]["migration"] = self.staging_policy_migration.to_dict()
                binding.pop("result", None)
                binding.pop("assessment", None)
                binding.pop("policy_version", None)
                binding.pop("minimum_required", None)
                binding["refresh_evidence"] = recollect
            if assessor_changed:
                binding.pop("result", None)
                binding.pop("assessment", None)
                binding.pop("configured_assessor", None)
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
        if self.runtime.environment == "staging":
            binding["binding_run_id"] = self.run_id
        self._save(self.run, "progress", key, binding)
        if self.runtime.environment == "staging":
            self._save(self.runtime.outbox("staging"), "progress", f"targets/{target.key}", {
                "run_id": self.run_id, "work_key": binding["work_key"],
            })
        return _Work(self.runtime.run(binding["traffic_run_id"]), binding)

    def _configured_assessor(self, reference: dict, declared: dict | None = None) -> dict[str, str]:
        configured = self.runtime.run(reference["run_id"]).read_completed("assessment-settings")
        identity = AssessmentSettings.from_dict(configured).to_dict()
        if declared is not None and AssessmentSettings.from_dict(declared).to_dict() != identity:
            raise StateError("assessment_identity_conflict")
        return identity

    def _prior_result(self, work: _Work) -> dict | None:
        result = work.binding.get("result")
        if result is not None:
            reference = work.binding.get("assessment")
            if not isinstance(reference, dict):
                raise StateError("assessment_checkpoint_missing")
            artifact = self.runtime.run(reference["run_id"]).read_artifact(reference["artifact"])
            if any(artifact.get(key) != value for key, value in result.items()):
                raise StateError("assessment_checkpoint_conflict")
            configured = self._configured_assessor(reference, work.binding.get("configured_assessor"))
            if artifact.get("configured_assessor") is not None and artifact["configured_assessor"] != configured:
                raise StateError("assessment_identity_conflict")
            if self.runtime.environment == "daily" and configured != self.assessment_settings.to_dict():
                raise StateError("assessment_identity_conflict")
        return result

    def _update(self, target: Target, work: _Work, **fields: Any) -> None:
        self._assert_staging_owner(target, work)
        work.binding.update(fields)
        self._save(self.run, "progress", f"targets/{target.key}/source", work.binding)

    def _assert_staging_owner(self, target: Target, work: _Work) -> None:
        if self.runtime.environment == "staging" and self.runtime.outbox("staging").read(
            f"targets/{target.key}",
        ) != {"run_id": self.run_id, "work_key": work.key}:
            raise StateError("staging_target_superseded")

    def _index_staging(self, target: Target, work: _Work) -> None:
        self._assert_staging_owner(target, work)
        self._save(self.runtime.staging_index, "progress", target.key, work.binding)

    def _deadline(self, records: RecordStore, key: str, seconds: int) -> datetime:
        value = records.read(key, missing_ok=True)
        if value is None:
            value = {"until": (self.now() + timedelta(seconds=seconds)).isoformat()}
            self._save(records, "completed", key, value)
        return _datetime(value["until"])

    async def _wait(self, target: Target, stage: str, deadline: datetime, start: float) -> bool:
        budget = (
            self.settings.insights_poll_timeout_seconds
            if stage == "insights" else self.settings.hydration_seconds
            if stage == "evidence" and self.runtime.environment == "daily"
            else self.settings.poll_timeout_seconds
        )
        remaining = min(
            (deadline - self.now()).total_seconds(),
            budget - (self.monotonic() - start),
        )
        if remaining <= 0:
            return False
        self._event("heartbeat", target, stage=stage)
        with scope(self.metrics, "wait", "hydration" if stage == "evidence" else "poll", stage=stage):
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
        with scope(self.metrics, "wait", "retry_backoff", stage=stage):
            await self.sleep(min(
                self.settings.retry_backoff_seconds * 2**count,
                self.settings.retry_max_backoff_seconds,
            ))
        self._check()
        return True

    @measure("stage", "deployment")
    async def _deployment(self, target: Target, work: _Work) -> Deployment:
        key = work.key + "/deployment"
        if work.records.read_completed(work.key + "/traffic-done", missing_ok=True):
            if self.metrics:
                self.metrics.reuse("stage", "deployment")
            return Deployment(**work.records.read(key))
        self._event("started", target, stage="deployment")
        raw = work.records.read(key, missing_ok=True)
        existing = Deployment(**raw) if raw else self.registry.get(target.key)
        revision = self.deployment_source(target)
        deadline = self._deadline(work.records, key + "/deadline", self.settings.poll_timeout_seconds)
        start = self.monotonic()
        async with limited(self.metrics, self.deploy_limit, "deployment"):
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
            if self.metrics:
                self.metrics.reuse("port_call", "create_session", skipped=True)
            return None
        key = work.key + f"/traffic/attempt-{index:02d}/session"
        saved = work.records.read(key, missing_ok=True)
        if saved and saved["status"] == "ready":
            if self.metrics:
                self.metrics.reuse("port_call", "create_session")
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

    @staticmethod
    def _session_affinity(deployment: Deployment) -> dict:
        return {
            "target_key": deployment.target_key, "agent_name": deployment.agent_name,
            "provider_version": deployment.provider_version,
            "deployment_source_revision": deployment.source_revision,
        }

    @measure("attempt_phase", "session_preparation")
    async def _prepare_travel_session(
        self, target: Target, work: _Work, deployment: Deployment, attempt: Attempt,
    ) -> str:
        key = work.key + f"/traffic/attempt-{attempt.index:02d}"
        saved = work.records.read(key + "/session", missing_ok=True)
        affinity = work.records.read_completed(key + "/session-affinity", missing_ok=True)
        if saved is not None and affinity is None:
            raise StateError("session_preparation_affinity_missing")
        self._save(
            work.records, "completed", key + "/session-affinity", self._session_affinity(deployment),
        )
        for step in attempt.steps:
            receipt = work.records.read(key + "/" + step.step_id, missing_ok=True)
            if receipt is not None and receipt["status"] != "blocked":
                if saved is None or saved["status"] != "ready":
                    raise StateError("session_checkpoint_missing")
                if receipt["session_id"] != saved["session_id"]:
                    raise StateError("session_checkpoint_mismatch")
        session = await self._session(target, work, deployment, attempt.index)
        if saved is not None and saved["status"] == "ready" and self.metrics:
            self.metrics.reuse("attempt_phase", "session_preparation")
        return session

    @measure("turn", "invoke")
    async def _invoke(
        self, target: Target, work: _Work, deployment: Deployment, attempt: Attempt,
        step: Step, session: str | None, previous: str | None,
    ) -> Invocation:
        key = work.key + f"/traffic/attempt-{attempt.index:02d}/{step.step_id}"
        raw = work.records.read(key, missing_ok=True)
        if raw and raw["status"] != "blocked":
            if self.metrics:
                self.metrics.reuse("turn", "invoke", receipt_status=raw["status"])
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

    def _reused_traffic(self, target: Target, attempts: tuple[Attempt, ...], invocations: Mapping) -> None:
        if self.metrics:
            with binding(self.metrics, unit=target.key, lane=target.unit_id.agent):
                for attempt in attempts:
                    self.metrics.reuse("attempt", "traffic", attempt=attempt.index)
                    for step in attempt.steps:
                        value = invocations.get((attempt.index, step.step_id))
                        self.metrics.reuse(
                            "turn", "invoke", attempt=attempt.index, turn=step.step_id,
                            receipt_status=value.status if value else "missing",
                        )

    @measure("stage", "traffic")
    async def _traffic(
        self, target: Target, work: _Work, deployment: Deployment, attempts: tuple[Attempt, ...],
    ) -> dict:
        if work.records.read_completed(work.key + "/traffic-done", missing_ok=True):
            invocations = self._load_traffic(target, work, attempts)
            if len(invocations) != sum(len(item.steps) for item in attempts):
                raise StateError("traffic_checkpoint_missing")
            if self.metrics:
                self.metrics.reuse("stage", "traffic")
                self._reused_traffic(target, attempts, invocations)
            return invocations
        lookahead = (
            self.runtime.environment == "daily" and target.unit_id.agent == "travel-agent"
            and not target.is_prompt and self.settings.daily_travel_session_lookahead == 1
            and work.binding["traffic_run_id"] == self.run_id
        )
        if lookahead:
            for attempt in attempts:
                affinity = work.records.read_completed(
                    work.key + f"/traffic/attempt-{attempt.index:02d}/session-affinity", missing_ok=True,
                )
                if affinity is not None and affinity != self._session_affinity(deployment):
                    raise StateError("session_preparation_affinity_mismatch")
        self._event("started", target, stage="traffic")
        self._check()
        await self.cloud.activate(deployment)
        invocations = {}
        allow_ahead, stop_business = True, None
        async def one(attempt: Attempt, prepared: _PreparedAttempt | None = None) -> None:
            nonlocal allow_ahead, stop_business
            with binding(self.metrics, attempt=attempt.index), scope(self.metrics, "attempt", "traffic") as measured:
                previous, session, blocked = None, None, None
                try:
                    if prepared is not None:
                        if stop_business:
                            blocked = stop_business
                            if self.metrics:
                                self.metrics.reuse("attempt_phase", "session_preparation", skipped=True)
                        else:
                            session = await self._prepare_travel_session(target, work, deployment, attempt)
                        for step in attempt.steps:
                            saved = work.records.read(
                                work.key + f"/traffic/attempt-{attempt.index:02d}/{step.step_id}",
                                missing_ok=True,
                            )
                            if saved and (
                                saved["status"] in {"submitting", "unknown"}
                                or saved.get("error_code") == "invocation_response_pending"
                            ):
                                allow_ahead = False
                    else:
                        session = await self._session(target, work, deployment, attempt.index)
                except QualityError as error:
                    self._fatal(error)
                    self._failure(target, error, "traffic")
                    if error.code in _INTEGRITY:
                        raise
                    blocked = error.code
                    if prepared is not None:
                        allow_ahead = False
                if prepared is not None:
                    prepared.session_ready = blocked is None
                    prepared.prepared.set()
                    with scope(self.metrics, "wait", "prepared_session"):
                        await prepared.execute.wait()
                    self._check()
                    blocked = stop_business or blocked
                with scope(
                    self.metrics if prepared is not None else None, "attempt_phase", "business_execution",
                ) as business:
                    for step in attempt.steps:
                        if blocked:
                            key = work.key + f"/traffic/attempt-{attempt.index:02d}/{step.step_id}"
                            saved = work.records.read(key, missing_ok=True)
                            receipt = Invocation(**saved) if saved else Invocation(
                                uuid.uuid4().hex, None, session, self.now().isoformat(),
                                self.now().isoformat(), "blocked", error_code=blocked,
                            )
                            self._save(work.records, "progress", key, asdict(receipt))
                            if self.metrics:
                                self.metrics.reuse(
                                    "turn", "invoke", skipped=True, turn=step.step_id, receipt_status=receipt.status,
                                )
                                self.metrics.increment_scope("attempt", "traffic", "skipped_turns")
                        else:
                            receipt = await self._invoke(target, work, deployment, attempt, step, session, previous)
                        invocations[(attempt.index, step.step_id)] = receipt
                        self._event("checkpoint", target, stage="traffic", attempt=attempt.index)
                        if receipt.status != "completed":
                            measured["status"] = receipt.status
                            if prepared is not None:
                                allow_ahead = False
                        if receipt.response is None or target.is_prompt and not receipt.response_id:
                            blocked = "conversation_continuation_unavailable"
                        if receipt.error_code == "invocation_response_pending":
                            blocked = "invocation_outcome_unresolved"
                        if prepared is not None and (
                            receipt.status == "unknown" or receipt.error_code == "invocation_response_pending"
                        ):
                            stop_business = "invocation_outcome_unresolved"
                            blocked = stop_business
                        previous = receipt.response_id
                    if self.metrics and not measured.get("fresh_turns"):
                        measured["receipt_status"] = measured["status"]
                        measured["status"] = "reused" if measured.get("reused_turns") else "skipped"
                    if prepared is not None and self.metrics:
                        business["status"] = measured["status"]
        if lookahead:
            jobs = asyncio.Queue()
            pending = iter(attempts)
            def enqueue() -> _PreparedAttempt | None:
                attempt = next(pending, None)
                if attempt is None:
                    return None
                job = _PreparedAttempt(attempt)
                jobs.put_nowait(job)
                return job
            async def prepare_worker() -> None:
                while (job := await jobs.get()) is not None:
                    with binding(self.metrics, attempt=job.attempt.index):
                        async with limited(self.metrics, self.attempt_limit, "daily_attempt"):
                            self._check()
                            await one(job.attempt, job)
                    job.finished.set()
            async def ordered_business() -> None:
                current = enqueue()
                while current is not None:
                    await current.prepared.wait()
                    current.execute.set()
                    following = enqueue() if allow_ahead and current.session_ready and not stop_business else None
                    await current.finished.wait()
                    current = following or enqueue()
                jobs.put_nowait(None)
                jobs.put_nowait(None)
            # At most current + next own whole-attempt permits. With budget one,
            # the next worker waits without blocking current business/release.
            await self._gather([ordered_business(), prepare_worker(), prepare_worker()])
        elif self.runtime.environment == "daily":
            # Travel's graph-wide BookingLedger mutates synthetic reservation state.
            # Keep its attempts serial even though native conversations are isolated.
            workers = 1 if target.unit_id.agent == "travel-agent" else self.settings.daily_attempt_workers
            pending = iter(attempts)
            async def worker() -> None:
                for attempt in pending:
                    self._check()
                    # Fixed target workers acquire the global slot in the same order.
                    with binding(self.metrics, attempt=attempt.index):
                        async with limited(self.metrics, self.attempt_limit, "daily_attempt"):
                            self._check()
                            await one(attempt)
            await self._gather(worker() for _ in range(workers))
        else:
            for attempt in attempts:
                await one(attempt)
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

    @staticmethod
    def _probe_roots_complete(attempts: tuple[Attempt, ...], invocations: Mapping, snapshot: Snapshot) -> bool:
        attributable = snapshot.attributable_responses
        return all(
            receipt.response_id in attributable
            for attempt in attempts for step in attempt.steps
            if step.phase == "probe"
            and (receipt := invocations.get((attempt.index, step.step_id))) is not None
            and receipt.response is not None
        )

    @measure("stage", "evidence")
    async def _evidence(
        self, target: Target, work: _Work, deployment: Deployment,
        attempts: tuple[Attempt, ...], invocations: Mapping,
    ) -> tuple[Snapshot, str]:
        key = work.key + "/evidence"
        self._event("started", target, stage="evidence")
        deadline = self._deadline(work.records, key + "/deadline", self.settings.hydration_seconds)
        if self.runtime.environment == "daily" and work.records.read_completed(
            key + "/grace-deadline", missing_ok=True,
        ) is None:
            retained = work.records.read(key, missing_ok=True)
            if retained is not None:
                previous = self._snapshot(work, retained["artifact"])
                if self._ready_attempts(attempts, invocations, previous) >= self.settings.readiness_attempts:
                    # Recover a crash between the visible snapshot and its grace
                    # checkpoint without granting another observation interval.
                    self._save(work.records, "completed", key + "/grace-deadline", {
                        "until": (_datetime(previous.observed_at) + timedelta(
                            seconds=self.settings.daily_evidence_grace_seconds,
                        )).isoformat(),
                    })
        start, extra_poll = self.monotonic(), False
        while True:
            self._check()
            wait_until = deadline
            grace = None
            if self.runtime.environment == "daily":
                grace = work.records.read_completed(key + "/grace-deadline", missing_ok=True)
                if grace is not None:
                    wait_until = min(deadline, _datetime(grace["until"]))
            try:
                async with limited(self.metrics, self.query_limit, "evidence_query"):
                    snapshot = await collect_snapshot(
                        self.cloud, deployment, invocations.values(), observed_at=self.now(),
                    )
                    if self.runtime.environment == "daily":
                        snapshot = replace(snapshot, observed_at=self.now().isoformat())
            except QualityError as error:
                self._fatal(error)
                if error.retryable and await self._wait(target, "evidence", wait_until, start):
                    continue
                raise
            artifact = work.key + "/snapshots/" + uuid.uuid4().hex
            self._save(work.records, "artifact", artifact, snapshot.to_private_dict())
            self._save(work.records, "progress", key, {"artifact": artifact})
            ready = self._ready_attempts(attempts, invocations, snapshot)
            if self.metrics:
                self.metrics.record({
                    "kind": "readiness", "name": "evidence", "unit": target.key, "status": "observed",
                    "attributable_attempts": ready, "query_complete": snapshot.query_complete,
                    "elapsed_seconds": None,
                })
            if self.runtime.environment == "daily":
                if ready >= self.settings.readiness_attempts:
                    if snapshot.query_complete and self._probe_roots_complete(attempts, invocations, snapshot):
                        return snapshot, artifact
                    grace_end = self._deadline(
                        work.records, key + "/grace-deadline", self.settings.daily_evidence_grace_seconds,
                    )
                    wait_until = min(deadline, grace_end)
            elif extra_poll and snapshot.query_complete:
                return snapshot, artifact
            if not await self._wait(target, "evidence", wait_until, start):
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

    @measure("stage", "insights")
    async def _insights(
        self, target: Target, work: _Work, monitor: str, invocations: Mapping,
        evidence_key: str, prior_end: str | None,
    ) -> dict:
        base = work.key + "/insights"
        completed = work.records.read_completed(base, missing_ok=True)
        if completed:
            if self.metrics:
                self.metrics.reuse("stage", "insights")
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
        deadline = self._deadline(work.records, base + "/deadline", self.settings.insights_poll_timeout_seconds)
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
        configured = self._configured_assessor(
            {"run_id": self.run_id}, result.get("configured_assessor"),
        )
        if self.runtime.environment == "staging":
            policy = {key: result[key] for key in ("policy_version", "minimum_required") if key in result}
            fields = {
                "result": {
                    **{key: result[key] for key in ("status", "passing_attempts", "reasons")}, **policy,
                },
                "status": result["status"], **policy,
                "judgment_source_revision": work.binding.get("policy_reassessment", {}).get(
                    "judgment_source_revision", self.revision,
                ),
            }
        else:
            restore_unit(result["unit_result"])
            fields = {"result": {key: result[key] for key in ("unit_result", "reasons")}}
        work.binding.pop("reason", None)
        work.binding.pop("failure_run_id", None)
        self._update(
            target, work, **fields, assessment={"run_id": self.run_id, "artifact": reference["artifact"]},
            assessed_at=self.now().isoformat(), configured_assessor=configured,
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

    @measure("stage", "assessment")
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
        reference = {
            "source_revision": self.revision, "work_key": work.key, "artifact": artifact,
            "configured_assessor": self.assessment_settings.to_dict(),
        }
        self._save(self.run, "progress", pending_key, {**reference, "status": "pending"})
        if saved is None:
            self._event("started", target, stage="assessment")
            async with limited(self.metrics, self.assessment_limit, "assessment"):
                self._check()
                result = await operation()
                saved = result.to_private_dict()
                saved["configured_assessor"] = self.assessment_settings.to_dict()
            self._save(self.run, "artifact", artifact, saved)
        elif self.metrics:
            self.metrics.reuse("stage", "assessment")
        self._save(self.run, "progress", pending_key, {**reference, "status": "saved"})
        self._apply_assessment(target, work, reference, saved)
        return saved

    @measure("run", "staging")
    async def run_staging(self, selections: tuple[Selection, ...]) -> dict[str, Any]:
        from .assessment import assess_staging, reassess_staging_policy

        if not self._initialized or self.runtime.environment != "staging":
            raise QualityError("runner_not_initialized")
        semaphore = asyncio.Semaphore(self.settings.staging_workers)
        @observe(self.metrics, "unit", "staging", lambda selection: {
            "unit": selection.target.key, "lane": selection.target.unit_id.agent,
        })
        async def one(selection: Selection) -> dict:
            target = selection.target
            async with limited(self.metrics, semaphore, "staging_worker"):
                work = None
                stage = "deployment"
                try:
                    work = self._binding(target, selection)
                    attempts = self._plan(target, work)
                    recovered = self._recover_assessment(target, work)
                    prior = self._prior_result(work)
                    policy_reassessment = work.binding.get("policy_reassessment")
                    if recovered or prior and (
                        prior["status"] in {"PASS", "FAIL"}
                        or policy_reassessment and prior.get("policy_version") == STAGING_POLICY.version
                    ):
                        invocations = self._load_traffic(target, work, attempts)
                        self._reused_traffic(target, attempts, invocations)
                        if self.metrics:
                            self.metrics.reuse("unit", "staging")
                        self._snapshot(work, work.binding["evidence_key"])
                        self._index_staging(target, work)
                        return {"unit": target.key, **work.binding}
                    if selection.action == "reassess" or policy_reassessment:
                        stage = "evidence"
                        invocations = self._load_traffic(target, work, attempts)
                        self._reused_traffic(target, attempts, invocations)
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
                    async def assess():
                        if policy_reassessment:
                            previous = self.runtime.run(policy_reassessment["run_id"]).read_artifact(
                                policy_reassessment["artifact"],
                            )
                            result = reassess_staging_policy(target, attempts, invocations, snapshot, previous)
                            result.private_detail["policy_reassessment"].update(policy_reassessment)
                            return result
                        return await assess_staging(target, attempts, invocations, snapshot, self.sol)
                    await self._assess(target, work, assess)
                except QualityError as error:
                    self._failure(target, error, stage)
                    if work is None:
                        raise
                    self._update(
                        target, work, status="INCOMPLETE", reason=error.code, failure_run_id=self.run_id,
                        policy_version=STAGING_POLICY.version, minimum_required=STAGING_POLICY.minimum_required,
                    )
                self._index_staging(target, work)
                return {"unit": target.key, **work.binding}
        records = await self._gather(one(item) for item in selections)
        summary = {
            "profile": "staging", "selected": len(selections), "results": records,
            "integrity_failure": self.integrity_failure, "staging_policy": STAGING_POLICY.to_dict(),
        }
        self._save(self.run, "progress", "staging-result", summary)
        self._event("completed")
        return summary

    @measure("run", "daily")
    async def run_daily(self, targets: tuple[Target, ...]) -> QualityResult:
        from .assessment import assess_daily

        if not self._initialized or self.runtime.environment != "daily" or targets != self.targets:
            raise QualityError("runner_not_initialized")
        agents = tuple(dict.fromkeys(target.unit_id.agent for target in targets))
        if any(not next(target for target in targets if target.unit_id.agent == agent).is_baseline for agent in agents):
            raise QualityError("daily_baseline_must_be_first")
        outcomes = {}
        assessments = asyncio.Queue()
        lane_limit = asyncio.Semaphore(self.settings.daily_lanes)
        @observe(self.metrics, "lane", "daily", lambda agent: {"lane": agent})
        async def lane(agent: str) -> None:
            async with limited(self.metrics, lane_limit, "daily_lane"):
                blocked = False
                for target in (item for item in targets if item.unit_id.agent == agent):
                    with binding(self.metrics, unit=target.key), scope(self.metrics, "unit", "daily_lane") as unit_metric:
                        work = None
                        prior = None
                        stage = "deployment"
                        try:
                            work = self._binding(target)
                            attempts = self._plan(target, work)
                            recovered = self._recover_assessment(target, work)
                            prior = self._prior_result(work)
                            if recovered or prior and not prior["unit_result"]["exclusion_reasons"]:
                                invocations = self._load_traffic(target, work, attempts)
                                self._reused_traffic(target, attempts, invocations)
                                if self.metrics:
                                    self.metrics.reuse("unit", "daily_lane")
                                work.records.read_completed(work.key + "/insights")
                                self._snapshot(work, work.binding["evidence_key"])
                                outcomes[target.key] = restore_unit(prior["unit_result"])
                                continue
                            insight = work.records.read_completed(work.key + "/insights", missing_ok=True)
                            if insight:
                                invocations = self._load_traffic(target, work, attempts)
                                self._reused_traffic(target, attempts, invocations)
                                if self.metrics:
                                    self.metrics.reuse("stage", "insights")
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
                                    if self.metrics:
                                        self.metrics.reuse("stage", "evidence")
                            else:
                                if blocked:
                                    if self.metrics:
                                        self.metrics.reuse("unit", "daily_lane", skipped=True)
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
                                    if self.metrics:
                                        self.metrics.reuse("stage", "evidence")
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
                            # Read the immutable completion, never retain provider-owned
                            # mutable card dictionaries across the next activation.
                            insight = work.records.read_completed(work.key + "/insights")
                            visible, window = self._engine_visible(
                                attempts, invocations, self._snapshot(work, insight["visible_snapshot"]), insight,
                            )
                            assessments.put_nowait((
                                target, work, attempts, invocations, snapshot, visible, insight, window,
                            ))
                        except QualityError as error:
                            unit_metric.update(status="failed", error_code=error.code)
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
        async def assess(values) -> None:
            target, work, attempts, invocations, snapshot, visible, insight, window = values
            try:
                result = await self._assess(target, work, lambda: assess_daily(
                    target, attempts, invocations, snapshot, self.sol,
                    before_cards=tuple(insight["before"]), after_cards=tuple(insight["after"]),
                    engine_started_at=insight["started_at"], visible_snapshot=visible, engine_window=window,
                    max_payload_bytes=self.settings.daily_assessment_max_payload_bytes,
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
        async def assessment_worker() -> None:
            while (values := await assessments.get()) is not None:
                target = values[0]
                with binding(self.metrics, unit=target.key, lane=target.unit_id.agent):
                    self._check()
                    await assess(values)

        async def produce() -> None:
            await self._gather(lane(agent) for agent in agents)
            for _ in range(self.settings.assessment_workers):
                assessments.put_nowait(None)

        # Workers belong to the same cancellation tree as lanes. Any fatal
        # checkpoint failure cancels and drains both before ownership can unwind.
        await self._gather([
            produce(), *(assessment_worker() for _ in range(self.settings.assessment_workers)),
        ])
        self.integrity_failure |= self.run.read_completed("integrity-failure", missing_ok=True) is not None
        with scope(self.metrics, "stage", "report"):
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
