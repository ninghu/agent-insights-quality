from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_insights_quality.util import ContractError, canonical_bytes


@dataclass(frozen=True)
class BlobRecord:
    container: str
    name: str
    value: dict[str, Any]
    etag: str
    version_id: str


class AzureValidationBlobStore:
    def __init__(self, storage_account_name: str) -> None:
        if not storage_account_name:
            raise ContractError("Validation storage account name is required")
        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient
        except ImportError as error:
            raise ContractError(
                "Validation Blob operations require the azure optional dependencies"
            ) from error
        self._service = BlobServiceClient(
            account_url=f"https://{storage_account_name}.blob.core.windows.net",
            credential=DefaultAzureCredential(),
        )

    def read(
        self,
        container: str,
        name: str,
        *,
        lease_id: str | None = None,
        version_id: str | None = None,
    ) -> BlobRecord:
        client = self._service.get_blob_client(
            container,
            name,
            version_id=version_id,
        )
        downloader = client.download_blob(lease=lease_id)
        value = downloader.readall()
        properties = client.get_blob_properties(lease=lease_id)
        return BlobRecord(
            container=container,
            name=name,
            value=_decode_object(value, f"{container}/{name}"),
            etag=str(properties.etag),
            version_id=str(properties.version_id or ""),
        )

    def read_optional(
        self,
        container: str,
        name: str,
        *,
        lease_id: str | None = None,
        version_id: str | None = None,
    ) -> BlobRecord | None:
        try:
            from azure.core.exceptions import ResourceNotFoundError
        except ImportError as error:
            raise ContractError(
                "Validation Blob operations require the azure optional dependencies"
            ) from error
        try:
            return self.read(
                container,
                name,
                lease_id=lease_id,
                version_id=version_id,
            )
        except ResourceNotFoundError:
            return None

    def create_once(
        self,
        container: str,
        name: str,
        value: dict[str, Any],
    ) -> BlobRecord:
        try:
            from azure.core.exceptions import ResourceExistsError
            from azure.storage.blob import ContentSettings
        except ImportError as error:
            raise ContractError(
                "Validation Blob operations require the azure optional dependencies"
            ) from error
        client = self._service.get_blob_client(container, name)
        rendered = canonical_bytes(value)
        try:
            client.upload_blob(
                rendered,
                overwrite=False,
                content_settings=ContentSettings(
                    content_type="application/json"
                ),
            )
        except ResourceExistsError:
            existing = self.read(container, name)
            if canonical_bytes(existing.value) != rendered:
                raise ContractError(
                    f"Immutable Blob {container}/{name} already has different content"
                ) from None
            return existing
        return self.read(container, name)

    def compare_and_swap(
        self,
        container: str,
        name: str,
        value: dict[str, Any],
        *,
        lease_id: str,
        etag: str,
    ) -> BlobRecord:
        try:
            from azure.core import MatchConditions
            from azure.core.exceptions import (
                HttpResponseError,
                ResourceModifiedError,
            )
            from azure.storage.blob import ContentSettings
        except ImportError as error:
            raise ContractError(
                "Validation Blob operations require the azure optional dependencies"
            ) from error
        client = self._service.get_blob_client(container, name)
        try:
            client.upload_blob(
                canonical_bytes(value),
                overwrite=True,
                content_settings=ContentSettings(
                    content_type="application/json"
                ),
                lease=lease_id,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except (ResourceModifiedError, HttpResponseError) as error:
            status_code = getattr(error, "status_code", None)
            if isinstance(error, ResourceModifiedError) or status_code in {
                409,
                412,
            }:
                raise ContractError(
                    "Validation journal lease or ETag ownership was lost"
                ) from error
            raise
        return self.read(container, name, lease_id=lease_id)

    def acquire_infinite_lease(
        self,
        container: str,
        name: str,
        *,
        proposed_lease_id: str | None = None,
    ) -> str:
        try:
            from azure.storage.blob import BlobLeaseClient
        except ImportError as error:
            raise ContractError(
                "Validation Blob operations require the azure optional dependencies"
            ) from error
        lease = BlobLeaseClient(
            self._service.get_blob_client(container, name),
            lease_id=proposed_lease_id,
        )
        lease.acquire(lease_duration=-1)
        if not lease.id:
            raise ContractError("Validation journal lease acquisition returned no ID")
        return str(lease.id)

    def break_lease(self, container: str, name: str) -> None:
        try:
            from azure.storage.blob import BlobLeaseClient
        except ImportError as error:
            raise ContractError(
                "Validation Blob operations require the azure optional dependencies"
            ) from error
        BlobLeaseClient(
            self._service.get_blob_client(container, name)
        ).break_lease()

    def release_lease(
        self,
        container: str,
        name: str,
        *,
        lease_id: str,
    ) -> None:
        try:
            from azure.storage.blob import BlobLeaseClient
        except ImportError as error:
            raise ContractError(
                "Validation Blob operations require the azure optional dependencies"
            ) from error
        BlobLeaseClient(
            self._service.get_blob_client(container, name),
            lease_id=lease_id,
        ).release()


def _decode_object(value: bytes, label: str) -> dict[str, Any]:
    import json

    try:
        decoded = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"Validation Blob {label} is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise ContractError(f"Validation Blob {label} must contain an object")
    return decoded
