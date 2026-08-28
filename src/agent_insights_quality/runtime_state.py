from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from agent_insights_quality.models import (
    InsightEvidence,
    InsightRunCheckpoint,
    InvocationEvidence,
    RequestCompletionEvidence,
    SemanticAssertionEvidence,
    VersionResult,
)
from agent_insights_quality.util import ContractError, atomic_json, read_json, runtime_root


class ActiveQualificationError(ContractError):
    """The selected profile is already executing qualification traffic."""


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
    def __init__(self, root: Path, run_contract_digest: str) -> None:
        self._root = root
        self._run_contract_digest = run_contract_digest

    def has_progress(self, agent_name: str) -> bool:
        return any(self._root.glob(f"{agent_name}-*.json"))

    def has_version_progress(self, agent_name: str, logical_version: str) -> bool:
        return self._path(agent_name, logical_version).exists()

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
                            )
                            for result in item["assertion_results"]
                        ),
                        activation_gate=bool(item["activation_gate"]),
                        direct_terminal_response_count=int(
                            item["direct_terminal_response_count"]
                        ),
                        function_call_count=int(item["function_call_count"]),
                    )
                    for item in payload["request_summaries"]
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
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        value["invocation"] = asdict(invocation)
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
        value["insight_start_pending"] = True
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
        value.pop("insight_run", None)
        value.pop("insight_start_pending", None)
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
        if not isinstance(payload, dict):
            return None
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
                trace_contract_verified=bool(payload["trace_contract_verified"]),
                trace_behavior_summary=dict(payload["trace_behavior_summary"]),
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
                            )
                            for result in item["assertion_results"]
                        ),
                        activation_gate=bool(item["activation_gate"]),
                        direct_terminal_response_count=int(
                            item["direct_terminal_response_count"]
                        ),
                        function_call_count=int(item["function_call_count"]),
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
        value = self._load(
            agent_name,
            logical_version,
            foundry_version,
            content_digest,
        )
        value["result"] = asdict(result)
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
        _unlock_file(handle)
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


def _unlock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
