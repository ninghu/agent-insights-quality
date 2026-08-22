from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import date
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import RuntimeFailure

_FORBIDDEN_KEY = re.compile(
    r"(?i)(?:access|refresh)?token|authorization|connection.?string|(?:api|account|client)?key|"
    r"client.?secret|password|credential|subscription.?id|tenant.?id|resource.?id|endpoint|url"
)
_FORBIDDEN_VALUE = re.compile(
    r"(?i)(?:bearer\s+|instrumentationkey=|accountkey=|sharedaccesssignature=|"
    r"https?://|/subscriptions/)"
)
_PRIVATE_TELEMETRY_ID = re.compile(r"^(?:[0-9a-f]{16}|[0-9a-f]{32})$")


def ensure_public_safe(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            if _FORBIDDEN_KEY.search(key):
                raise RuntimeFailure(
                    "private_value_in_receipt",
                    f"Receipt field {path}.{key} is not public-safe.",
                )
            ensure_public_safe(child, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            ensure_public_safe(child, f"{path}[{index}]")
        return
    if isinstance(value, str) and (
        _FORBIDDEN_VALUE.search(value) or _PRIVATE_TELEMETRY_ID.fullmatch(value)
    ):
        raise RuntimeFailure(
            "private_value_in_receipt",
            f"Receipt value at {path} is not public-safe.",
        )


def write_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    ensure_public_safe(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_receipt(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeFailure("invalid_receipt", "Runtime receipt is missing or invalid.") from error
    if not isinstance(payload, dict):
        raise RuntimeFailure("invalid_receipt", "Runtime receipt must be a JSON object.")
    ensure_public_safe(payload)
    return payload


def opaque_reference(*values: str) -> str:
    import hashlib

    encoded = "\x1f".join(values).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class MonitorOwnership:
    key: str
    project_reference: str
    agent_reference: str
    monitor_reference: str
    model_reference: str
    expires_on: str


class MonitorOwnershipRegistry:
    """Public-safe ownership receipts without recoverable project or monitor identifiers."""

    def __init__(self, path: Path, project_identity: str) -> None:
        self._path = path
        self._project_reference = opaque_reference(project_identity)
        self._lock = threading.RLock()

    def _key(self, agent_name: str, monitor_id: str) -> str:
        return opaque_reference(self._project_reference, agent_name, monitor_id)

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"schema_version": "1.0.0", "monitors": {}}
        payload = read_receipt(self._path)
        if payload.get("schema_version") != "1.0.0" or not isinstance(
            payload.get("monitors"), Mapping
        ):
            raise RuntimeFailure("invalid_monitor_receipt", "Monitor ownership receipt is invalid.")
        return payload

    def record(
        self,
        *,
        agent_name: str,
        monitor_id: str,
        model_deployment_name: str,
        expires_on: date,
    ) -> None:
        with self._lock:
            payload = self._load()
            key = self._key(agent_name, monitor_id)
            payload["monitors"][key] = {
                "project_reference": self._project_reference,
                "agent_reference": opaque_reference(agent_name),
                "monitor_reference": opaque_reference(monitor_id),
                "model_reference": opaque_reference(model_deployment_name),
                "expires_on": expires_on.isoformat(),
            }
            try:
                write_receipt(self._path, payload)
            except OSError as error:
                raise RuntimeFailure(
                    "monitor_receipt_write_failed",
                    "Monitor ownership receipt could not be persisted.",
                ) from error

    def require(
        self,
        *,
        agent_name: str,
        monitor_id: str,
        model_deployment_name: str | None = None,
    ) -> MonitorOwnership:
        payload = self._load()
        key = self._key(agent_name, monitor_id)
        value = payload["monitors"].get(key)
        if not isinstance(value, Mapping):
            raise RuntimeFailure("ownership_mismatch", "Monitor has no matching ownership receipt.")
        expected_model = (
            opaque_reference(model_deployment_name) if model_deployment_name is not None else None
        )
        if (
            value.get("project_reference") != self._project_reference
            or value.get("agent_reference") != opaque_reference(agent_name)
            or value.get("monitor_reference") != opaque_reference(monitor_id)
            or (expected_model is not None and value.get("model_reference") != expected_model)
        ):
            raise RuntimeFailure("ownership_mismatch", "Monitor ownership receipt does not match.")
        return MonitorOwnership(
            key=key,
            project_reference=str(value["project_reference"]),
            agent_reference=str(value["agent_reference"]),
            monitor_reference=str(value["monitor_reference"]),
            model_reference=str(value["model_reference"]),
            expires_on=str(value["expires_on"]),
        )

    def remove(self, *, agent_name: str, monitor_id: str) -> None:
        with self._lock:
            payload = self._load()
            payload["monitors"].pop(self._key(agent_name, monitor_id), None)
            try:
                write_receipt(self._path, payload)
            except OSError as error:
                raise RuntimeFailure(
                    "monitor_receipt_write_failed",
                    "Monitor ownership receipt could not be persisted.",
                ) from error
