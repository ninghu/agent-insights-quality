from __future__ import annotations

import copy
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from jsonschema import Draft202012Validator, FormatChecker

from agent_insights_quality.util import (
    ROOT,
    ContractError,
    atomic_json,
    content_hash,
    file_hash,
    immutable_json,
    read_json,
    runtime_root,
)
from agent_insights_quality.validation_approved import validate_approval_binding

AGENT_ORDER = (
    "weather-agent",
    "healthcare-agent",
    "finance-agent",
    "travel-agent",
    "support-ticket-agent",
)
TERMINAL_STATES = {"COMPLETE", "FAILED"}
_TRANSITIONS = {
    "LOCKED": {"PREPARED", "FAILED"},
    "PREPARED": {"TRAFFIC", "FAILED"},
    "TRAFFIC": {"COMPOSED", "FAILED"},
    "COMPOSED": {"ASSESSMENTS_VALIDATED", "FAILED"},
    "ASSESSMENTS_VALIDATED": {"IMPROVEMENT_INPUT_READY", "FAILED"},
    "IMPROVEMENT_INPUT_READY": {"FINALIZED", "FAILED"},
    "FINALIZED": {"SEND_CLAIMED", "FAILED"},
    "SEND_CLAIMED": {"RECEIPT_IMPORTED", "FAILED"},
    "RECEIPT_IMPORTED": {"COMPLETE", "FAILED"},
    "COMPLETE": set(),
    "FAILED": set(),
}
_SCHEMA = ROOT / "schemas" / "daily-lifecycle.schema.json"


def daily_runtime_root(base: Path | None = None) -> Path:
    return (base or runtime_root()) / "daily-workflow"


class DailyLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: BinaryIO | None = None

    @property
    def owned(self) -> bool:
        return self._stream is not None

    def acquire(self) -> None:
        if self.owned:
            raise ContractError("Daily lock is already held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            stream.seek(0)
            if stream.read(1) == b"":
                stream.write(b"0")
                stream.flush()
                os.fsync(stream.fileno())
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            raise ContractError("Another Daily operation holds this lock") from error
        self._stream = stream

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._stream = None

    def __enter__(self) -> DailyLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


@dataclass(frozen=True)
class DailyRecord:
    path: Path
    value: dict[str, Any]
    digest: str


class DailyLifecycle:
    def __init__(self, *, lock: DailyLock, base: Path | None = None) -> None:
        self._lock = lock
        self.root = daily_runtime_root(base)
        self.active_path = self.root / "active.json"

    def read_optional(self) -> DailyRecord | None:
        return self._read(self.active_path) if self.active_path.is_file() else None

    def read_active(self) -> DailyRecord:
        if not self.active_path.is_file():
            raise ContractError("Daily lifecycle has not been prepared")
        return self._read(self.active_path)

    def begin(self, value: Mapping[str, Any]) -> DailyRecord:
        self._assert_locked()
        superseded_format_digest: str | None = None
        try:
            current = self.read_optional()
        except (ContractError, OSError, ValueError):
            superseded_format_digest = file_hash(self.active_path)
            self._archive_superseded_format(superseded_format_digest)
            current = None
        if current is not None:
            if current.value["bindings"] == value["bindings"]:
                return current
            if current.value["state"] not in TERMINAL_STATES:
                raise ContractError(
                    "Another Daily lifecycle is active; complete or explicitly fail it first"
                )
        if value.get("state") != "LOCKED" or value.get("event_sequence") != 0:
            raise ContractError(
                "New Daily lifecycle must begin at the LOCKED event"
            )
        if value.get("superseded_format_digest") is not None:
            raise ContractError(
                "New Daily lifecycle cannot supply a format supersession digest"
            )
        initial = copy.deepcopy(dict(value))
        initial["superseded_format_digest"] = superseded_format_digest
        event = self._stamp(initial, snapshot_type="event", event_reference=None)
        event_record = self._write_event(event)
        active = self._stamp(
            event,
            snapshot_type="active",
            event_reference=self._reference(event_record),
        )
        atomic_json(self.active_path, active)
        return self._read(self.active_path)

    def _archive_superseded_format(self, digest: str) -> None:
        archive = (
            self.root
            / "superseded-formats"
            / f"{digest.removeprefix('sha256:')}.json"
        )
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists():
            if file_hash(archive) != digest:
                raise ContractError("Superseded Daily lifecycle archive digest changed")
            return
        with self.active_path.open("rb") as source:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{archive.name}.",
                dir=archive.parent,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as target:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
                if file_hash(temporary) != digest:
                    raise ContractError(
                        "Superseded Daily lifecycle archive is not byte exact"
                    )
                os.replace(temporary, archive)
                if file_hash(archive) != digest:
                    raise ContractError(
                        "Superseded Daily lifecycle archive is not byte exact"
                    )
            finally:
                temporary.unlink(missing_ok=True)

    def transition(
        self,
        current: DailyRecord,
        *,
        next_state: str,
        artifact_updates: Mapping[str, Any] | None = None,
        binding_updates: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> DailyRecord:
        self._assert_locked()
        disk = self.read_active()
        if disk.digest != current.digest:
            raise ContractError("Daily lifecycle changed before mutation")
        state = str(current.value["state"])
        if next_state != state and next_state not in _TRANSITIONS[state]:
            raise ContractError(
                f"Daily lifecycle cannot transition from {state} to {next_state}"
            )
        value = copy.deepcopy(current.value)
        value["snapshot_type"] = "event"
        value["state"] = next_state
        value["event_sequence"] += 1
        value["last_activity_at"] = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        value["previous_lifecycle_digest"] = current.digest
        value["event_reference"] = None
        if artifact_updates:
            _merge(value["artifacts"], artifact_updates)
        if binding_updates:
            _merge(value["bindings"], binding_updates)
        for field in ("execution_id", "started_at"):
            if value[field] != current.value[field]:
                raise ContractError(f"Daily lifecycle {field} is immutable")
        if value.get("superseded_format_digest") != current.value.get(
            "superseded_format_digest"
        ):
            raise ContractError(
                "Daily lifecycle superseded_format_digest is immutable"
            )
        for field in (
            "repository",
            "public_run_id",
            "report_date",
            "delivery_mode",
            "publish_preview",
            "work_items",
            "approval",
            "catalog_hashes",
            "selection",
            "policy",
        ):
            if value["bindings"][field] != current.value["bindings"][field]:
                raise ContractError(f"Daily lifecycle binding {field} is immutable")
        event = self._stamp(value, snapshot_type="event", event_reference=None)
        event_record = self._write_event(event)
        active = self._stamp(
            event,
            snapshot_type="active",
            event_reference=self._reference(event_record),
        )
        atomic_json(self.active_path, active)
        return self._read(self.active_path)

    def _write_event(self, value: Mapping[str, Any]) -> DailyRecord:
        digest = str(value["lifecycle_digest"]).removeprefix("sha256:")
        path = (
            self.root
            / "executions"
            / str(value["execution_id"])
            / "history"
            / f"e{value['event_sequence']:04d}-{value['state'].lower()}-{digest}.json"
        )
        immutable_json(path, value)
        return self._read(path)

    def _read(self, path: Path) -> DailyRecord:
        value = read_json(path)
        validate_daily_lifecycle(value)
        return DailyRecord(path, value, str(value["lifecycle_digest"]))

    def _stamp(
        self,
        value: Mapping[str, Any],
        *,
        snapshot_type: str,
        event_reference: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        result = copy.deepcopy(dict(value))
        result["snapshot_type"] = snapshot_type
        result["event_reference"] = (
            copy.deepcopy(dict(event_reference))
            if event_reference is not None
            else None
        )
        result["lifecycle_digest"] = ""
        result["lifecycle_digest"] = content_hash(
            {key: item for key, item in result.items() if key != "lifecycle_digest"}
        )
        validate_daily_lifecycle(result)
        return result

    def _reference(self, record: DailyRecord) -> dict[str, str]:
        return {
            "path": record.path.relative_to(self.root).as_posix(),
            "digest": record.digest,
        }

    def _assert_locked(self) -> None:
        if not self._lock.owned:
            raise ContractError("Daily lifecycle mutation requires the coordinator lock")


def validate_daily_lifecycle(value: Mapping[str, Any]) -> None:
    errors = sorted(
        Draft202012Validator(
            read_json(_SCHEMA),
            format_checker=FormatChecker(),
        ).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ContractError(
            f"Daily lifecycle schema error at {location}: {error.message}"
        )
    expected = content_hash(
        {key: item for key, item in value.items() if key != "lifecycle_digest"}
    )
    if value["lifecycle_digest"] != expected:
        raise ContractError("Daily lifecycle digest is stale")
    approval = value["bindings"]["approval"]
    validate_approval_binding(
        approval,
        expected_checkout_commit_sha=approval["checkout_commit_sha"],
        expected_validation_digest=approval["validation_digest"],
    )
    selection = value["bindings"]["selection"]
    if set(selection) != set(AGENT_ORDER):
        raise ContractError("Daily Agent lane inventory is not canonical")
    if sum(len(issue_ids) for issue_ids in selection.values()) != 20:
        raise ContractError("Daily lifecycle must bind exactly 20 issues")
    report_date = date.fromisoformat(value["bindings"]["report_date"])
    if (
        date.fromisoformat(value["bindings"]["work_items"]["closed_business_date"])
        != report_date - timedelta(days=1)
    ):
        raise ContractError("Daily work-item closed-business date is not bound")
    preview_requested = value["bindings"]["publish_preview"]
    if preview_requested and (
        value["bindings"]["delivery_mode"] != "test_email_only"
        or re.fullmatch(
            r"aiq-[0-9]{8}-r(?:0[1-9]|[1-9][0-9]+)",
            value["bindings"]["public_run_id"],
            re.ASCII,
        )
        is None
    ):
        raise ContractError(
            "Daily GitHub preview requires an email-only nonzero rerun"
        )
    if value["state"] not in {"LOCKED", "FAILED"} and (
        value["bindings"]["registry"] is None
        or value["bindings"]["run_contract_digest"] is None
    ):
        raise ContractError("Prepared Daily lifecycle lacks registry bindings")
    state = value["state"]
    artifacts = value["artifacts"]
    progressed = {
        "COMPOSED",
        "ASSESSMENTS_VALIDATED",
        "IMPROVEMENT_INPUT_READY",
        "FINALIZED",
        "SEND_CLAIMED",
        "RECEIPT_IMPORTED",
        "COMPLETE",
    }
    if state in progressed and (
        artifacts["manifest"] is None
        or any(
            artifacts["lane_receipts"][agent_name] is None
            for agent_name in AGENT_ORDER
        )
    ):
        raise ContractError("Composed Daily lifecycle lacks exact Agent receipts")
    if state in progressed - {"COMPOSED"} and artifacts["assessment_index"] is None:
        raise ContractError("Daily lifecycle lacks validated assessment outputs")
    if state in progressed - {"COMPOSED", "ASSESSMENTS_VALIDATED"} and (
        artifacts["improvement_input"] is None
    ):
        raise ContractError("Daily lifecycle lacks its improvement input")
    if state in {"FINALIZED", "SEND_CLAIMED", "RECEIPT_IMPORTED", "COMPLETE"} and (
        artifacts["improvement_analysis"] is None
        or artifacts["final_report"] is None
        or artifacts["email_request"] is None
        or artifacts["adx_publication_status"] is None
    ):
        raise ContractError("Finalized Daily lifecycle lacks publication artifacts")
    preview_publication = artifacts["preview_publication"]
    if state in {"FINALIZED", "SEND_CLAIMED", "RECEIPT_IMPORTED", "COMPLETE"}:
        if preview_requested != (preview_publication is not None):
            raise ContractError("Daily GitHub preview lifecycle binding is incomplete")
    elif preview_publication is not None:
        raise ContractError("Daily GitHub preview was recorded before finalization")
    if state in {"SEND_CLAIMED", "RECEIPT_IMPORTED", "COMPLETE"} and (
        artifacts["send_claim"] is None
    ):
        raise ContractError("Daily lifecycle lacks its one-time send claim")
    if state in {"RECEIPT_IMPORTED", "COMPLETE"} and artifacts["email_receipt"] is None:
        raise ContractError("Daily lifecycle lacks its provider delivery receipt")
    if (
        state == "COMPLETE"
        and value["bindings"]["delivery_mode"] == "official"
        and artifacts["publication"] is None
    ):
        raise ContractError("Official Daily lifecycle lacks its one pull request receipt")


def artifact_reference(path: Path, root: Path, digest: str) -> dict[str, str]:
    resolved = path.resolve()
    resolved_root = root.resolve()
    if resolved_root != resolved and resolved_root not in resolved.parents:
        raise ContractError("Daily artifact escapes its private runtime root")
    return {
        "path": resolved.relative_to(resolved_root).as_posix(),
        "digest": digest,
    }


def read_artifact_reference(
    reference: Mapping[str, str],
    root: Path,
    *,
    digest_field: str,
) -> tuple[Path, dict[str, Any]]:
    path = (root / reference["path"]).resolve()
    if root.resolve() not in path.parents:
        raise ContractError("Daily artifact reference escapes its runtime root")
    value = read_json(path)
    digest = value.get(digest_field)
    if digest != reference["digest"]:
        raise ContractError("Daily artifact reference digest is stale")
    return path, value


def _merge(target: dict[str, Any], updates: Mapping[str, Any]) -> None:
    for key, value in updates.items():
        if key not in target:
            raise ContractError(f"Daily lifecycle update contains unknown field: {key}")
        if isinstance(target[key], dict) and isinstance(value, Mapping):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)
