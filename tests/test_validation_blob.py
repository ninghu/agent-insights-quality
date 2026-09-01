from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceExistsError

from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_blob import (
    APPROVED_RECORD_CONTAINER,
    RESOURCE_GROUP,
    AzureValidationBlobStore,
)


def test_blob_store_uses_only_injected_credential(monkeypatch) -> None:
    observed = {}

    class Service:
        def __init__(self, *, account_url, credential):
            observed["account_url"] = account_url
            observed["credential"] = credential

    monkeypatch.setattr("azure.storage.blob.BlobServiceClient", Service)
    credential = object()
    store = AzureValidationBlobStore(
        "aiqsweartsynthetic",
        credential=credential,
    )
    assert store._service is not None
    assert observed == {
        "account_url": "https://aiqsweartsynthetic.blob.core.windows.net",
        "credential": credential,
    }
    with pytest.raises(ContractError, match="Explicit"):
        AzureValidationBlobStore("aiqsweartsynthetic", credential=None)
    with pytest.raises(ContractError, match="reviewed Sweden environment"):
        AzureValidationBlobStore("aiqartifactslegacy", credential=credential)


def test_blob_contract_checks_private_locked_exact_worm(monkeypatch) -> None:
    observed = []
    calls = []

    class Service:
        @staticmethod
        def get_container_client(container):
            observed.append(container)
            return SimpleNamespace(
                get_container_properties=lambda: {
                    "public_access": None,
                    "has_immutability_policy": True,
                    "immutable_storage_with_versioning_enabled": True,
                }
            )

    store = object.__new__(AzureValidationBlobStore)
    store._storage_account_name = "aiqsweartsynthetic"
    store._service = Service()
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="true\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="false\n"),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "state": "Locked",
                        "immutabilityPeriodSinceCreationInDays": 90,
                        "allowProtectedAppendWrites": False,
                    }
                ),
            ),
        ]
    )
    def run(arguments, **_kwargs):
        calls.append(arguments)
        return next(responses)

    monkeypatch.setattr(
        "agent_insights_quality.validation_blob.subprocess.run",
        run,
    )
    store.assert_approved_record_contract(APPROVED_RECORD_CONTAINER)
    assert observed == [APPROVED_RECORD_CONTAINER]
    versioning_call = next(
        arguments
        for arguments in calls
        if "blob-service-properties" in arguments
    )
    assert versioning_call[versioning_call.index("--account-name") + 1] == (
        "aiqsweartsynthetic"
    )
    assert versioning_call[versioning_call.index("--resource-group") + 1] == (
        RESOURCE_GROUP
    )
    assert versioning_call[versioning_call.index("--query") + 1] == (
        "isVersioningEnabled"
    )
    assert "--auth-mode" not in next(
        arguments
        for arguments in calls
        if "immutability-policy" in arguments
    )


def test_blob_contract_rejects_public_or_unlocked_storage(monkeypatch) -> None:
    store = object.__new__(AzureValidationBlobStore)
    store._storage_account_name = "aiqsweartsynthetic"
    store._service = SimpleNamespace(
        get_container_client=lambda _container: SimpleNamespace(
            get_container_properties=lambda: {
                "public_access": None,
                "has_immutability_policy": True,
                "immutable_storage_with_versioning_enabled": True,
            }
        ),
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_blob.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="true\n"),
    )
    with pytest.raises(ContractError, match="anonymous Blob access"):
        store.assert_approved_record_contract(APPROVED_RECORD_CONTAINER)

    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="true\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="false\n"),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "state": "Unlocked",
                        "immutabilityPeriodSinceCreationInDays": 90,
                        "allowProtectedAppendWrites": False,
                    }
                ),
            ),
        ]
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_blob.subprocess.run",
        lambda *_args, **_kwargs: next(responses),
    )
    with pytest.raises(ContractError, match="locked 90-day"):
        store.assert_approved_record_contract(APPROVED_RECORD_CONTAINER)


def test_blob_contract_rejects_empty_success_policy_response(monkeypatch) -> None:
    store = object.__new__(AzureValidationBlobStore)
    store._storage_account_name = "aiqsweartsynthetic"
    store._service = SimpleNamespace(
        get_container_client=lambda _container: SimpleNamespace(
            get_container_properties=lambda: {
                "public_access": None,
                "has_immutability_policy": True,
                "immutable_storage_with_versioning_enabled": True,
            }
        ),
    )
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="true\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="false\n"),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_blob.subprocess.run",
        lambda *_args, **_kwargs: next(responses),
    )
    with pytest.raises(ContractError, match="response is invalid"):
        store.assert_approved_record_contract(APPROVED_RECORD_CONTAINER)


def test_blob_contract_rejects_valid_policy_with_stderr(monkeypatch) -> None:
    store = object.__new__(AzureValidationBlobStore)
    store._storage_account_name = "aiqsweartsynthetic"
    store._service = SimpleNamespace(
        get_container_client=lambda _container: SimpleNamespace(
            get_container_properties=lambda: {
                "public_access": None,
                "has_immutability_policy": True,
                "immutable_storage_with_versioning_enabled": True,
            }
        ),
    )
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="true\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="false\n"),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "state": "Locked",
                        "immutabilityPeriodSinceCreationInDays": 90,
                        "allowProtectedAppendWrites": False,
                    }
                ),
                stderr="WARNING: unexpected diagnostic\n",
            ),
        ]
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_blob.subprocess.run",
        lambda *_args, **_kwargs: next(responses),
    )
    with pytest.raises(ContractError, match="response is invalid"):
        store.assert_approved_record_contract(APPROVED_RECORD_CONTAINER)


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (1, "true\n", ""),
        (0, "false\n", ""),
        (0, "", ""),
        (0, "null\n", ""),
        (0, "not-json\n", ""),
        (0, "\n", ""),
        (0, " true\n", ""),
        (0, "true\n", "WARNING: unexpected diagnostic\n"),
        (0, "true\n", " "),
    ],
)
def test_blob_contract_rejects_invalid_versioning_response(
    monkeypatch,
    returncode,
    stdout,
    stderr,
) -> None:
    store = object.__new__(AzureValidationBlobStore)
    store._storage_account_name = "aiqsweartsynthetic"
    store._service = SimpleNamespace()
    calls = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        return SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    monkeypatch.setattr(
        "agent_insights_quality.validation_blob.subprocess.run",
        run,
    )
    with pytest.raises(ContractError, match="Blob versioning is not enabled"):
        store.assert_approved_record_contract(APPROVED_RECORD_CONTAINER)
    assert len(calls) == 1
    assert "blob-service-properties" in calls[0]


@pytest.mark.parametrize(
    "error",
    [
        OSError("synthetic process failure"),
        subprocess.TimeoutExpired("az", 120),
    ],
)
def test_blob_contract_translates_versioning_process_failures(
    monkeypatch,
    error,
) -> None:
    store = object.__new__(AzureValidationBlobStore)
    store._storage_account_name = "aiqsweartsynthetic"
    store._service = SimpleNamespace()

    def run(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(
        "agent_insights_quality.validation_blob.subprocess.run",
        run,
    )
    with pytest.raises(
        ContractError,
        match="Blob versioning cannot be read",
    ) as captured:
        store.assert_approved_record_contract(APPROVED_RECORD_CONTAINER)
    assert captured.value.__cause__ is error


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
    store._storage_account_name = "aiqsweartsynthetic"
    store._service = SimpleNamespace(
        get_blob_client=lambda *_args, **_kwargs: client
    )
    value = {"kind": "test-agent-validation-approved-record"}
    first = store.create_once(APPROVED_RECORD_CONTAINER, "record", value)
    second = store.create_once(APPROVED_RECORD_CONTAINER, "record", value)
    assert first.value == second.value == value
    with pytest.raises(ContractError, match="different content"):
        store.create_once(
            APPROVED_RECORD_CONTAINER,
            "record",
            {"kind": "different"},
        )


def test_semantically_equal_but_byte_different_blob_is_not_idempotent() -> None:
    class Client:
        value = b'{ "kind": "test-agent-validation-approved-record" }'

        def upload_blob(self, _value, **_kwargs):
            raise ResourceExistsError("already exists")

        def download_blob(self):
            return SimpleNamespace(readall=lambda: self.value)

        @staticmethod
        def get_blob_properties():
            return SimpleNamespace(etag="etag", version_id="version")

    store = object.__new__(AzureValidationBlobStore)
    store._storage_account_name = "aiqsweartsynthetic"
    store._service = SimpleNamespace(
        get_blob_client=lambda *_args, **_kwargs: Client()
    )
    with pytest.raises(ContractError, match="different content"):
        store.create_once(
            APPROVED_RECORD_CONTAINER,
            "record",
            {"kind": "test-agent-validation-approved-record"},
        )
