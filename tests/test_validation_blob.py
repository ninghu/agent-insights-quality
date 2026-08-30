from __future__ import annotations

from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceExistsError

from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_blob import AzureValidationBlobStore


def test_blob_store_uses_only_injected_credential(monkeypatch) -> None:
    observed = {}

    class Service:
        def __init__(self, *, account_url, credential):
            observed["account_url"] = account_url
            observed["credential"] = credential

    monkeypatch.setattr("azure.storage.blob.BlobServiceClient", Service)
    credential = object()
    store = AzureValidationBlobStore(
        "syntheticstorage",
        credential=credential,
    )
    assert store._service is not None
    assert observed == {
        "account_url": "https://syntheticstorage.blob.core.windows.net",
        "credential": credential,
    }
    with pytest.raises(ContractError, match="Explicit"):
        AzureValidationBlobStore("syntheticstorage", credential=None)


def test_blob_contract_checks_only_approved_record_container() -> None:
    observed = []

    class Service:
        @staticmethod
        def get_service_properties():
            return {"is_versioning_enabled": True}

        @staticmethod
        def get_container_client(container):
            observed.append(container)
            return SimpleNamespace(
                get_container_properties=lambda: {
                    "has_immutability_policy": True,
                    "immutable_storage_with_versioning_enabled": True,
                }
            )

    store = object.__new__(AzureValidationBlobStore)
    store._service = Service()
    store.assert_approved_record_contract(
        "test-agent-validation-approved-records"
    )
    assert observed == ["test-agent-validation-approved-records"]


def test_approved_record_blob_is_create_once_and_idempotent() -> None:
    class Client:
        value = None

        def upload_blob(self, value, **_kwargs):
            if self.value is not None:
                raise ResourceExistsError("already exists")
            self.value = value

        def download_blob(self):
            return SimpleNamespace(readall=lambda: self.value)

        @staticmethod
        def get_blob_properties():
            return SimpleNamespace(etag="etag", version_id="version")

    client = Client()
    store = object.__new__(AzureValidationBlobStore)
    store._service = SimpleNamespace(
        get_blob_client=lambda *_args, **_kwargs: client
    )
    value = {"kind": "test-agent-validation-approved-record"}
    first = store.create_once("test-agent-validation-approved-records", "record", value)
    second = store.create_once("test-agent-validation-approved-records", "record", value)
    assert first.value == second.value == value
    with pytest.raises(ContractError, match="different content"):
        store.create_once(
            "test-agent-validation-approved-records",
            "record",
            {"kind": "different"},
        )
