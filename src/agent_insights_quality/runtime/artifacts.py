from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .errors import RuntimeFailure

_OPAQUE_REFERENCE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    name: str
    reference: str
    created_at: datetime
    expires_at: datetime
    owner_reference: str

    def public_dict(self) -> dict[str, str]:
        return {
            "name_reference": "sha256:" + hashlib.sha256(self.name.encode("utf-8")).hexdigest(),
            "reference": self.reference,
            "created_at": self.created_at.astimezone(UTC).isoformat(),
            "expires_at": self.expires_at.astimezone(UTC).isoformat(),
            "owner_reference": self.owner_reference,
        }


class ArtifactStore(Protocol):
    def put(self, name: str, content: bytes, owner_reference: str) -> ArtifactRecord: ...

    def get(self, name: str) -> bytes: ...

    def cleanup_expired(
        self,
        owner_reference: str,
        *,
        now: datetime | None = None,
        dry_run: bool = True,
    ) -> list[str]: ...


def _safe_name(name: str) -> str:
    candidate = name.replace("\\", "/")
    parts = candidate.split("/")
    if (
        not name
        or name.startswith(("/", "\\"))
        or any(part in {"", ".", ".."} for part in parts)
        or any("\x00" in part for part in parts)
    ):
        raise RuntimeFailure("invalid_artifact_name", "Artifact name is not a safe relative path.")
    return "/".join(parts)


def _reference(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class LocalArtifactStore:
    def __init__(self, root: Path, *, retention_days: int = 90) -> None:
        self._root = root.resolve()
        self._retention = timedelta(days=retention_days)

    def _path(self, name: str) -> Path:
        path = (self._root / _safe_name(name)).resolve()
        if self._root not in path.parents:
            raise RuntimeFailure("invalid_artifact_name", "Artifact escaped the configured root.")
        return path

    def put(self, name: str, content: bytes, owner_reference: str) -> ArtifactRecord:
        path = self._path(name)
        metadata_path = path.with_suffix(path.suffix + ".metadata.json")
        now = datetime.now(UTC)
        record = ArtifactRecord(name, _reference(content), now, now + self._retention, owner_reference)
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata = record.public_dict() | {"name": record.name, "state": "pending"}
        try:
            _write_json_atomic(metadata_path, metadata)
        except OSError as error:
            raise RuntimeFailure("artifact_write_failed", "Artifact pending manifest could not be written.") from error
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
            )
        except OSError as error:
            raise RuntimeFailure("artifact_write_failed", "Artifact payload could not be staged.") from error
        temporary = Path(temporary_name)
        try:
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            _write_json_atomic(metadata_path, metadata | {"state": "committed"})
        except OSError as error:
            raise RuntimeFailure(
                "artifact_write_failed",
                "Artifact payload or committed manifest could not be written.",
            ) from error
        return record

    def get(self, name: str) -> bytes:
        path = self._path(name)
        metadata_path = path.with_suffix(path.suffix + ".metadata.json")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            content = path.read_bytes()
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeFailure("artifact_not_found", "Artifact could not be read.") from error
        if (
            metadata.get("state") != "committed"
            or metadata.get("reference") != _reference(content)
        ):
            raise RuntimeFailure("artifact_incomplete", "Artifact manifest is incomplete or mismatched.")
        return content

    def cleanup_expired(
        self,
        owner_reference: str,
        *,
        now: datetime | None = None,
        dry_run: bool = True,
    ) -> list[str]:
        selected: list[str] = []
        failures = 0
        threshold = now or datetime.now(UTC)
        if not self._root.exists():
            return selected
        for metadata_path in self._root.rglob("*.metadata.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                expires = datetime.fromisoformat(str(metadata["expires_at"]))
                name = _safe_name(str(metadata["name"]))
                state = str(metadata.get("state") or "")
            except (OSError, KeyError, ValueError, json.JSONDecodeError, RuntimeFailure):
                continue
            if (
                metadata.get("owner_reference") != owner_reference
                or state not in {"pending", "committed"}
                or metadata.get("name_reference")
                != "sha256:" + hashlib.sha256(name.encode("utf-8")).hexdigest()
                or not _OPAQUE_REFERENCE.fullmatch(str(metadata.get("reference") or ""))
                or expires >= threshold
            ):
                continue
            artifact_path = self._path(name)
            if metadata_path != artifact_path.with_suffix(artifact_path.suffix + ".metadata.json"):
                continue
            selected.append(name)
            if not dry_run:
                try:
                    artifact_path.unlink(missing_ok=True)
                    metadata_path.unlink(missing_ok=True)
                except OSError:
                    failures += 1
                    selected.pop()
        if failures:
            raise RuntimeFailure(
                "cleanup_partial_failure",
                "One or more owned artifacts could not be deleted; other eligible artifacts were processed.",
                {"deleted_count": len(selected), "failure_count": failures},
            )
        return sorted(selected)


class AzureBlobArtifactStore:
    """Private Blob artifact backend using an identity-authenticated client."""

    def __init__(
        self,
        container_client: Any,
        *,
        retention_days: int = 90,
        error_type: type[Exception] = OSError,
    ) -> None:
        self._container = container_client
        self._retention = timedelta(days=retention_days)
        self._error_type = error_type

    @classmethod
    def from_identity(
        cls,
        *,
        account_url: str,
        container: str,
        credential: Any,
        retention_days: int = 90,
    ) -> AzureBlobArtifactStore:
        storage = importlib.import_module("azure.storage.blob")
        exceptions = importlib.import_module("azure.core.exceptions")
        service = storage.BlobServiceClient(account_url=account_url, credential=credential)
        return cls(
            service.get_container_client(container),
            retention_days=retention_days,
            error_type=exceptions.AzureError,
        )

    def put(self, name: str, content: bytes, owner_reference: str) -> ArtifactRecord:
        name = _safe_name(name)
        now = datetime.now(UTC)
        record = ArtifactRecord(name, _reference(content), now, now + self._retention, owner_reference)
        metadata = {
            "purpose": "agent-insights-quality",
            "owner_reference": owner_reference,
            "created_at": now.isoformat(),
            "expires_at": record.expires_at.isoformat(),
            "content_reference": record.reference,
        }
        try:
            self._container.upload_blob(name, content, overwrite=False, metadata=metadata)
        except self._error_type as error:
            raise RuntimeFailure("artifact_write_failed", "Blob artifact could not be written.") from error
        return record

    def get(self, name: str) -> bytes:
        name = _safe_name(name)
        try:
            content = self._container.download_blob(name).readall()
            properties = self._container.get_blob_client(name).get_blob_properties()
        except self._error_type as error:
            raise RuntimeFailure("artifact_not_found", "Blob artifact could not be read.") from error
        metadata = getattr(properties, "metadata", None)
        if (
            not isinstance(metadata, Mapping)
            or metadata.get("purpose") != "agent-insights-quality"
            or metadata.get("content_reference") != _reference(content)
        ):
            raise RuntimeFailure("artifact_incomplete", "Blob artifact metadata is incomplete or mismatched.")
        return content

    def cleanup_expired(
        self,
        owner_reference: str,
        *,
        now: datetime | None = None,
        dry_run: bool = True,
    ) -> list[str]:
        threshold = now or datetime.now(UTC)
        selected: list[str] = []
        failures = 0
        try:
            blobs = list(self._container.list_blobs(include=["metadata"]))
        except self._error_type as error:
            raise RuntimeFailure("artifact_cleanup_failed", "Blob artifacts could not be listed.") from error
        for blob in blobs:
            metadata = getattr(blob, "metadata", None)
            if not isinstance(metadata, Mapping):
                continue
            try:
                expires = datetime.fromisoformat(str(metadata.get("expires_at") or ""))
                name = _safe_name(str(blob.name))
            except (ValueError, RuntimeFailure):
                continue
            if (
                metadata.get("purpose") != "agent-insights-quality"
                or metadata.get("owner_reference") != owner_reference
                or not _OPAQUE_REFERENCE.fullmatch(
                    str(metadata.get("content_reference") or "")
                )
                or expires >= threshold
            ):
                continue
            selected.append(name)
            if not dry_run:
                try:
                    self._container.delete_blob(name)
                except self._error_type:
                    failures += 1
                    selected.pop()
        if failures:
            raise RuntimeFailure(
                "cleanup_partial_failure",
                "One or more owned blob artifacts could not be deleted; other eligible artifacts were processed.",
                {"deleted_count": len(selected), "failure_count": failures},
            )
        return sorted(selected)
