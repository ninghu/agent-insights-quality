from __future__ import annotations

import copy
import os
from collections.abc import Mapping
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
    immutable_json,
    read_json,
)

LIFECYCLE_SCHEMA = (
    ROOT / "schemas" / "test-agent-validation-lifecycle.schema.json"
)
STATES = {
    "LOCKED",
    "PREFLIGHT",
    "CREATING",
    "VALIDATING",
    "FINAL_CHECKS",
    "CLEANING",
    "CLEAN",
    "FAILED_CLEAN",
    "CLEANUP_BLOCKED",
}
TERMINAL_STATES = {"CLEAN", "FAILED_CLEAN"}
_TRANSITIONS = {
    "LOCKED": {"PREFLIGHT", "CLEANING"},
    "PREFLIGHT": {"CREATING", "CLEANING"},
    "CREATING": {"VALIDATING", "CLEANING"},
    "VALIDATING": {"FINAL_CHECKS", "CLEANING"},
    "FINAL_CHECKS": {"CLEANING"},
    "CLEANING": {"CLEAN", "FAILED_CLEAN", "CLEANUP_BLOCKED"},
    "CLEANUP_BLOCKED": {"CLEANING"},
    "CLEAN": set(),
    "FAILED_CLEAN": set(),
}
_REQUIRED_FIELDS = {
    "schema_version",
    "kind",
    "snapshot_type",
    "cycle_id",
    "revision",
    "state",
    "repository",
    "pr_number",
    "commit_sha",
    "digests",
    "operator",
    "substrate",
    "ownership_nonce",
    "capacity",
    "project",
    "runtime_topology",
    "deployment",
    "resources",
    "cleanup",
    "event_reference",
    "clean_reference",
    "evidence_reference",
    "started_at",
    "last_activity_at",
    "absolute_expires_at",
    "failure",
    "previous_journal_digest",
    "journal_digest",
}


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
    ).resolve()


class LocalValidationLock:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (validation_runtime_root() / "validation.lock")
        self._stream: BinaryIO | None = None

    @property
    def owned(self) -> bool:
        return self._stream is not None

    def acquire(self) -> None:
        if self._stream is not None:
            raise ContractError("Local validation lock is already held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if stream is not None:
                stream.close()
            raise ContractError(
                "Another local Test Agent Validation process holds the shared lock"
            ) from error
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

    def begin_cycle(self, initial: Mapping[str, Any]) -> LocalRecord:
        self._assert_locked()
        current = self.read_optional()
        if current is not None and current.value["state"] not in TERMINAL_STATES:
            raise ContractError(
                "Incomplete local validation must be cleaned before a new cycle"
            )
        value = stamp_lifecycle_digest(initial)
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
        return self._read(self.active_path)

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
            or disk.value["revision"] != current.value["revision"]
        ):
            raise ContractError("Local validation journal changed before mutation")
        self._validate_transition(current.value["state"], next_state)
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        event_value = copy.deepcopy(current.value)
        event_value["snapshot_type"] = "event"
        event_value["state"] = next_state
        event_value["revision"] += 1
        event_value["last_activity_at"] = moment.isoformat()
        event_value["previous_journal_digest"] = current.value["journal_digest"]
        event_value["event_reference"] = None
        if updates:
            _merge(event_value, updates)
        for key in (
            "cycle_id",
            "repository",
            "pr_number",
            "started_at",
            "absolute_expires_at",
        ):
            if event_value[key] != current.value[key]:
                raise ContractError(f"Local validation {key} is immutable")
        expires = datetime.fromisoformat(
            str(current.value["absolute_expires_at"]).replace("Z", "+00:00")
        ).astimezone(UTC)
        if moment >= expires and next_state not in {
            "CLEANING",
            "CLEAN",
            "FAILED_CLEAN",
            "CLEANUP_BLOCKED",
        }:
            raise ContractError("Local validation exceeded its absolute TTL")
        event_value = stamp_lifecycle_digest(event_value)
        validate_lifecycle(event_value)
        event = self._write_event(event_value)

        active = copy.deepcopy(event_value)
        active["snapshot_type"] = "active"
        active["event_reference"] = _local_reference(event, self.root)
        if next_state in TERMINAL_STATES:
            clean_value = copy.deepcopy(event_value)
            clean_value["snapshot_type"] = "clean"
            clean_value["event_reference"] = None
            clean_value["clean_reference"] = None
            clean_value = stamp_lifecycle_digest(clean_value)
            validate_lifecycle(clean_value)
            clean = self._write_clean(clean_value)
            active["clean_reference"] = _local_reference(clean, self.root)
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
        path = self.root / _snapshot_name(snapshot, "history")
        immutable_json(path, snapshot)
        return self._read(path)

    def _write_clean(self, value: Mapping[str, Any]) -> LocalRecord:
        path = self.root / _snapshot_name(value, "clean")
        immutable_json(path, value)
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
    if set(value) != _REQUIRED_FIELDS:
        raise ContractError("Local validation lifecycle fields are invalid")
    if (
        value["schema_version"] != "1.0.0"
        or value["kind"] != "test-agent-validation-lifecycle"
        or value["snapshot_type"] not in {"active", "event", "clean"}
        or value["state"] not in STATES
        or not isinstance(value["revision"], int)
        or value["revision"] < 1
        or not isinstance(value["resources"], list)
    ):
        raise ContractError("Local validation lifecycle contract is invalid")
    expected = content_hash(
        {key: item for key, item in value.items() if key != "journal_digest"}
    )
    if value["journal_digest"] != expected:
        raise ContractError("Local validation lifecycle digest is stale")
    deployment = value["deployment"]
    ready_ids = {
        item["authority_id"] for item in value["runtime_topology"]["agents"]
    }
    recovery_ids = [
        item["authority_id"] for item in deployment["recoveries"]
    ]
    failure_keys = [
        (item["authority_id"], item["stage"])
        for item in deployment["failures"]
    ]
    correlation_count_fields = {
        "matched_reference_count",
        "expected_reference_count",
        "missing_reference_count",
    }
    if (
        len(ready_ids) != len(value["runtime_topology"]["agents"])
        or len(recovery_ids) != len(set(recovery_ids))
        or len(failure_keys) != len(set(failure_keys))
        or any(
            (
                correlation_count_fields.intersection(item)
                and not correlation_count_fields.issubset(item)
            )
            or (
                correlation_count_fields.issubset(item)
                and item["matched_reference_count"]
                + item["missing_reference_count"]
                != item["expected_reference_count"]
            )
            for item in deployment["failures"]
        )
        or (
            deployment["phase"] == "phase_1_traffic"
            and len(value["runtime_topology"]["agents"]) != 2
        )
        or (
            deployment["phase"] == "phase_2_deployment"
            and not 2 <= len(value["runtime_topology"]["agents"]) <= 41
        )
        or (
            deployment["phase"] in {"phase_2_traffic", "complete"}
            and len(value["runtime_topology"]["agents"]) != 41
        )
        or (
            deployment["phase"] != "phase_1_deployment"
            and deployment["traffic_started"] is not True
        )
        or (
            deployment["phase"] == "phase_1_deployment"
            and deployment["traffic_started"] is not False
        )
        or any(
            (item["state"] == "ready") != (item["authority_id"] in ready_ids)
            for item in deployment["recoveries"]
        )
        or any(
            sum(
                1
                for recovery in deployment["recoveries"]
                if recovery["canonical_agent"] == agent
            )
            > 3
            for agent in {
                item["canonical_agent"]
                for item in deployment["recoveries"]
            }
        )
    ):
        raise ContractError(
            "Local validation deployment progress is inconsistent"
        )
    operator = value["operator"]
    if not isinstance(operator, Mapping) or set(operator) != {
        "session_reference",
        "operator_reference",
        "run_reference",
    }:
        raise ContractError("Local validation operator provenance is invalid")
    if not all(_hash_reference(operator[key]) for key in operator):
        raise ContractError("Local validation operator provenance is incomplete")
    if (
        not isinstance(value["ownership_nonce"], str)
        or len(value["ownership_nonce"]) < 8
    ):
        raise ContractError("Local validation ownership nonce is invalid")
    if (
        not isinstance(value["commit_sha"], str)
        or len(value["commit_sha"]) != 40
    ):
        raise ContractError("Local validation commit identity is invalid")
    if value["state"] == "CLEAN":
        cleanup = value["cleanup"]
        if (
            not isinstance(cleanup, Mapping)
            or cleanup.get("exact_clean") is not True
            or cleanup.get("residue_ids")
        ):
            raise ContractError("CLEAN requires exact local cleanup proof")
        if value["snapshot_type"] == "active" and value["clean_reference"] is None:
            raise ContractError("CLEAN active journal lacks its immutable snapshot")
    if value["state"] == "FINAL_CHECKS" and value["evidence_reference"] is None:
        raise ContractError("FINAL_CHECKS requires local evidence")
    for field in ("event_reference", "clean_reference", "evidence_reference"):
        reference = value[field]
        if reference is not None and (
            not isinstance(reference, Mapping)
            or set(reference) != {"path", "digest"}
            or not isinstance(reference["path"], str)
            or not _hash_reference(reference["digest"])
        ):
            raise ContractError(f"Local validation {field} is invalid")


def validate_topology_resource_bindings(
    topology: Mapping[str, Any],
    resources: list[Mapping[str, Any]],
) -> None:
    provider_ids = {str(item["provider_id"]) for item in resources}
    required: set[str] = set()
    for field in (
        "project_reference",
        "connection_ids",
        "runtime_principal_ids",
        "telemetry_identity_ids",
    ):
        value = topology.get(field)
        if isinstance(value, str) and value:
            required.add(value)
        elif isinstance(value, list):
            required.update(str(item) for item in value)
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
            value = agent.get(field)
            if isinstance(value, str) and value:
                required.add(value)
        required.update(str(item) for item in agent.get("connection_ids", []))
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
            if path == ("cleanup",) and key == "failure":
                target[key] = copy.deepcopy(value)
                continue
            raise ContractError(f"Lifecycle update contains unknown field: {key}")
        if isinstance(target[key], dict) and isinstance(value, Mapping):
            _merge(target[key], value, path=(*path, key))
        else:
            target[key] = copy.deepcopy(value)


def _snapshot_name(value: Mapping[str, Any], prefix: str) -> Path:
    repository = value["repository"].replace("/", "--")
    digest = str(value["journal_digest"]).removeprefix("sha256:")
    return (
        Path(prefix)
        / repository
        / str(value["pr_number"])
        / str(value["cycle_id"])
        / f"r{value['revision']:06d}-{value['state'].lower()}-{digest}.json"
    )


def _local_reference(record: LocalRecord, root: Path) -> dict[str, str]:
    return {
        "path": record.path.relative_to(root).as_posix(),
        "digest": record.digest,
    }


def _hash_reference(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )
