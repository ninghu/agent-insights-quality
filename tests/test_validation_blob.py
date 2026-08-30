from __future__ import annotations

import pytest

from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_blob import AzureValidationBlobStore


def test_proposed_lease_id_is_supplied_only_to_lease_client(monkeypatch) -> None:
    observed = {}

    class BlobClient:
        pass

    class Service:
        def get_blob_client(self, container, name):
            observed["blob"] = (container, name)
            return BlobClient()

    class Lease:
        def __init__(self, client, *, lease_id=None):
            observed["client"] = client
            observed["lease_id"] = lease_id
            self.id = lease_id

        def acquire(self, **kwargs):
            observed["acquire"] = kwargs

    monkeypatch.setattr("azure.storage.blob.BlobLeaseClient", Lease)
    store = object.__new__(AzureValidationBlobStore)
    store._service = Service()
    assert (
        store.acquire_infinite_lease(
            "test-agent-validation-lifecycle",
            "active.json",
            proposed_lease_id="proposed-lease-id",
        )
        == "proposed-lease-id"
    )
    assert observed["lease_id"] == "proposed-lease-id"
    assert observed["acquire"] == {"lease_duration": -1}


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
