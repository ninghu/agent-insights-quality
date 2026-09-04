from __future__ import annotations

import copy
import os
import shutil
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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
)
from agent_insights_quality.validation_assignments import (
    verification_assignment,
)

LIFECYCLE_SCHEMA = (
    ROOT / "schemas" / "test-agent-validation-lifecycle.schema.json"
)
STATES = {
    "LOCKED",
    "PREFLIGHT",
    "CREATING",
    "VALIDATING",
    "READY",
    "FAILED",
    "SUPERSEDED",
}
TERMINAL_STATES = {"READY", "FAILED", "SUPERSEDED"}
_TRANSITIONS = {
    "LOCKED": {"PREFLIGHT", "SUPERSEDED"},
    "PREFLIGHT": {"CREATING", "SUPERSEDED"},
    "CREATING": {"VALIDATING", "SUPERSEDED"},
    "VALIDATING": {"READY", "FAILED", "SUPERSEDED"},
    "READY": set(),
    "FAILED": set(),
    "SUPERSEDED": set(),
}


class ValidationLockBusy(ContractError):
    """The shared validation lock is temporarily owned by another process."""


@dataclass(frozen=True)
class LocalRecord:
    path: Path
    value: dict[str, Any]
    digest: str


def validation_runtime_root() -> Path:
    if "AIQ_RUNTIME_ROOT" in os.environ:
        raise ContractError(
            "Test Agent Validation does not permit a runtime-root override"
        )
    return (
        Path.home()
        / ".aiq-runtime"
        / "agent-insights-quality"
        / "test-agent-validation"
        / "environments"
        / "swedencentral-g30"
    ).resolve()


class LocalValidationLock:
    def __init__(
        self,
        path: Path | None = None,
        *,
        wait_seconds: float = 0,
        retry_seconds: float = 0.05,
    ) -> None:
        if wait_seconds < 0 or retry_seconds <= 0:
            raise ContractError("Local validation lock wait is invalid")
        self.path = path or (validation_runtime_root() / "validation.lock")
        self._wait_seconds = wait_seconds
        self._retry_seconds = retry_seconds
        self._stream: BinaryIO | None = None

    @property
    def owned(self) -> bool:
        return self._stream is not None

    def acquire(self) -> None:
        if self._stream is not None:
            raise ContractError("Local validation lock is already held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._wait_seconds
        while True:
            stream: BinaryIO | None = None
            try:
                stream = self.path.open("a+b")
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

                    fcntl.flock(
                        stream.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
            except OSError as error:
                if stream is not None:
                    stream.close()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValidationLockBusy(
                        "Another local Test Agent Validation process holds "
                        "the shared lock"
                    ) from error
                time.sleep(min(self._retry_seconds, remaining))
                continue
            self._stream = stream
            return

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

    def __enter__(self) -> LocalValidationLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class LifecycleJournal:
    def __init__(
        self,
        *,
        lock: LocalValidationLock,
        root: Path | None = None,
    ) -> None:
        self._lock = lock
        self.root = root or (validation_runtime_root() / "lifecycle")
        self.active_path = self.root / "active.json"

    def read_optional(self) -> LocalRecord | None:
        if not self.active_path.exists():
            return None
        return self._read(self.active_path)

    def read_active(self) -> LocalRecord:
        if not self.active_path.exists():
            raise ContractError("Local validation journal does not exist")
        return self._read(self.active_path)

    def superseded_run_ids(self, current: Mapping[str, Any]) -> list[str]:
        return [
            str(item["run_id"])
            for item in self.superseded_generations(current)
        ]

    def superseded_generations(
        self,
        current: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        validate_lifecycle(current)
        recovery_source = current.get("recovery_intent")
        if recovery_source is None:
            return []
        if (
            not isinstance(recovery_source, Mapping)
            or current["supersedes"]
            != recovery_source.get("source_journal_digest")
        ):
            raise ContractError("Validation recovery source reference is invalid")
        return [
            {
                "repository": str(current["repository"]),
                "pr_number": int(current["pr_number"]),
                "run_id": str(recovery_source["source_run_id"]),
            }
        ]

    def begin_run(
        self,
        initial: Mapping[str, Any],
        *,
        all_authority_ids: Sequence[str],
        now: datetime | None = None,
    ) -> tuple[LocalRecord, list[str]]:
        self._assert_locked()
        superseded_ids: list[str] = []
        supersedes: str | None = None
        if self.active_path.exists():
            try:
                current = self._read(self.active_path)
            except ContractError:
                # Superseded formats are retained but never interpreted or reused.
                supersedes = file_hash(self.active_path)
                superseded_ids = list(all_authority_ids)
                self._archive_superseded_format(supersedes)
            else:
                if current.value["state"] not in TERMINAL_STATES:
                    superseded_ids = list(
                        current.value["validation_authority_ids"]
                    )
                    current = self.commit(
                        current,
                        next_state="SUPERSEDED",
                        now=now,
                    )
                supersedes = current.digest
        value = copy.deepcopy(dict(initial))
        value["supersedes"] = supersedes
        value = stamp_lifecycle_digest(value)
        validate_lifecycle(value)
        if value["snapshot_type"] != "event" or value["state"] != "LOCKED":
            raise ContractError("New local validation must begin in LOCKED")
        event = self._write_event(value)
        active = copy.deepcopy(value)
        active["snapshot_type"] = "active"
        active["event_reference"] = _local_reference(event, self.root)
        active = stamp_lifecycle_digest(active)
        validate_lifecycle(active)
        atomic_json(self.active_path, active)
        return self._read(self.active_path), superseded_ids

    def _archive_superseded_format(self, digest: str) -> None:
        archive = (
            self.root
            / "superseded-formats"
            / f"{digest.removeprefix('sha256:')}.json"
        )
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists():
            if file_hash(archive) != digest:
                raise ContractError("Superseded lifecycle archive digest changed")
            return
        temporary = archive.with_name(f".{archive.name}.{os.getpid()}.tmp")
        with self.active_path.open("rb") as source, temporary.open("xb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, archive)
        if file_hash(archive) != digest:
            raise ContractError("Superseded lifecycle archive is not byte exact")

    def commit(
        self,
        current: LocalRecord,
        *,
        next_state: str,
        updates: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> LocalRecord:
        self._assert_locked()
        validate_lifecycle(current.value)
        disk = self.read_active()
        if (
            disk.value["journal_digest"] != current.value["journal_digest"]
            or disk.value["event_sequence"] != current.value["event_sequence"]
        ):
            raise ContractError("Local validation journal changed before mutation")
        self._validate_transition(current.value["state"], next_state)
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        event_value = copy.deepcopy(current.value)
        event_value["snapshot_type"] = "event"
        event_value["state"] = next_state
        event_value["event_sequence"] += 1
        event_value["last_activity_at"] = moment.isoformat()
        event_value["previous_journal_digest"] = current.value["journal_digest"]
        event_value["event_reference"] = None
        if updates:
            _merge(event_value, updates)
        for key in (
            "run_id",
            "repository",
            "pr_number",
            "started_at",
            "absolute_expires_at",
            "supersedes",
        ):
            if event_value[key] != current.value[key]:
                raise ContractError(f"Local validation {key} is immutable")
        expires = datetime.fromisoformat(
            str(current.value["absolute_expires_at"]).replace("Z", "+00:00")
        ).astimezone(UTC)
        if moment >= expires and next_state != "SUPERSEDED":
            raise ContractError("Local validation exceeded its absolute TTL")
        event_value = stamp_lifecycle_digest(event_value)
        validate_lifecycle(event_value)
        event = self._write_event(event_value)
        active = copy.deepcopy(event_value)
        active["snapshot_type"] = "active"
        active["event_reference"] = _local_reference(event, self.root)
        active = stamp_lifecycle_digest(active)
        validate_lifecycle(active)
        atomic_json(self.active_path, active)
        return self._read(self.active_path)

    def _write_event(self, value: Mapping[str, Any]) -> LocalRecord:
        snapshot = copy.deepcopy(dict(value))
        snapshot["snapshot_type"] = "event"
        snapshot["event_reference"] = None
        snapshot = stamp_lifecycle_digest(snapshot)
        validate_lifecycle(snapshot)
        path = self.root / _snapshot_name(snapshot)
        immutable_json(path, snapshot)
        return self._read(path)

    def _read(self, path: Path) -> LocalRecord:
        value = read_json(path)
        validate_lifecycle(value)
        return LocalRecord(
            path=path,
            value=value,
            digest=str(value["journal_digest"]),
        )

    def _assert_locked(self) -> None:
        if not self._lock.owned:
            raise ContractError(
                "Local validation journal mutation requires the shared process lock"
            )

    @staticmethod
    def _validate_transition(current: str, next_state: str) -> None:
        if current not in _TRANSITIONS or next_state not in STATES:
            raise ContractError("Local validation lifecycle state is invalid")
        if next_state != current and next_state not in _TRANSITIONS[current]:
            raise ContractError(
                f"Local validation cannot transition from {current} to {next_state}"
            )


def stamp_lifecycle_digest(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    result["journal_digest"] = ""
    result["journal_digest"] = content_hash(
        {key: item for key, item in result.items() if key != "journal_digest"}
    )
    return result


def validate_lifecycle(value: Mapping[str, Any]) -> None:
    errors = sorted(
        Draft202012Validator(
            read_json(LIFECYCLE_SCHEMA),
            format_checker=FormatChecker(),
        ).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "<root>"
        raise ContractError(
            f"Local validation lifecycle schema error at {location}: "
            f"{error.message}"
        )
    expected = content_hash(
        {key: item for key, item in value.items() if key != "journal_digest"}
    )
    if value["journal_digest"] != expected:
        raise ContractError("Local validation lifecycle digest is stale")
    recovery_intent = value["recovery_intent"]
    if recovery_intent is not None and recovery_intent["intent_digest"] != content_hash(
        {
            key: item
            for key, item in recovery_intent.items()
            if key != "intent_digest"
        }
    ):
        raise ContractError("Validation recovery intent digest is stale")
    if any(
        item["quota_plan_digest"] != value["digests"]["quota_plan_digest"]
        for item in value["deployment_assignments"]
    ):
        raise ContractError("Validation deployment assignment quota binding is stale")
    if value["state"] in {"VALIDATING", "READY", "FAILED"}:
        _validate_selection(value)
    if value["state"] in {"READY", "FAILED"} and value["evidence_reference"] is None:
        raise ContractError("Terminal validation lacks exact evidence")


def _validate_selection(value: Mapping[str, Any]) -> None:
    all_ids = {
        item["authority_id"] for item in value["runtime_topology"]["agents"]
    }
    selected = list(value["validation_authority_ids"])
    reused = [item["authority_id"] for item in value["reused_authorities"]]
    invoked = list(value["invocation_authority_ids"])
    reused_invocations = [
        item["authority_id"] for item in value["reused_invocations"]
    ]
    invoke_assigned = [
        authority_id
        for shard in value["invocation_shard_assignments"]
        for authority_id in shard["authority_ids"]
    ]
    verification_assignments = value["verification_authority_assignments"]
    assigned = [item["authority_id"] for item in verification_assignments]
    if (
        len(all_ids) != 41
        or len(selected) != len(set(selected))
        or len(reused) != len(set(reused))
        or set(selected).intersection(reused)
        or set(selected).union(reused) != all_ids
        or len(invoked) != len(set(invoked))
        or len(reused_invocations) != len(set(reused_invocations))
        or set(invoked).intersection(reused_invocations)
        or set(invoked).union(reused_invocations) != set(selected)
        or set(invoke_assigned) != set(invoked)
        or len(invoke_assigned) != len(set(invoke_assigned))
        or any(
            item["quota_plan_digest"]
            != value["digests"]["quota_plan_digest"]
            for item in value["invocation_shard_assignments"]
        )
        or set(assigned) != set(selected)
        or len(assigned) != len(set(assigned))
        or any(
            item != verification_assignment(value, item["authority_id"])
            for item in verification_assignments
        )
        or len(value["invocation_shard_assignments"]) > 8
        or [
            item["shard_id"]
            for item in value["invocation_shard_assignments"]
        ]
        != list(range(1, len(value["invocation_shard_assignments"]) + 1))
    ):
        raise ContractError("Validation authority selection is inconsistent")


def validate_topology_resource_bindings(
    topology: Mapping[str, Any],
    resources: list[Mapping[str, Any]],
) -> None:
    provider_ids = {str(item["provider_id"]) for item in resources}
    required: set[str] = set()
    for field in ("runtime_principal_ids", "telemetry_identity_ids"):
        item = topology.get(field)
        if isinstance(item, str) and item:
            required.add(item)
        elif isinstance(item, list):
            required.update(str(entry) for entry in item)
    for agent in topology.get("agents", []):
        for field in (
            "provider_agent_id",
            "provider_agent_version_id",
            "hosted_identity_id",
            "hosted_blueprint_id",
            "hosted_deployment_id",
            "runtime_principal_id",
            "telemetry_identity_id",
        ):
            item = agent.get(field)
            if isinstance(item, str) and item:
                required.add(item)
        required.update(str(entry) for entry in agent.get("connection_ids", []))
    missing = sorted(required - provider_ids)
    if missing:
        raise ContractError(
            "Validation runtime topology contains unjournaled resource identities"
        )


def read_bound_local_record(
    root: Path,
    reference: Any,
    *,
    digest_field: str,
    label: str,
) -> LocalRecord:
    if not isinstance(reference, Mapping):
        raise ContractError(f"Local {label} reference is missing")
    path = (root / str(reference.get("path") or "")).resolve()
    resolved_root = root.resolve()
    if resolved_root not in path.parents:
        raise ContractError(f"Local {label} reference escapes the runtime root")
    value = read_json(path)
    digest = value.get(digest_field)
    if (
        digest != reference.get("digest")
        or content_hash(
            {key: item for key, item in value.items() if key != digest_field}
        )
        != digest
    ):
        raise ContractError(f"Local {label} content digest is invalid")
    return LocalRecord(path=path, value=value, digest=str(digest))


def _merge(
    target: dict[str, Any],
    updates: Mapping[str, Any],
    *,
    path: tuple[str, ...] = (),
) -> None:
    for key, value in updates.items():
        if key not in target:
            raise ContractError(f"Lifecycle update contains unknown field: {key}")
        if isinstance(target[key], dict) and isinstance(value, Mapping):
            _merge(target[key], value, path=(*path, key))
        else:
            target[key] = copy.deepcopy(value)


def _snapshot_name(value: Mapping[str, Any]) -> Path:
    repository = value["repository"].replace("/", "--")
    digest = str(value["journal_digest"]).removeprefix("sha256:")
    return (
        Path("history")
        / repository
        / str(value["pr_number"])
        / str(value["run_id"])
        / (
            f"e{value['event_sequence']:06d}-"
            f"{value['state'].lower()}-{digest}.json"
        )
    )


def _local_reference(record: LocalRecord, root: Path) -> dict[str, str]:
    return {
        "path": record.path.relative_to(root).as_posix(),
        "digest": record.digest,
    }
