from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import json
import subprocess
from typing import Any

from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.util import ContractError, canonical_bytes


@dataclass(frozen=True)
class BlobRecord:
    container: str
    name: str
    value: dict[str, Any]
    etag: str
    version_id: str
    content: bytes


class AzureValidationBlobStore:
    def __init__(self, storage_account_name: str, *, credential: Any) -> None:
        if not storage_account_name:
            raise ContractError("Validation storage account name is required")
        if credential is None:
            raise ContractError("Explicit Validation Blob credential is required")
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError as error:
            raise ContractError(
                "Validation Blob operations require the azure optional dependencies"
            ) from error
        self._storage_account_name = storage_account_name
        self._service = BlobServiceClient(
            account_url=f"https://{storage_account_name}.blob.core.windows.net",
            credential=credential,
        )

    def assert_approved_record_contract(
        self,
        container: str,
    ) -> None:
        try:
            from azure.core.exceptions import AzureError
        except ImportError as error:
            raise ContractError(
                "Approved record upload requires the azure optional dependencies"
            ) from error
        try:
            service = self._service.get_service_properties()
        except (AzureError, OSError) as error:
            raise ContractError(
                "Approved record storage properties cannot be read"
            ) from error
        versioning = (
            service.get("is_versioning_enabled")
            if isinstance(service, Mapping)
            else getattr(service, "is_versioning_enabled", None)
        )
        if versioning is not True:
            raise ContractError(
                "Approved record Blob versioning is not enabled"
            )
        if container != "test-agent-validation-approved-records":
            raise ContractError("Approved record container is not reviewed")
        account = subprocess.run(
            [
                azure_cli(),
                "storage",
                "account",
                "show",
                "--name",
                self._storage_account_name,
                "--query",
                "allowBlobPublicAccess",
                "--output",
                "tsv",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if account.returncode != 0 or account.stdout.strip().casefold() != "false":
            raise ContractError(
                "Approved record storage account permits anonymous Blob access"
            )
        try:
            container_client = self._service.get_container_client(container)
            properties = container_client.get_container_properties()
        except (AzureError, OSError) as error:
            raise ContractError("Approved record container is unavailable") from error
        has_policy = (
            properties.get("has_immutability_policy")
            if isinstance(properties, Mapping)
            else getattr(properties, "has_immutability_policy", None)
        )
        immutable_versioning = (
            properties.get("immutable_storage_with_versioning_enabled")
            if isinstance(properties, Mapping)
            else getattr(
                properties,
                "immutable_storage_with_versioning_enabled",
                None,
            )
        )
        public_access = (
            properties.get("public_access")
            if isinstance(properties, Mapping)
            else getattr(properties, "public_access", None)
        )
        if (
            public_access not in {None, "None"}
            or has_policy is not True
            or immutable_versioning is not True
        ):
            raise ContractError("Approved record immutable storage is incomplete")
        policy = subprocess.run(
            [
                azure_cli(),
                "storage",
                "container",
                "immutability-policy",
                "show",
                "--account-name",
                self._storage_account_name,
                "--container-name",
                container,
                "--auth-mode",
                "login",
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        try:
            policy_value = json.loads(policy.stdout)
        except json.JSONDecodeError as error:
            raise ContractError(
                "Approved record immutability policy response is invalid"
            ) from error
        if (
            policy.returncode != 0
            or not isinstance(policy_value, Mapping)
            or str(policy_value.get("state") or "") != "Locked"
            or policy_value.get("immutabilityPeriodSinceCreationInDays") != 90
            or policy_value.get("allowProtectedAppendWrites") is not False
        ):
            raise ContractError(
                "Approved record container lacks the exact locked 90-day WORM policy"
            )

    def read(
        self,
        container: str,
        name: str,
        *,
        version_id: str | None = None,
    ) -> BlobRecord:
        client = self._service.get_blob_client(
            container,
            name,
            version_id=version_id,
        )
        downloader = client.download_blob()
        value = downloader.readall()
        properties = client.get_blob_properties()
        return BlobRecord(
            container=container,
            name=name,
            value=_decode_object(value, f"{container}/{name}"),
            etag=str(properties.etag),
            version_id=str(properties.version_id or ""),
            content=value,
        )

    def read_optional(
        self,
        container: str,
        name: str,
        *,
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
            if existing.content != rendered:
                raise ContractError(
                    f"Immutable Blob {container}/{name} already has different content"
                ) from None
            return existing
        created = self.read(container, name)
        if created.content != rendered:
            raise ContractError("Approved record Blob bytes changed after upload")
        return created

def _decode_object(value: bytes, label: str) -> dict[str, Any]:
    import json

    try:
        decoded = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"Validation Blob {label} is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise ContractError(f"Validation Blob {label} must contain an object")
    return decoded
