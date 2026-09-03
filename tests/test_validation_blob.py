from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceExistsError

from agent_insights_quality.util import ContractError, canonical_bytes
from agent_insights_quality.validation_blob import (
    APPROVED_RECORD_CONTAINER,
    RESOURCE_GROUP,
    AzureValidationBlobStore,
    _normalize_azure_etag,
    approved_record_blob_prefix,
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


def test_approved_record_listing_is_repository_scoped_and_version_bound() -> None:
    repository = "ninghu/agent-insights-quality"
    prefix = approved_record_blob_prefix(repository)
    names = [
        f"{prefix}{'b' * 40}/record.json",
        f"{prefix}{'a' * 40}/record.json",
    ]
    values = {
        name: canonical_bytes({"name": name})
        for name in names
    }
    observed: dict[str, object] = {}

    class Container:
        @staticmethod
        def list_blobs(*, name_starts_with, include):
            observed["prefix"] = name_starts_with
            observed["include"] = include
            return [
                SimpleNamespace(name=name, etag=f"etag-{index}", version_id=f"v-{index}")
                for index, name in enumerate(names)
            ]

    class Client:
        def __init__(self, name, version_id):
            self.name = name
            self.version_id = version_id

        def download_blob(self):
            return SimpleNamespace(readall=lambda: values[self.name])

        def get_blob_properties(self):
            index = names.index(self.name)
            assert self.version_id == f"v-{index}"
            return SimpleNamespace(etag=f"etag-{index}", version_id=f"v-{index}")

    store = object.__new__(AzureValidationBlobStore)
    store._service = SimpleNamespace(
        get_container_client=lambda container: (
            Container()
            if container == APPROVED_RECORD_CONTAINER
            else pytest.fail(container)
        ),
        get_blob_client=lambda container, name, version_id=None: (
            Client(name, version_id)
            if container == APPROVED_RECORD_CONTAINER
            else pytest.fail(container)
        ),
    )

    records = store.list_approved_records(repository)

    assert observed["prefix"] == prefix
    assert observed["include"] == ["versions"]
    assert [record.name for record in records] == sorted(names)
    assert all(record.content == canonical_bytes(record.value) for record in records)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('"0xABCDEF"', "0xabcdef"),
        ("'0xABCDEF'", "0xabcdef"),
        ("0xABCDEF", "0xabcdef"),
        ('W/"0xABCDEF"', "0xabcdef"),
        ("w/'0xABCDEF'", "0xabcdef"),
        (' \tW/"0xABCDEF"\r\n', "0xabcdef"),
    ],
)
def test_azure_etag_normalization(value, expected) -> None:
    assert _normalize_azure_etag(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        " \t\r\n",
        "W/",
        "w/''",
        '"unterminated',
        "'mismatched\"",
        'W/ "spaced"',
        '"internal space"',
        'W/W/"repeated"',
    ],
)
def test_azure_etag_normalization_rejects_empty_or_malformed_values(
    value,
) -> None:
    assert _normalize_azure_etag(value) is None


def _approved_record_listing_store(
    *,
    listed_etag: object,
    read_etag: object,
    listed_version_id: str = "version",
    read_version_id: str = "version",
) -> AzureValidationBlobStore:
    repository = "ninghu/agent-insights-quality"
    name = f"{approved_record_blob_prefix(repository)}{'b' * 40}/record.json"
    content = canonical_bytes({"name": name})

    def get_blob_client(container, requested_name, version_id=None):
        assert container == APPROVED_RECORD_CONTAINER
        assert requested_name == name
        assert version_id == listed_version_id
        return SimpleNamespace(
            download_blob=lambda: SimpleNamespace(readall=lambda: content),
            get_blob_properties=lambda: SimpleNamespace(
                etag=read_etag,
                version_id=read_version_id,
            ),
        )

    store = object.__new__(AzureValidationBlobStore)
    store._service = SimpleNamespace(
        get_container_client=lambda container: (
            SimpleNamespace(
                list_blobs=lambda **_kwargs: [
                    SimpleNamespace(
                        name=name,
                        etag=listed_etag,
                        version_id=listed_version_id,
                    )
                ]
            )
            if container == APPROVED_RECORD_CONTAINER
            else pytest.fail(container)
        ),
        get_blob_client=get_blob_client,
    )
    return store


def test_approved_record_listing_compares_normalized_etags() -> None:
    store = _approved_record_listing_store(
        listed_etag=' \tW/"0xABCDEF"\r\n',
        read_etag="'0xabcdef'",
    )

    records = store.list_approved_records("ninghu/agent-insights-quality")

    assert len(records) == 1
    assert records[0].etag == "'0xabcdef'"
    assert records[0].version_id == "version"


def test_approved_record_listing_still_rejects_version_mismatch() -> None:
    store = _approved_record_listing_store(
        listed_etag='W/"0xABCDEF"',
        read_etag="0xabcdef",
        read_version_id="different-version",
    )

    with pytest.raises(ContractError, match="metadata changed during read"):
        store.list_approved_records("ninghu/agent-insights-quality")


@pytest.mark.parametrize(
    "listed",
    [
        SimpleNamespace(
            name=(
                "approved-validation-records/ninghu/agent-insights-quality/"
                + ("b" * 40)
                + "/unexpected.json"
            ),
            etag="etag",
            version_id="version",
        ),
        SimpleNamespace(
            name=(
                "approved-validation-records/ninghu/agent-insights-quality/"
                + ("b" * 40)
                + "/record.json"
            ),
            etag="",
            version_id="version",
        ),
        SimpleNamespace(
            name=(
                "approved-validation-records/ninghu/agent-insights-quality/"
                + ("b" * 40)
                + "/record.json"
            ),
            etag="etag",
            version_id=None,
        ),
        SimpleNamespace(
            name=(
                "approved-validation-records/ninghu/agent-insights-quality/"
                + ("b" * 40)
                + "/record.json"
            ),
            etag='W/"unterminated',
            version_id="version",
        ),
    ],
)
def test_approved_record_listing_rejects_noncanonical_or_missing_metadata(
    listed,
) -> None:
    store = object.__new__(AzureValidationBlobStore)
    store._service = SimpleNamespace(
        get_container_client=lambda _container: SimpleNamespace(
            list_blobs=lambda **_kwargs: [listed]
        )
    )

    with pytest.raises(ContractError, match="listing metadata is invalid"):
        store.list_approved_records("ninghu/agent-insights-quality")
