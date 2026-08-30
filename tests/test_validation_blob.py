from __future__ import annotations

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
