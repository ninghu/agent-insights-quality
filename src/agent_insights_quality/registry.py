from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any, Protocol

from agent_insights_quality.contracts import Deployment, Environment
from agent_insights_quality.errors import QualityError
from agent_insights_quality.state import RecordStore


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
            )
        return self._client

    async def read(self) -> tuple[bytes, str] | None:
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

        try:
            download = await self._get_client().download_blob()
            return await download.readall(), download.properties.etag
        except ResourceNotFoundError:
            return None
        except HttpResponseError as error:
            raise QualityError("registry_read_failed", status=error.status_code) from error

    async def write(self, data: bytes, etag: str | None) -> str:
        from azure.core import MatchConditions
        from azure.core.exceptions import HttpResponseError

        options: dict[str, Any] = {"overwrite": etag is not None}
        if etag is not None:
            options.update(etag=etag, match_condition=MatchConditions.IfNotModified)
        try:
            result = await self._get_client().upload_blob(data, **options)
            return str(result["etag"])
        except HttpResponseError as error:
            raise QualityError("registry_write_failed", status=error.status_code) from error

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
        if self._credential is not None:
            await self._credential.close()


def _decode(data: bytes) -> dict[str, Deployment]:
    try:
        document = json.loads(data)
        if set(document) != {"schema_version", "targets"} or document["schema_version"] != "1.0":
            raise ValueError("Wrong registry format")
        if not isinstance(document["targets"], dict):
            raise ValueError("Wrong registry entries")
        result = {}
        for key, value in document["targets"].items():
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
            result[key] = record
        return result
    except (TypeError, ValueError, KeyError, UnicodeError) as error:
        raise QualityError("registry_format_invalid") from error


class DeploymentRegistry:
    """One current-format private blob, with a validated local cache."""

    def __init__(self, blob: BlobPort, cache: RecordStore) -> None:
        self.blob = blob
        self.cache = cache
        self.records: dict[str, Deployment] = {}
        self.etag: str | None = None
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        value = await self.blob.read()
        records = _decode(value[0]) if value is not None else {}
        self.records = records
        self.etag = value[1] if value is not None else None
        self.cache.save_progress("deployment-registry", {
            "schema_version": "1.0",
            "targets": {key: asdict(item) for key, item in records.items()},
        })

    def get(self, key: str) -> Deployment | None:
        return self.records.get(key)

    async def save(self, deployment: Deployment) -> None:
        async with self._lock:
            updated = {**self.records, deployment.target_key: deployment}
            document = {
                "schema_version": "1.0",
                "targets": {key: asdict(item) for key, item in updated.items()},
            }
            payload = json.dumps(document, sort_keys=True, allow_nan=False).encode("utf-8")
            _decode(payload)
            etag = await self.blob.write(payload, self.etag)
            self.records = updated
            self.etag = etag
            self.cache.save_progress("deployment-registry", document)
