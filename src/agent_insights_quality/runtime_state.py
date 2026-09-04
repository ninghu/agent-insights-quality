from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator

from agent_insights_quality.models import (
    AgentResult,
    InsightEvidence,
    InsightRunCheckpoint,
    InvocationEvidence,
    RequestCompletionEvidence,
    SKIPPED_VERSION_STATUSES,
    SemanticAssertionEvidence,
    TraceAssertionEvidence,
    VersionResult,
)
from agent_insights_quality.daily_lifecycle import DailyLock
from agent_insights_quality.util import (
    ContractError,
    atomic_json,
    content_hash,
    immutable_json,
    read_json,
    runtime_root,
)


class ActiveQualificationError(ContractError):
    """The selected profile is already executing qualification traffic."""


def _traffic_receipt_payload_from_mapping(value: dict) -> dict:
    return {
        key: (
            [
                {
                    nested_key: nested_value
                    for nested_key, nested_value in summary.items()
                    if nested_key
                    not in {
                        "trace_assertion_count",
                        "trace_assertions_passed",
                        "trace_assertion_results",
                        "error_code",
                    }
                }
                for summary in item
            ]
            if key == "request_summaries"
            else item
        )
        for key, item in value.items()
        if key not in {"trace_assertion_count", "trace_assertions_passed"}
    }


class TrafficLedger:
    def __init__(self, profile: str, root: Path | None = None) -> None:
        base = root or runtime_root()
        self._path = base / "traffic-ledger" / f"{profile}.json"
        self._lock = threading.Lock()

    def mark_started(
        self,
        agent_name: str,
        *,
        now: datetime,
        uncertain_seconds: int,
    ) -> None:
        with self._lock:
            value = self._read()
            existing = value["agents"].get(agent_name)
            existing_until = None
            if isinstance(existing, dict):
                try:
                    existing_until = datetime.fromisoformat(
                        str(existing["uncertain_until"])
                    ).astimezone(UTC)
                except (KeyError, ValueError) as error:
                    raise ContractError(
                        "Traffic ledger contains an invalid timestamp"
                    ) from error
            uncertain_until = now + timedelta(seconds=uncertain_seconds)
            if existing_until is not None:
                uncertain_until = max(uncertain_until, existing_until)
            value["agents"][agent_name] = {
                "activity_at": now.astimezone(UTC).isoformat(),
                "uncertain_until": uncertain_until.astimezone(UTC).isoformat(),
            }
            atomic_json(self._path, value)

    def mark_completed(self, agent_name: str, *, now: datetime) -> None:
        self._update(
            agent_name,
            activity_at=now,
            uncertain_until=now,
        )

    def clean_after(
        self,
        agent_name: str,
        *,
        lookback_seconds: int,
        margin_seconds: int,
    ) -> datetime | None:
        with self._lock:
            value = self._read()
        item = value["agents"].get(agent_name)
        if not isinstance(item, dict):
            return None
        try:
            activity = datetime.fromisoformat(str(item["activity_at"]))
            uncertain = datetime.fromisoformat(str(item["uncertain_until"]))
        except (KeyError, ValueError) as error:
            raise ContractError("Traffic ledger contains an invalid timestamp") from error
        if activity.tzinfo is None or uncertain.tzinfo is None:
            raise ContractError("Traffic ledger timestamps must include a timezone")
        return max(activity, uncertain).astimezone(UTC) + timedelta(
            seconds=lookback_seconds + margin_seconds
        )

    def _update(
        self,
        agent_name: str,
        *,
        activity_at: datetime,
        uncertain_until: datetime,
    ) -> None:
        with self._lock:
            value = self._read()
            value["agents"][agent_name] = {
                "activity_at": activity_at.astimezone(UTC).isoformat(),
                "uncertain_until": uncertain_until.astimezone(UTC).isoformat(),
            }
            atomic_json(self._path, value)

    def _read(self) -> dict:
        if not self._path.exists():
            return {"schema_version": "1.0.0", "agents": {}}
        value = read_json(self._path)
        if (
            value.get("schema_version") != "1.0.0"
            or not isinstance(value.get("agents"), dict)
        ):
            raise ContractError("Traffic ledger is invalid")
        return value


class VersionCheckpointStore:
    def __init__(
        self,
        root: Path,
        run_contract_digest: str,
        publication_fence: Callable[[], None] | None = None,
    ) -> None:
        self._root = root
        self._run_contract_digest = run_contract_digest
        self._publication_fence = publication_fence or (lambda: None)
        self._recovery_lock = threading.Lock()

    def has_progress(self, agent_name: str) -> bool:
        return any(self._root.glob(f"{agent_name}-*.json")) or (
            self._version_artifact_root() / agent_name
        ).is_dir()

    def has_version_progress(self, agent_name: str, logical_version: str) -> bool:
        return self._path(agent_name, logical_version).exists() or bool(
            self._artifact_candidates(
                agent_name,
                logical_version,
                "traffic-receipt",
            )
            or self._artifact_candidates(
                agent_name,
                logical_version,
                "result",
            )
        )

    def version_execution_claim(
        self,
        agent_name: str,
        logical_version: str,
    ) -> DailyLock:
        self._path(agent_name, logical_version)
        return DailyLock(
            self._version_artifact_root()
            / agent_name
            / logical_version
            / "execution.lock",
            wait_seconds=5,
        )

    def has_unresolved_insight_state(
        self,
        agent_name: str | None = None,
    ) -> bool:
        for path in self._root.glob("*.json"):
            value = read_json(path)
            try:
                self._validate_header(
                    value,
                    str(value["agent_name"]),
                    str(value["logical_version"]),
                    str(value["foundry_version"]),
                    str(value["content_digest"]),
                )
            except KeyError as error:
                raise ContractError("Version checkpoint identity is invalid") from error
            if agent_name is not None and value["agent_name"] != agent_name:
                continue
            if (
                value.get("insight_start_pending") is True
                or value.get("insight_drain_pending") is True
            ):
                return True
        return False

    def completed_agent_result(
        self,
        agent_name: str,
        logical_versions: list[str],
    ) -> AgentResult | None:
        results: list[VersionResult] = []
        for logical_version in logical_versions:
            identity = self._version_identity(agent_name, logical_version)
            if identity is None:
                return None
            foundry_version, digest = identity
            result = self.result(
                agent_name,
                logical_version,
                foundry_version,
                digest,
            )
            if result is None:
                return None
            results.append(result)
        if not results or results[0].logical_version != "v0":
            raise ContractError("Daily Agent checkpoint order is invalid")
        return AgentResult(
            agent_name=agent_name,
            baseline=results[0],
            issues=results[1:],
        )

    def public_agent_progress(
        self,
        agent_name: str,
        logical_versions: list[str],
    ) -> dict:
        versions = []
        for logical_version in logical_versions:
            path = self._path(agent_name, logical_version)
            if not path.is_file():
                result_artifacts = self._artifact_candidates(
                    agent_name,
                    logical_version,
                    "result",
                )
                traffic_artifacts = self._artifact_candidates(
                    agent_name,
                    logical_version,
                    "traffic-receipt",
                )
                if len(result_artifacts) > 1 or len(traffic_artifacts) > 1:
                    raise ContractError(
                        "Daily version has conflicting immutable artifact identities"
                    )
                if result_artifacts:
                    raw = read_json(result_artifacts[0])
                    record = self._read_version_artifact(
                        agent_name,
                        logical_version,
                        str(raw.get("foundry_version") or ""),
                        str(raw.get("content_digest") or ""),
                        "result",
                    )
                    if record is None:
                        raise ContractError(
                            "Immutable Daily version result is missing"
                        )
                    result = record.get("value")
                    if not isinstance(result, dict):
                        raise ContractError(
                            "Immutable Daily version result is invalid"
                        )
                    status = str(result.get("status") or "")
                    stage = (
                        status
                        if status in SKIPPED_VERSION_STATUSES
                        else "incomplete"
                        if status == "inconclusive"
                        else "complete"
                    )
                elif traffic_artifacts:
                    stage = "traffic_complete"
                else:
                    stage = "pending"
            else:
                value = read_json(path)
                try:
                    self._validate_header(
                        value,
                        agent_name,
                        logical_version,
                        str(value["foundry_version"]),
                        str(value["content_digest"]),
                    )
                except KeyError as error:
                    raise ContractError(
                        "Version checkpoint identity is invalid"
                    ) from error
                result = value.get("result")
                if isinstance(result, dict):
                    status = str(result.get("status") or "")
                    stage = (
                        status
                        if status in SKIPPED_VERSION_STATUSES
                        else "incomplete"
                        if status == "inconclusive"
                        else "complete"
                    )
                elif value.get("insight_run") is not None:
                    stage = "insight_running"
                elif value.get("insight_start_pending") is True:
                    stage = "insight_start_pending"
                elif value.get("trace_verified") is True:
                    stage = "trace_verified"
                elif value.get("operation_ids") is not None:
                    stage = "telemetry_correlated"
                elif value.get("invocation") is not None:
                    stage = "traffic_complete"
                else:
                    stage = "pending"
            versions.append(
                {
                    "logical_version": logical_version,
                    "stage": stage,
                }
            )
        terminal = {"complete", *SKIPPED_VERSION_STATUSES}
        current = next(
            (
                item["logical_version"]
                for item in versions
                if item["stage"] not in terminal
            ),
            None,
        )
        return {
            "current_version": current,
            "completed_version_count": sum(
                item["stage"] in terminal for item in versions
            ),
            "versions": versions,
        }

    def ensure_agent_monitor_reset(
        self,
        agent_name: str,
        monitor_reference: str,
        reset: Callable[[], None],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        root = self._root / "monitor-resets" / agent_name
        intent_path = root / "intent.json"
        outcome_path = root / "outcome.json"
        if outcome_path.is_file():
            outcome = read_json(outcome_path)
            intent = read_json(intent_path)
            if (
                outcome.get("intent_digest") != intent.get("intent_digest")
                or outcome.get("outcome_digest")
                != content_hash(
                    {
                        key: item
                        for key, item in outcome.items()
                        if key != "outcome_digest"
                    }
                )
            ):
                raise ContractError("Daily monitor-reset outcome is invalid")
            return
        if intent_path.is_file():
            raise ContractError(
                "Daily monitor reset has an unresolved provider outcome"
            )
        intent = {
            "schema_version": "1.0.0",
            "kind": "daily-agent-monitor-reset-intent",
            "run_contract_digest": self._run_contract_digest,
            "agent_name": agent_name,
            "monitor_reference": monitor_reference,
            "requested_at": now().astimezone(UTC).isoformat(),
            "intent_digest": "",
        }
        intent["intent_digest"] = content_hash(
            {key: item for key, item in intent.items() if key != "intent_digest"}
        )
        immutable_json(intent_path, intent)
        reset()
        outcome = {
            "schema_version": "1.0.0",
            "kind": "daily-agent-monitor-reset-outcome",
            "intent_digest": intent["intent_digest"],
            "completed_at": now().astimezone(UTC).isoformat(),
            "outcome_digest": "",
        }
        outcome["outcome_digest"] = content_hash(
            {key: item for key, item in outcome.items() if key != "outcome_digest"}
        )
        immutable_json(outcome_path, outcome)

    def claim_agent_recovery(self, agent_name: str, maximum: int) -> bool:
        if (
            isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or maximum < 0
        ):
            raise ContractError("Recovery checkpoint maximum is invalid")
        path = self._recovery_path(agent_name)
        with self._recovery_lock:
            value = (
                read_json(path)
                if path.exists()
                else {
                    "schema_version": "1.0.0",
                    "run_contract_digest": self._run_contract_digest,
                    "agent_name": agent_name,
                    "maximum": maximum,
                    "claimed": 0,
                }
            )
            claimed = value.get("claimed")
            if (
                value.get("schema_version") != "1.0.0"
                or value.get("run_contract_digest") != self._run_contract_digest
                or value.get("agent_name") != agent_name
                or value.get("maximum") != maximum
                or isinstance(claimed, bool)
                or not isinstance(claimed, int)
                or claimed < 0
                or claimed > maximum
            ):
                raise ContractError("Recovery checkpoint is invalid")
            if claimed == maximum:
                return False
            value["claimed"] = claimed + 1
            atomic_json(path, value)
            return True

    def agent_recovery_count(self, agent_name: str, maximum: int) -> int:
        path = self._recovery_path(agent_name)
        if not path.exists():
            return 0
        value = read_json(path)
        claimed = value.get("claimed")
        if (
            value.get("schema_version") != "1.0.0"
            or value.get("run_contract_digest") != self._run_contract_digest
            or value.get("agent_name") != agent_name
            or value.get("maximum") != maximum
            or isinstance(claimed, bool)
            or not isinstance(claimed, int)
            or claimed < 0
            or claimed > maximum
        ):
            raise ContractError("Recovery checkpoint is invalid")
        return claimed

    def archive_version_for_recovery(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> str:
        digest = self.preserve_version_attempt(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        self._path(agent_name, logical_version).unlink()
        return digest

    def preserve_version_attempt(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> str:
        path = self._path(agent_name, logical_version)
        if not path.is_file():
            raise ContractError("Recoverable version checkpoint is missing")
        value = read_json(path)
        self._validate_header(
            value,
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        if (
            value.get("insight_start_pending") is True
            or value.get("insight_drain_pending") is True
        ):
            raise ContractError(
                "Recoverable version has an ambiguous Agent Insights operation"
            )
        result = value.get("result")
        if (
            not isinstance(result, dict)
            or result.get("status") != "inconclusive"
            or result.get("error_code")
            not in {"baseline_evidence_incomplete", "baseline_evidence_failed"}
        ):
            raise ContractError("Version checkpoint is not recoverable")
        digest = content_hash(value)
        archive = (
            self._root
            / "recovery-history"
            / agent_name
            / logical_version
            / f"{digest.removeprefix('sha256:')}.json"
        )
        immutable_json(archive, value)
        return digest

    def invocation(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> InvocationEvidence | None:
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        payload = value.get("invocation")
        artifact = self._read_version_artifact(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
            "traffic-receipt",
        )
        if artifact is not None:
            artifact_payload = artifact["value"]
            if (
                isinstance(payload, dict)
                and _traffic_receipt_payload_from_mapping(payload)
                != artifact_payload
            ):
                raise ContractError(
                    "Version checkpoint conflicts with its immutable traffic receipt"
                )
            if not isinstance(payload, dict):
                payload = artifact["invocation"]
        if not isinstance(payload, dict):
            return None
        try:
            return InvocationEvidence(
                operation_ids=tuple(payload.get("operation_ids") or []),
                response_references=tuple(payload["response_references"]),
                started_at=str(payload["started_at"]),
                completed_at=str(payload["completed_at"]),
                request_count=int(payload["request_count"]),
                allow_window_correlation=bool(payload["allow_window_correlation"]),
                response_count=int(payload["response_count"]),
                usable_response_count=int(payload["usable_response_count"]),
                semantic_assertion_count=int(payload["semantic_assertion_count"]),
                semantic_assertions_passed=int(payload["semantic_assertions_passed"]),
                request_summaries=tuple(
                    RequestCompletionEvidence(
                        request_index=int(item["request_index"]),
                        response_count=int(item["response_count"]),
                        usable_response=bool(item["usable_response"]),
                        semantic_assertion_count=int(item["semantic_assertion_count"]),
                        semantic_assertions_passed=int(
                            item["semantic_assertions_passed"]
                        ),
                        assertion_results=tuple(
                            SemanticAssertionEvidence(
                                assertion=str(result["assertion"]),
                                passed=bool(result["passed"]),
                                evidence_sufficient=bool(
                                    result["evidence_sufficient"]
                                ),
                            )
                            for result in item["assertion_results"]
                        ),
                        activation_gate=bool(item["activation_gate"]),
                        direct_terminal_response_count=int(
                            item["direct_terminal_response_count"]
                        ),
                        function_call_count=int(item["function_call_count"]),
                        trace_assertion_count=int(
                            item["trace_assertion_count"]
                        ),
                        trace_assertions_passed=int(
                            item["trace_assertions_passed"]
                        ),
                        trace_assertion_results=tuple(
                            TraceAssertionEvidence(
                                assertion=str(result["assertion"]),
                                passed=bool(result["passed"]),
                                evidence_sufficient=bool(
                                    result["evidence_sufficient"]
                                ),
                            )
                            for result in item["trace_assertion_results"]
                        ),
                        error_code=item.get("error_code"),
                    )
                    for item in payload["request_summaries"]
                ),
                trace_assertion_count=int(payload["trace_assertion_count"]),
                trace_assertions_passed=int(
                    payload["trace_assertions_passed"]
                ),
                session_references=tuple(
                    payload.get("session_references") or []
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ContractError("Version checkpoint invocation is invalid") from error

    def save_invocation(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
        invocation: InvocationEvidence,
    ) -> None:
        payload = asdict(invocation)
        with self._version_lock(agent_name, logical_version):
            self._publication_fence()
            self._publish_version_artifact(
                agent_name,
                logical_version,
                foundry_version,
                content_digest,
                "traffic-receipt",
                _traffic_receipt_payload_from_mapping(payload),
                supplemental={"invocation": payload},
            )
            self._publication_fence()
            value = self._load(
                agent_name,
                logical_version,
                foundry_version,
                content_digest,
            )
            existing = value.get("invocation")
            if (
                isinstance(existing, dict)
                and content_hash(
                    _traffic_receipt_payload_from_mapping(existing)
                )
                != content_hash(
                    _traffic_receipt_payload_from_mapping(payload)
                )
            ):
                raise ContractError(
                    "Version checkpoint traffic receipt conflicts with prior traffic"
                )
            value["invocation"] = payload
            self._write(agent_name, logical_version, value)

    def operation_ids(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> tuple[str, ...] | None:
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        payload = value.get("operation_ids")
        if payload is None:
            return None
        if not isinstance(payload, list) or not all(
            isinstance(item, str) for item in payload
        ):
            raise ContractError("Version checkpoint operations are invalid")
        return tuple(payload)

    def save_operation_ids(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
        operation_ids: tuple[str, ...],
    ) -> None:
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        value["operation_ids"] = list(operation_ids)
        self._write(agent_name, logical_version, value)

    def save_insight_lookback(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
        lookback: dict,
    ) -> None:
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        if lookback.get("calculation_digest") != content_hash(
            {
                key: item
                for key, item in lookback.items()
                if key != "calculation_digest"
            }
        ):
            raise ContractError("Daily Insight lookback binding is invalid")
        existing = value.get("insight_lookback")
        if existing is not None and existing != lookback:
            raise ContractError("Daily Insight lookback is immutable")
        value["insight_lookback"] = lookback
        self._write(agent_name, logical_version, value)

    def insight_lookback(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> dict | None:
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        ).get("insight_lookback")
        if value is None:
            return None
        if not isinstance(value, dict) or value.get(
            "calculation_digest"
        ) != content_hash(
            {
                key: item
                for key, item in value.items()
                if key != "calculation_digest"
            }
        ):
            raise ContractError("Daily Insight lookback binding is invalid")
        return dict(value)

    def trace_verified(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> bool:
        return (
            self._load(
                agent_name,
                logical_version,
                foundry_version,
                content_digest,
            ).get("trace_verified")
            is True
        )

    def save_trace_verified(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> None:
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        value["trace_verified"] = True
        self._write(agent_name, logical_version, value)

    def insight_run(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> InsightRunCheckpoint | None:
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        payload = value.get("insight_run")
        if not isinstance(payload, dict):
            return None
        revisions = payload.get("before_revisions")
        if not isinstance(revisions, dict):
            raise ContractError("Version checkpoint Insight revisions are invalid")
        try:
            run_id = str(payload["run_id"])
            parsed = {
                str(key): (str(item[0]), int(item[1]))
                for key, item in revisions.items()
                if isinstance(item, list) and len(item) == 2
            }
        except (KeyError, TypeError, ValueError) as error:
            raise ContractError("Version checkpoint Insight run is invalid") from error
        if not run_id or len(parsed) != len(revisions):
            raise ContractError("Version checkpoint Insight run is invalid")
        return InsightRunCheckpoint(run_id=run_id, before_revisions=parsed)

    def insight_start_pending(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> bool:
        return (
            self._load(
                agent_name,
                logical_version,
                foundry_version,
                content_digest,
            ).get("insight_start_pending")
            is True
        )

    def mark_insight_start_pending(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> None:
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        prior = value.get("insight_start_outcome")
        retry_count = (
            int(prior.get("retry_count") or 0)
            if isinstance(prior, dict)
            else 0
        )
        if isinstance(prior, dict) and prior.get("status") == "explicit_no_run":
            if retry_count >= 1:
                raise ContractError("Agent Insights start retry is exhausted")
            retry_count += 1
        elif isinstance(prior, dict) and prior.get("status") in {
            "pending",
            "unknown",
            "started",
        }:
            raise ContractError("Agent Insights start outcome is unresolved")
        intent = {
            "status": "pending",
            "retry_count": retry_count,
            "operation_ids_digest": content_hash(value.get("operation_ids") or []),
            "intent_digest": "",
        }
        intent["intent_digest"] = content_hash(
            {key: item for key, item in intent.items() if key != "intent_digest"}
        )
        value["insight_start_outcome"] = intent
        value["insight_start_pending"] = True
        self._write(agent_name, logical_version, value)

    def insight_start_outcome(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> dict:
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        outcome = value.get("insight_start_outcome")
        if isinstance(outcome, dict):
            expected = content_hash(
                {key: item for key, item in outcome.items() if key != "intent_digest"}
            )
            if outcome.get("intent_digest") != expected:
                raise ContractError("Agent Insights start intent digest is stale")
            return dict(outcome)
        if value.get("insight_start_pending") is True:
            return {
                "status": "pending",
                "retry_count": 0,
                "operation_ids_digest": content_hash(
                    value.get("operation_ids") or []
                ),
                "intent_digest": None,
            }
        return {"status": "none", "retry_count": 0}

    def record_insight_start_outcome(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
        *,
        status: str,
    ) -> None:
        if status not in {"unknown", "explicit_no_run"}:
            raise ContractError("Agent Insights start outcome is invalid")
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        prior = self.insight_start_outcome(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        outcome = {
            "status": status,
            "retry_count": int(prior.get("retry_count") or 0),
            "operation_ids_digest": content_hash(value.get("operation_ids") or []),
            "intent_digest": "",
        }
        outcome["intent_digest"] = content_hash(
            {key: item for key, item in outcome.items() if key != "intent_digest"}
        )
        value["insight_start_outcome"] = outcome
        value["insight_start_pending"] = status == "unknown"
        self._write(agent_name, logical_version, value)

    def prepare_insight_start_retry(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> None:
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        outcome = value.get("insight_start_outcome")
        if (
            not isinstance(outcome, dict)
            or outcome.get("status") != "explicit_no_run"
            or "result" not in value
        ):
            raise ContractError("Agent Insights start retry proof is incomplete")
        digest = content_hash(value)
        immutable_json(
            self._root
            / "insight-start-history"
            / agent_name
            / logical_version
            / f"{digest.removeprefix('sha256:')}.json",
            value,
        )
        value.pop("result")
        self._write(agent_name, logical_version, value)

    def clear_insight_start_pending(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> None:
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        value.pop("insight_start_pending", None)
        self._write(agent_name, logical_version, value)

    def save_insight_run(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
        checkpoint: InsightRunCheckpoint,
    ) -> None:
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        value["insight_run"] = asdict(checkpoint)
        prior = self.insight_start_outcome(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        outcome = {
            "status": "started",
            "retry_count": int(prior.get("retry_count") or 0),
            "operation_ids_digest": content_hash(value.get("operation_ids") or []),
            "provider_reference_digest": content_hash(checkpoint.run_id),
            "intent_digest": "",
        }
        outcome["intent_digest"] = content_hash(
            {key: item for key, item in outcome.items() if key != "intent_digest"}
        )
        value["insight_start_outcome"] = outcome
        value.pop("insight_start_pending", None)
        self._write(agent_name, logical_version, value)

    def clear_insight_run(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> None:
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        if value.get("insight_run") is not None:
            digest = content_hash(
                {
                    "insight_run": value["insight_run"],
                    "insight_start_outcome": value.get(
                        "insight_start_outcome"
                    ),
                }
            )
            immutable_json(
                self._root
                / "insight-run-history"
                / agent_name
                / logical_version
                / f"{digest.removeprefix('sha256:')}.json",
                {
                    "schema_version": "1.0.0",
                    "run_contract_digest": self._run_contract_digest,
                    "agent_name": agent_name,
                    "logical_version": logical_version,
                    "foundry_version": foundry_version,
                    "content_digest": content_digest,
                    "insight_run": value["insight_run"],
                    "insight_start_outcome": value.get(
                        "insight_start_outcome"
                    ),
                    "history_digest": digest,
                },
            )
        value.pop("insight_run", None)
        value.pop("insight_start_outcome", None)
        value.pop("insight_start_pending", None)
        value.pop("insight_drain_pending", None)
        self._write(agent_name, logical_version, value)

    def insight_drain_pending(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> bool:
        return (
            self._load(
                agent_name,
                logical_version,
                foundry_version,
                content_digest,
            ).get("insight_drain_pending")
            is True
        )

    def clear_insight_drain_pending(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> None:
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        value.pop("insight_drain_pending", None)
        self._write(agent_name, logical_version, value)

    def result(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> VersionResult | None:
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        payload = value.get("result")
        artifact = self._read_version_artifact(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
            "result",
        )
        if artifact is not None:
            artifact_payload = artifact["value"]
            if isinstance(payload, dict) and payload != artifact_payload:
                raise ContractError(
                    "Version checkpoint conflicts with its immutable result"
                )
            payload = artifact_payload
        if not isinstance(payload, dict):
            return None
        supplemental_path = self._supplemental_result_path(
            agent_name,
            logical_version,
        )
        if supplemental_path.is_file():
            supplemental = read_json(supplemental_path)
            replacement = supplemental.get("result")
            if (
                supplemental.get("schema_version") != "1.0.0"
                or supplemental.get("run_contract_digest")
                != self._run_contract_digest
                or supplemental.get("agent_name") != agent_name
                or supplemental.get("logical_version") != logical_version
                or supplemental.get("foundry_version") != foundry_version
                or supplemental.get("content_digest") != content_digest
                or supplemental.get("supersedes_result_digest")
                != content_hash(payload)
                or not isinstance(replacement, dict)
                or supplemental.get("supplemental_digest")
                != content_hash(
                    {
                        key: item
                        for key, item in supplemental.items()
                        if key != "supplemental_digest"
                    }
                )
            ):
                raise ContractError("Supplemental version result is invalid")
            payload = replacement
        try:
            observed = [
                _insight_from_json(item)
                for item in payload.get("observed_insights", [])
            ]
            primary = payload.get("observed_insight")
            return VersionResult(
                logical_version=str(payload["logical_version"]),
                foundry_version=str(payload["foundry_version"]),
                status=str(payload["status"]),
                operation_ids=[str(item) for item in payload["operation_ids"]],
                insight_references=[
                    str(item) for item in payload["insight_references"]
                ],
                window_start=payload.get("window_start"),
                window_end=payload.get("window_end"),
                error_code=payload.get("error_code"),
                observed_insight=(
                    _insight_from_json(primary) if isinstance(primary, dict) else None
                ),
                observed_insights=observed,
                endpoint_request_count=int(payload["endpoint_request_count"]),
                endpoint_response_count=int(payload["endpoint_response_count"]),
                endpoint_usable_response_count=int(
                    payload["endpoint_usable_response_count"]
                ),
                semantic_assertion_count=int(payload["semantic_assertion_count"]),
                semantic_assertions_passed=int(
                    payload["semantic_assertions_passed"]
                ),
                trace_assertion_count=int(payload["trace_assertion_count"]),
                trace_assertions_passed=int(payload["trace_assertions_passed"]),
                trace_contract_verified=bool(payload["trace_contract_verified"]),
                trace_behavior_summary=dict(payload["trace_behavior_summary"]),
                trace_maturity_proof=payload.get("trace_maturity_proof"),
                role_pass_summary=payload.get("role_pass_summary"),
                endpoint_request_summaries=[
                    RequestCompletionEvidence(
                        request_index=int(item["request_index"]),
                        response_count=int(item["response_count"]),
                        usable_response=bool(item["usable_response"]),
                        semantic_assertion_count=int(item["semantic_assertion_count"]),
                        semantic_assertions_passed=int(
                            item["semantic_assertions_passed"]
                        ),
                        assertion_results=tuple(
                            SemanticAssertionEvidence(
                                assertion=str(result["assertion"]),
                                passed=bool(result["passed"]),
                                evidence_sufficient=bool(
                                    result["evidence_sufficient"]
                                ),
                            )
                            for result in item["assertion_results"]
                        ),
                        activation_gate=bool(item["activation_gate"]),
                        direct_terminal_response_count=int(
                            item["direct_terminal_response_count"]
                        ),
                        function_call_count=int(item["function_call_count"]),
                        trace_assertion_count=int(
                            item["trace_assertion_count"]
                        ),
                        trace_assertions_passed=int(
                            item["trace_assertions_passed"]
                        ),
                        trace_assertion_results=tuple(
                            TraceAssertionEvidence(
                                assertion=str(result["assertion"]),
                                passed=bool(result["passed"]),
                                evidence_sufficient=bool(
                                    result["evidence_sufficient"]
                                ),
                            )
                            for result in item["trace_assertion_results"]
                        ),
                        error_code=item.get("error_code"),
                    )
                    for item in payload["endpoint_request_summaries"]
                ],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ContractError("Version checkpoint result is invalid") from error

    def save_result(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
        result: VersionResult,
    ) -> None:
        payload = asdict(result)
        with self._version_lock(agent_name, logical_version):
            if result.status != "inconclusive":
                self._publication_fence()
                self._publish_version_artifact(
                    agent_name,
                    logical_version,
                    foundry_version,
                    content_digest,
                    "result",
                    payload,
                )
                self._publication_fence()
            value = self._load(
                agent_name,
                logical_version,
                foundry_version,
                content_digest,
            )
            existing = value.get("result")
            if (
                isinstance(existing, dict)
                and existing != payload
                and existing.get("status") != "inconclusive"
            ):
                raise ContractError(
                    "Version checkpoint result conflicts with a definitive result"
                )
            value["result"] = payload
            self._write(agent_name, logical_version, value)

    def save_supplemental_result(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
        result: VersionResult,
        *,
        event_digest: str,
    ) -> str:
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        original = value.get("result")
        if not isinstance(original, dict):
            raise ContractError("Original version result is missing")
        supplemental = {
            "schema_version": "1.0.0",
            "run_contract_digest": self._run_contract_digest,
            "agent_name": agent_name,
            "logical_version": logical_version,
            "foundry_version": foundry_version,
            "content_digest": content_digest,
            "event_digest": event_digest,
            "supersedes_result_digest": content_hash(original),
            "result": asdict(result),
            "supplemental_digest": "",
        }
        supplemental["supplemental_digest"] = content_hash(
            {
                key: item
                for key, item in supplemental.items()
                if key != "supplemental_digest"
            }
        )
        immutable_json(
            self._supplemental_result_path(agent_name, logical_version),
            supplemental,
        )
        return str(supplemental["supplemental_digest"])

    def supplemental_result_digest(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> str | None:
        path = self._supplemental_result_path(agent_name, logical_version)
        if not path.is_file():
            return None
        self.result(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        value = read_json(path)
        return str(value["supplemental_digest"])

    def save_rejected_result(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
        result: VersionResult,
        *,
        drain_pending: bool,
    ) -> None:
        payload = asdict(result)
        with self._version_lock(agent_name, logical_version):
            if result.status != "inconclusive":
                self._publication_fence()
                self._publish_version_artifact(
                    agent_name,
                    logical_version,
                    foundry_version,
                    content_digest,
                    "result",
                    payload,
                )
                self._publication_fence()
            value = self._load(
                agent_name,
                logical_version,
                foundry_version,
                content_digest,
            )
            value["result"] = payload
            if drain_pending:
                value["insight_drain_pending"] = True
            else:
                value.pop("insight_drain_pending", None)
            self._write(agent_name, logical_version, value)

    def clear(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> None:
        path = self._path(agent_name, logical_version)
        if not path.exists():
            return
        self._validate_header(
            read_json(path),
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        path.unlink()

    def _load(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> dict:
        path = self._path(agent_name, logical_version)
        if not path.exists():
            return {
                "schema_version": "1.0.0",
                "run_contract_digest": self._run_contract_digest,
                "agent_name": agent_name,
                "logical_version": logical_version,
                "foundry_version": foundry_version,
                "content_digest": content_digest,
            }
        value = read_json(path)
        self._validate_header(
            value,
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        return value

    def _write(self, agent_name: str, logical_version: str, value: dict) -> None:
        atomic_json(self._path(agent_name, logical_version), value)

    def _path(self, agent_name: str, logical_version: str) -> Path:
        if (
            not agent_name.endswith("-agent")
            or "/" in agent_name
            or "\\" in agent_name
            or "/" in logical_version
            or "\\" in logical_version
        ):
            raise ContractError("Version checkpoint identity is invalid")
        return self._root / f"{agent_name}-{logical_version}.json"

    def _version_lock(
        self,
        agent_name: str,
        logical_version: str,
    ) -> DailyLock:
        self._path(agent_name, logical_version)
        return DailyLock(
            self._version_artifact_root()
            / agent_name
            / logical_version
            / "version.lock",
            wait_seconds=5,
        )

    def _version_artifact_root(self) -> Path:
        return self._root / "version-artifacts"

    def _version_artifact_path(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
        kind: str,
    ) -> Path:
        self._path(agent_name, logical_version)
        identity = content_hash(
            {
                "run_contract_digest": self._run_contract_digest,
                "agent_name": agent_name,
                "logical_version": logical_version,
                "foundry_version": foundry_version,
                "content_digest": content_digest,
            }
        ).removeprefix("sha256:")
        return (
            self._version_artifact_root()
            / agent_name
            / logical_version
            / identity
            / f"{kind}.json"
        )

    def _publish_version_artifact(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
        kind: str,
        value: dict,
        *,
        supplemental: dict | None = None,
    ) -> None:
        record = {
            "schema_version": "1.0.0",
            "kind": f"daily-version-{kind}",
            "run_contract_digest": self._run_contract_digest,
            "agent_name": agent_name,
            "logical_version": logical_version,
            "foundry_version": foundry_version,
            "content_digest": content_digest,
            "value": value,
            **(supplemental or {}),
            "artifact_digest": "",
        }
        record["artifact_digest"] = content_hash(
            {
                key: item
                for key, item in record.items()
                if key != "artifact_digest"
            }
        )
        path = self._version_artifact_path(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
            kind,
        )
        if path.is_file():
            existing = self._read_version_artifact(
                agent_name,
                logical_version,
                foundry_version,
                content_digest,
                kind,
            )
            if (
                existing is None
                or content_hash(existing["value"]) != content_hash(value)
            ):
                raise ContractError(
                    f"Conflicting immutable Daily version {kind}"
                )
            return
        immutable_json(path, record)

    def _read_version_artifact(
        self,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
        kind: str,
    ) -> dict | None:
        path = self._version_artifact_path(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
            kind,
        )
        if not path.is_file():
            return None
        value = read_json(path)
        expected_digest = content_hash(
            {
                key: item
                for key, item in value.items()
                if key != "artifact_digest"
            }
        )
        if (
            value.get("schema_version") != "1.0.0"
            or value.get("kind") != f"daily-version-{kind}"
            or value.get("run_contract_digest") != self._run_contract_digest
            or value.get("agent_name") != agent_name
            or value.get("logical_version") != logical_version
            or value.get("foundry_version") != foundry_version
            or value.get("content_digest") != content_digest
            or value.get("artifact_digest") != expected_digest
            or not isinstance(value.get("value"), dict)
        ):
            raise ContractError(f"Immutable Daily version {kind} is invalid")
        return value

    def _artifact_candidates(
        self,
        agent_name: str,
        logical_version: str,
        kind: str,
    ) -> list[Path]:
        self._path(agent_name, logical_version)
        return sorted(
            (
                self._version_artifact_root()
                / agent_name
                / logical_version
            ).glob(f"*/{kind}.json")
        )

    def _version_identity(
        self,
        agent_name: str,
        logical_version: str,
    ) -> tuple[str, str] | None:
        path = self._path(agent_name, logical_version)
        if path.is_file():
            value = read_json(path)
            try:
                foundry_version = str(value["foundry_version"])
                content_digest = str(value["content_digest"])
            except KeyError as error:
                raise ContractError(
                    "Version checkpoint identity is invalid"
                ) from error
            self._validate_header(
                value,
                agent_name,
                logical_version,
                foundry_version,
                content_digest,
            )
            return foundry_version, content_digest
        candidates = self._artifact_candidates(
            agent_name,
            logical_version,
            "result",
        )
        if len(candidates) > 1:
            raise ContractError(
                "Daily version has conflicting immutable result identities"
            )
        if not candidates:
            return None
        value = read_json(candidates[0])
        return str(value["foundry_version"]), str(value["content_digest"])

    def _recovery_path(self, agent_name: str) -> Path:
        if (
            not agent_name.endswith("-agent")
            or "/" in agent_name
            or "\\" in agent_name
        ):
            raise ContractError("Recovery checkpoint identity is invalid")
        return self._root / "recovery" / f"{agent_name}.json"

    def _supplemental_result_path(
        self,
        agent_name: str,
        logical_version: str,
    ) -> Path:
        self._path(agent_name, logical_version)
        return (
            self._root
            / "supplemental-results"
            / f"{agent_name}-{logical_version}.json"
        )

    def _validate_header(
        self,
        value: dict,
        agent_name: str,
        logical_version: str,
        foundry_version: str,
        content_digest: str,
    ) -> None:
        expected = {
            "schema_version": "1.0.0",
            "run_contract_digest": self._run_contract_digest,
            "agent_name": agent_name,
            "logical_version": logical_version,
            "foundry_version": foundry_version,
            "content_digest": content_digest,
        }
        if any(value.get(key) != item for key, item in expected.items()):
            raise ContractError("Version checkpoint does not match the current contract")


def _insight_from_json(value: dict) -> InsightEvidence:
    return InsightEvidence(
        reference=str(value["reference"]),
        agent_version=str(value["agent_version"]),
        title=str(value["title"]),
        description=str(value["description"]),
        category=str(value["category"]),
        severity=str(value["severity"]),
        proposed_fix=str(value["proposed_fix"]),
        linked_operation_ids=tuple(value["linked_operation_ids"]),
        trace_count=int(value["trace_count"]),
        updated_at=str(value["updated_at"]),
    )


@contextmanager
def profile_run_lock(
    profile: str,
    run_id: str,
    root: Path | None = None,
) -> Iterator[None]:
    base = root or runtime_root()
    path = base / "run-locks" / f"{profile}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        _lock_file(handle)
    except OSError as error:
        handle.close()
        raise ActiveQualificationError(
            f"{profile} already has an active qualification"
        ) from error
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(
            json.dumps(
                {"schema_version": "1.0.0", "run_id": run_id},
                sort_keys=True,
            ).encode("ascii")
        )
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        handle.close()


def _lock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        if handle.read(1) == b"":
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
