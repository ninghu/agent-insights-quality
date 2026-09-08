from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any, Protocol

from agent_insights_quality.contracts import Deployment, Environment
from agent_insights_quality.errors import QualityError
from agent_insights_quality.state import RecordStore, _unique_object


class BlobPort(Protocol):
    async def read(self) -> tuple[bytes, str] | None: ...
    async def write(self, data: bytes, etag: str | None) -> str: ...


class AzureRegistryBlob:
    def __init__(self, environment: Environment) -> None:
        self.environment = environment
        self._client = None
        self._credential = None

    def _get_client(self):
        if self._client is None:
            from azure.identity.aio import AzureCliCredential
            from azure.storage.blob.aio import BlobClient

            self._credential = AzureCliCredential()
            self._client = BlobClient(
                account_url=(
                    f"https://{self.environment.storage_account_name}.blob.core.windows.net"
                ),
                container_name="deployment-registries",
                blob_name=f"swedencentral-g30/runner-v1/{self.environment.profile}.json",
                credential=self._credential,
                retry_total=0,
            )
        return self._client

    async def read(self) -> tuple[bytes, str] | None:
        from azure.core.exceptions import (
            AzureError, HttpResponseError, ResourceNotFoundError,
        )

        try:
            download = await self._get_client().download_blob()
            data = await download.readall()
            return data, _etag(download.properties.etag)
        except ResourceNotFoundError as error:
            code = getattr(error.error_code, "value", error.error_code)
            if code == "BlobNotFound":
                return None
            raise QualityError("registry_resource_missing", status=error.status_code) from None
        except HttpResponseError as error:
            raise QualityError("registry_read_failed", status=error.status_code) from None
        except (AzureError, OSError):
            raise QualityError("registry_read_failed") from None

    async def write(self, data: bytes, etag: str | None) -> str:
        from azure.core import MatchConditions
        from azure.core.exceptions import AzureError, HttpResponseError

        if etag is not None:
            _etag(etag)
        options: dict[str, Any] = {"overwrite": etag is not None}
        if etag is not None:
            options.update(etag=etag, match_condition=MatchConditions.IfNotModified)
        try:
            result = await self._get_client().upload_blob(data, **options)
            return _etag(result.get("etag"))
        except HttpResponseError as error:
            status = error.status_code
            rejected = status is not None and 400 <= status < 500 and status != 408
            raise QualityError(
                "registry_write_failed", status=status,
                request_accepted=False if rejected else None,
            ) from None
        except (AzureError, OSError):
            raise QualityError("registry_write_failed", request_accepted=None) from None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
        if self._credential is not None:
            await self._credential.close()


def _etag(value: Any) -> str:
    if not isinstance(value, str) or not value or value == "*":
        raise QualityError("registry_etag_invalid")
    return value


def _decode(data: bytes) -> dict[str, Deployment]:
    try:
        document = json.loads(data, object_pairs_hook=_unique_object)
        json.dumps(document, allow_nan=False)
        if set(document) != {"schema_version", "targets"} or document["schema_version"] != "1.0":
            raise ValueError("Wrong registry format")
        if not isinstance(document["targets"], dict):
            raise ValueError("Wrong registry entries")
        result = {}
        for key, value in document["targets"].items():
            if not isinstance(value, dict):
                raise ValueError("Invalid registry entry")
            value = dict(value)
            details = value.get("details")
            if isinstance(details, Mapping) and "content_hash" in details:
                content_hash = details["content_hash"]
                if "content_hash" in value and value["content_hash"] != content_hash:
                    raise ValueError("Conflicting registry content identity")
                value["content_hash"] = content_hash
                value["details"] = {
                    name: item for name, item in details.items() if name != "content_hash"
                }
            record = Deployment(**value)
            if (
                key != record.target_key
                or not all(isinstance(part, str) and part for part in (
                    key, record.agent_name, record.agent_type, record.source_revision,
                ))
                or not isinstance(record.provider_version, str)
                or not isinstance(record.details, Mapping)
                or (
                    record.details.get("provisioning_state") == "active"
                    and not record.provider_version
                )
            ):
                raise ValueError("Invalid registry entry")
            if record.content_hash is not None:
                metadata = record.details.get("metadata")
                if not isinstance(metadata, Mapping) or (
                    metadata.get("aiq_content_hash") != record.content_hash
                    or metadata.get("aiq_source_revision") != record.source_revision
                    or metadata.get("aiq_agent_type") != record.agent_type
                ):
                    raise ValueError("Invalid registry content identity")
            result[key] = record
        return result
    except (TypeError, ValueError, KeyError, UnicodeError):
        raise QualityError("registry_format_invalid") from None


def _document(records: Mapping[str, Deployment]) -> dict[str, Any]:
    targets = {}
    for key, item in records.items():
        if not isinstance(item.details, Mapping):
            raise ValueError("Invalid registry details")
        value = asdict(item)
        content_hash = value.pop("content_hash")
        details = dict(value["details"])
        if "content_hash" in details and details["content_hash"] != content_hash:
            raise ValueError("Conflicting registry content identity")
        if content_hash is not None:
            # Old source readers construct Deployment(**record); only details is extensible.
            details["content_hash"] = content_hash
        value["details"] = details
        targets[key] = value
    return {"schema_version": "1.0", "targets": targets}


class DeploymentRegistry:
    """One current-format private blob, with a validated local cache."""

    def __init__(self, blob: BlobPort, cache: RecordStore) -> None:
        self.blob = blob
        self.cache = cache
        self.records: dict[str, Deployment] = {}
        self.etag: str | None = None
        self._lock = asyncio.Lock()
        self._pending: dict[str, Deployment] | None = None
        self._loaded = False

    async def load(self) -> None:
        async with self._lock:
            records, etag = await self._read()
            self._accept(records, etag)

    async def _read(self) -> tuple[dict[str, Deployment], str | None]:
        try:
            value = await self.blob.read()
        except OSError:
            raise QualityError("registry_read_failed") from None
        if value is None:
            return {}, None
        records = _decode(value[0])
        return records, _etag(value[1])

    def _accept(self, records: dict[str, Deployment], etag: str | None) -> None:
        self.cache.save_progress("deployment-registry", _document(records))
        self.records = records
        self.etag = etag
        self._pending = None
        self._loaded = True

    async def _reconcile(self, *, conflict: bool = False) -> None:
        records, etag = await self._read()
        if records != self._pending or etag is None:
            raise QualityError(
                "registry_write_conflict" if conflict else "registry_write_unresolved",
                request_accepted=False if conflict else None,
            )
        self._accept(records, etag)

    def get(self, key: str) -> Deployment | None:
        return self.records.get(key)

    async def save(self, deployment: Deployment) -> None:
        async with self._lock:
            if not self._loaded:
                raise QualityError("registry_not_loaded", request_accepted=False)
            if self._pending is not None:
                await self._reconcile()
            updated = {**self.records, deployment.target_key: deployment}
            try:
                payload = json.dumps(_document(updated), sort_keys=True, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError):
                raise QualityError("registry_format_invalid", request_accepted=False) from None
            updated = _decode(payload)
            if self.records == updated:
                return
            self._pending = updated
            try:
                etag = _etag(await self.blob.write(payload, self.etag))
            except (QualityError, OSError) as error:
                conflict = isinstance(error, QualityError) and error.status in {409, 412}
                if isinstance(error, QualityError) and error.request_accepted is False and not conflict:
                    self._pending = None
                    raise
                # Conditional writes can have committed even when their reply was lost.
                # Read the exact result before retrying or letting another lane write.
                try:
                    await self._reconcile(conflict=conflict)
                except QualityError as reconciliation:
                    raise reconciliation from None
                return
            self._accept(updated, etag)
