from __future__ import annotations

import hashlib
import importlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .errors import RuntimeFailure


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
        now = datetime.now(UTC)
        record = ArtifactRecord(name, _reference(content), now, now + self._retention, owner_reference)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, path)
        metadata = record.public_dict() | {"name": record.name}
        path.with_suffix(path.suffix + ".metadata.json").write_text(
            json.dumps(metadata, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return record

    def get(self, name: str) -> bytes:
        try:
            return self._path(name).read_bytes()
        except OSError as error:
            raise RuntimeFailure("artifact_not_found", "Artifact could not be read.") from error

    def cleanup_expired(
        self,
        owner_reference: str,
        *,
        now: datetime | None = None,
        dry_run: bool = True,
    ) -> list[str]:
        selected: list[str] = []
        threshold = now or datetime.now(UTC)
        if not self._root.exists():
            return selected
        for metadata_path in self._root.rglob("*.metadata.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                expires = datetime.fromisoformat(str(metadata["expires_at"]))
                name = _safe_name(str(metadata["name"]))
            except (OSError, KeyError, ValueError, json.JSONDecodeError, RuntimeFailure):
                continue
            if metadata.get("owner_reference") != owner_reference or expires >= threshold:
                continue
            artifact_path = self._path(name)
            if metadata_path != artifact_path.with_suffix(artifact_path.suffix + ".metadata.json"):
                continue
            selected.append(name)
            if not dry_run:
                artifact_path.unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
        return sorted(selected)


class AzureBlobArtifactStore:
    """Private Blob artifact backend using an identity-authenticated client."""

    def __init__(self, container_client: Any, *, retention_days: int = 90) -> None:
        self._container = container_client
        self._retention = timedelta(days=retention_days)

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
        service = storage.BlobServiceClient(account_url=account_url, credential=credential)
        return cls(service.get_container_client(container), retention_days=retention_days)

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
        self._container.upload_blob(name, content, overwrite=False, metadata=metadata)
        return record

    def get(self, name: str) -> bytes:
        return self._container.download_blob(_safe_name(name)).readall()

    def cleanup_expired(
        self,
        owner_reference: str,
        *,
        now: datetime | None = None,
        dry_run: bool = True,
    ) -> list[str]:
        threshold = now or datetime.now(UTC)
        selected: list[str] = []
        for blob in self._container.list_blobs(include=["metadata"]):
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
                or expires >= threshold
            ):
                continue
            selected.append(name)
            if not dry_run:
                self._container.delete_blob(name)
        return sorted(selected)
