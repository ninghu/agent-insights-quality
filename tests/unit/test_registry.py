import asyncio
import json
import sys
from dataclasses import asdict, make_dataclass, replace
from types import ModuleType, SimpleNamespace

import pytest

from agent_insights_quality.contracts import Deployment, Environment
from agent_insights_quality.errors import QualityError
from agent_insights_quality.registry import AzureRegistryBlob, DeploymentRegistry, _decode, _document
from agent_insights_quality.state import RuntimeStore


class Blob:
    def __init__(self, data=None):
        self.data = data
        self.etag = "version-one" if data else None
        self.writes = []

    async def read(self):
        return (self.data, self.etag) if self.data is not None else None

    async def write(self, data, etag):
        assert etag == self.etag
        self.writes.append(data)
        self.data, self.etag = data, "version-two"
        return self.etag


def test_registry_preserves_pending_identity_and_uses_conditional_write(tmp_path):
    store = RuntimeStore("staging", root=tmp_path)
    blob = Blob()
    registry = DeploymentRegistry(blob, store.outbox("registry"))
    target = Deployment(
        "weather-agent/v0", "weather-agent-v0", "", "prompt", "source",
        {"provisioning_state": "pending"},
    )
    with store.ownership():
        asyncio.run(registry.load())
        asyncio.run(registry.save(target))
    assert registry.get(target.target_key) == target
    assert json.loads(blob.data)["targets"][target.target_key]["provider_version"] == ""
    assert registry.etag == "version-two"


def test_invalid_download_never_replaces_a_good_local_cache(tmp_path):
    store = RuntimeStore("daily", root=tmp_path)
    cache = store.outbox("registry")
    previous = {"schema_version": "1.0", "targets": {}}
    with store.ownership():
        cache.save_progress("deployment-registry", previous)
        with pytest.raises(QualityError, match="registry_format_invalid"):
            asyncio.run(DeploymentRegistry(Blob(b'{"wrong":"format"}'), cache).load())
    assert cache.read("deployment-registry") == previous


def test_active_registry_version_cannot_be_missing(tmp_path):
    store = RuntimeStore("daily", root=tmp_path)
    record = {
        "schema_version": "1.0",
        "targets": {
            "weather-agent/v0": {
                "target_key": "weather-agent/v0", "agent_name": "weather-agent",
                "provider_version": "", "agent_type": "prompt",
                "source_revision": "source", "details": {"provisioning_state": "active"},
            },
        },
    }
    with store.ownership(), pytest.raises(QualityError, match="registry_format_invalid"):
        asyncio.run(DeploymentRegistry(
            Blob(json.dumps(record).encode()), store.outbox("registry"),
        ).load())


def deployment(name="weather-agent", version="42"):
    return Deployment(
        name + "/v0", name, version, "prompt", "source-one", {"provisioning_state": "active"},
    )


def content_deployment():
    content_hash = "v1:sha256:" + "a" * 64
    return replace(deployment(), content_hash=content_hash, details={
        "provisioning_state": "active",
        "metadata": {
            "aiq_profile": "daily", "aiq_logical_version": "v0", "aiq_agent_type": "prompt",
            "aiq_source_revision": "source-one", "aiq_content_hash": content_hash,
        },
    })


def test_content_identity_round_trip_preserves_git_provenance_and_legacy_entries(tmp_path):
    legacy = asdict(deployment("healthcare-agent"))
    legacy.pop("content_hash")
    blob = Blob(json.dumps({"schema_version": "1.0", "targets": {
        legacy["target_key"]: legacy,
    }}).encode())
    store = RuntimeStore("daily", root=tmp_path)
    registry = DeploymentRegistry(blob, store.outbox("registry"))
    current = content_deployment()
    with store.ownership():
        asyncio.run(registry.load())
        assert not blob.writes
        asyncio.run(registry.save(current))
        restored = DeploymentRegistry(blob, store.outbox("registry"))
        asyncio.run(restored.load())
    assert restored.get(current.target_key) == current
    assert json.loads(blob.data)["targets"][legacy["target_key"]] == legacy
    assert restored.get(legacy["target_key"]).content_hash is None
    assert {
        key: value for key, value in asdict(restored.get(legacy["target_key"])).items()
        if key != "content_hash"
    } == legacy


def test_canonical_content_record_survives_legacy_constructor_and_writer_round_trip():
    # Constructor contract verified against actual 0ff8dbfd and 222a19bb readers.
    legacy_type = make_dataclass("LegacyDeployment", [
        ("target_key", str), ("agent_name", str), ("provider_version", str),
        ("agent_type", str), ("source_revision", str), ("details", dict),
    ], frozen=True)
    current = content_deployment()
    document = _document({current.target_key: current})
    value = document["targets"][current.target_key]
    assert "content_hash" not in value
    assert value["details"]["content_hash"] == current.content_hash
    legacy = legacy_type(**value)
    assert legacy.source_revision == current.source_revision
    assert legacy.provider_version == current.provider_version
    rewritten = {"schema_version": "1.0", "targets": {legacy.target_key: asdict(legacy)}}
    assert rewritten == document
    assert _decode(json.dumps(rewritten).encode())[current.target_key] == current
    assert "content_hash" not in current.details


def test_draft_top_level_hash_is_read_without_rewriting_canonical_blob(tmp_path):
    current = content_deployment()
    draft = {"schema_version": "1.0", "targets": {current.target_key: asdict(current)}}
    original = json.dumps(draft).encode()
    blob = Blob(original)
    store = RuntimeStore("daily", root=tmp_path)
    registry = DeploymentRegistry(blob, store.outbox("registry"))
    with store.ownership():
        asyncio.run(registry.load())
    assert registry.get(current.target_key) == current
    assert not blob.writes and blob.data == original
    projected = store.outbox("registry").read("deployment-registry")
    assert "content_hash" not in projected["targets"][current.target_key]
    assert projected["targets"][current.target_key]["details"]["content_hash"] == current.content_hash


@pytest.mark.parametrize("location", ["top_level", "details"])
@pytest.mark.parametrize("mutation", ["invalid_hash", "missing_metadata", "mismatched_hash", "mismatched_source"])
def test_invalid_content_identity_never_replaces_registry_cache(tmp_path, mutation, location):
    value = asdict(content_deployment())
    if mutation == "invalid_hash":
        value["content_hash"] = "not-a-commit-or-content-hash"
    elif mutation == "missing_metadata":
        value["details"].pop("metadata")
    else:
        key = "aiq_content_hash" if mutation == "mismatched_hash" else "aiq_source_revision"
        value["details"]["metadata"][key] = "different"
    if location == "details":
        value["details"]["content_hash"] = value.pop("content_hash")
    blob = Blob(json.dumps({"schema_version": "1.0", "targets": {value["target_key"]: value}}).encode())
    store = RuntimeStore("daily", root=tmp_path)
    cache = store.outbox("registry")
    previous = {"schema_version": "1.0", "targets": {}}
    with store.ownership():
        cache.save_progress("deployment-registry", previous)
        with pytest.raises(QualityError, match="registry_format_invalid"):
            asyncio.run(DeploymentRegistry(blob, cache).load())
    assert cache.read("deployment-registry") == previous
    assert not blob.writes


def test_conflicting_hash_representations_fail_before_cache_or_blob_change(tmp_path):
    current = content_deployment()
    value = asdict(current)
    value["details"]["content_hash"] = "v1:sha256:" + "b" * 64
    blob = Blob(json.dumps({"schema_version": "1.0", "targets": {current.target_key: value}}).encode())
    original = blob.data
    store = RuntimeStore("daily", root=tmp_path)
    cache = store.outbox("registry")
    previous = {"schema_version": "1.0", "targets": {}}
    with store.ownership():
        cache.save_progress("deployment-registry", previous)
        with pytest.raises(QualityError, match="registry_format_invalid"):
            asyncio.run(DeploymentRegistry(blob, cache).load())
    assert cache.read("deployment-registry") == previous
    assert not blob.writes and blob.data == original


def test_in_memory_conflicting_details_hash_is_not_silently_overwritten(tmp_path):
    current = content_deployment()
    conflicting = replace(current, details={
        **current.details, "content_hash": "v1:sha256:" + "b" * 64,
    })
    store = RuntimeStore("daily", root=tmp_path)
    blob = Blob()
    registry = DeploymentRegistry(blob, store.outbox("registry"))
    with store.ownership():
        asyncio.run(registry.load())
        with pytest.raises(QualityError, match="registry_format_invalid"):
            asyncio.run(registry.save(conflicting))
    assert not blob.writes and not registry.records


@pytest.mark.parametrize("details", [None, [], [("provisioning_state", "active")], "not an object"])
def test_registry_projection_does_not_coerce_invalid_details(tmp_path, details):
    store = RuntimeStore("daily", root=tmp_path)
    blob = Blob()
    registry = DeploymentRegistry(blob, store.outbox("registry"))
    with store.ownership():
        asyncio.run(registry.load())
        with pytest.raises(QualityError, match="registry_format_invalid"):
            asyncio.run(registry.save(replace(deployment(), details=details)))
    assert not blob.writes and not registry.records


def document(*records):
    return {
        "schema_version": "1.0",
        "targets": {
            item.target_key: {
                key: value for key, value in asdict(item).items()
                if key != "content_hash" or value is not None
            }
            for item in records
        },
    }


class UncertainBlob(Blob):
    def __init__(self, *, commits=True, read_failure=False, conflict=False):
        super().__init__()
        self.commits = commits
        self.read_failure = read_failure
        self.conflict = conflict
        self.reads = 0

    async def read(self):
        self.reads += 1
        if self.writes and self.read_failure:
            raise QualityError("registry_read_failed")
        return await super().read()

    async def write(self, data, etag):
        if self.commits:
            await super().write(data, etag)
        else:
            self.writes.append(data)
        raise QualityError(
            "registry_write_failed",
            status=412 if self.conflict else None,
            request_accepted=False if self.conflict else None,
        )


def test_content_hash_unknown_write_reconciles_the_compatible_representation(tmp_path):
    store = RuntimeStore("daily", root=tmp_path)
    blob = UncertainBlob()
    registry = DeploymentRegistry(blob, store.outbox("registry"))
    current = content_deployment()
    with store.ownership():
        asyncio.run(registry.load())
        asyncio.run(registry.save(current))
        asyncio.run(registry.save(current))
    assert len(blob.writes) == 1 and blob.reads == 2
    value = json.loads(blob.data)["targets"][current.target_key]
    assert "content_hash" not in value
    assert value["details"]["content_hash"] == current.content_hash
    assert registry.get(current.target_key) == current


@pytest.mark.parametrize("conflict", [False, True])
def test_committed_unknown_write_is_reconciled_without_rewriting(tmp_path, conflict):
    store = RuntimeStore("daily", root=tmp_path)
    blob = UncertainBlob(conflict=conflict)
    registry = DeploymentRegistry(blob, store.outbox("registry"))
    target = deployment()
    with store.ownership():
        asyncio.run(registry.load())
        asyncio.run(registry.save(target))
        asyncio.run(registry.save(target))
    assert registry.get(target.target_key) == target
    assert registry.etag == blob.etag
    assert len(blob.writes) == 1
    assert blob.reads == 2
    assert store.outbox("registry").read("deployment-registry") == document(target)


def test_unresolved_write_blocks_other_lanes_until_exact_read_reconciles(tmp_path):
    store = RuntimeStore("daily", root=tmp_path)
    blob = UncertainBlob(commits=False)
    registry = DeploymentRegistry(blob, store.outbox("registry"))
    target = deployment()
    with store.ownership():
        asyncio.run(registry.load())
        for value in (target, deployment("healthcare-agent")):
            with pytest.raises(QualityError, match="registry_write_unresolved") as error:
                asyncio.run(registry.save(value))
            assert error.value.request_accepted is None and not error.value.retryable
        assert not registry.records
        assert store.outbox("registry").read("deployment-registry") == document()
        assert len(blob.writes) == 1
        blob.data, blob.etag = blob.writes[0], "eventual-native-etag"
        asyncio.run(registry.save(target))
    assert len(blob.writes) == 1
    assert registry.etag == "eventual-native-etag"
    assert registry.get(target.target_key) == target


def test_restart_load_recognizes_prior_committed_write(tmp_path):
    store = RuntimeStore("daily", root=tmp_path)
    blob = UncertainBlob(read_failure=True)
    target = deployment()
    with store.ownership():
        registry = DeploymentRegistry(blob, store.outbox("registry"))
        asyncio.run(registry.load())
        with pytest.raises(QualityError, match="registry_read_failed"):
            asyncio.run(registry.save(target))
        blob.read_failure = False
        restarted = DeploymentRegistry(blob, store.outbox("registry"))
        asyncio.run(restarted.load())
        asyncio.run(restarted.save(target))
    assert restarted.get(target.target_key) == target
    assert len(blob.writes) == 1


def test_conflicting_remote_writer_is_never_overwritten(tmp_path):
    store = RuntimeStore("daily", root=tmp_path)
    target = deployment()
    previous = deployment(version="41")
    other = deployment("healthcare-agent")
    blob = UncertainBlob(commits=False, conflict=True)
    blob.data, blob.etag = json.dumps(document(previous)).encode(), "initial-etag"
    registry = DeploymentRegistry(blob, store.outbox("registry"))
    with store.ownership():
        asyncio.run(registry.load())
        concurrent = document(replace(previous, provider_version="99"), other)
        blob.data, blob.etag = json.dumps(concurrent).encode(), "concurrent-etag"
        with pytest.raises(QualityError, match="registry_write_conflict") as error:
            asyncio.run(registry.save(target))
        assert error.value.request_accepted is False
        assert registry.get(target.target_key) == previous
        with pytest.raises(QualityError, match="registry_write_unresolved"):
            asyncio.run(registry.save(other))
    assert len(blob.writes) == 1
    assert json.loads(blob.data) == concurrent
    assert store.outbox("registry").read("deployment-registry") == document(previous)


def test_failed_cache_commit_keeps_memory_consistent_and_reconciles_next_save():
    class Cache:
        def __init__(self):
            self.calls = 0
            self.value = None

        def save_progress(self, key, value):
            self.calls += 1
            if self.calls == 2:
                raise QualityError("state_checkpoint_failed")
            self.value = value

    cache = Cache()
    blob = Blob()
    registry = DeploymentRegistry(blob, cache)
    target = deployment()
    asyncio.run(registry.load())
    with pytest.raises(QualityError, match="state_checkpoint_failed"):
        asyncio.run(registry.save(target))
    assert registry.records == {} and registry.etag is None
    assert cache.value == document()
    asyncio.run(registry.save(target))
    assert registry.get(target.target_key) == target
    assert cache.value == document(target) and len(blob.writes) == 1


def test_registry_must_load_before_it_can_write(tmp_path):
    store = RuntimeStore("daily", root=tmp_path)
    blob = Blob()
    with store.ownership(), pytest.raises(QualityError, match="registry_not_loaded"):
        asyncio.run(DeploymentRegistry(blob, store.outbox("registry")).save(deployment()))
    assert not blob.writes


def test_identical_json_records_do_not_rewrite_after_restart(tmp_path):
    target = replace(deployment(), details={"provisioning_state": "active", "items": ("synthetic",)})
    blob = Blob(json.dumps(document(target)).encode())
    store = RuntimeStore("daily", root=tmp_path)
    registry = DeploymentRegistry(blob, store.outbox("registry"))
    with store.ownership():
        asyncio.run(registry.load())
        asyncio.run(registry.save(target))
    assert not blob.writes


def test_transport_diagnostics_are_not_exposed_on_unresolved_write(tmp_path):
    class FailedBlob(Blob):
        async def write(self, data, etag):
            raise OSError("Synthetic private transport diagnostic")

    store = RuntimeStore("daily", root=tmp_path)
    registry = DeploymentRegistry(FailedBlob(), store.outbox("registry"))
    with store.ownership():
        asyncio.run(registry.load())
        with pytest.raises(QualityError, match="registry_write_unresolved") as error:
            asyncio.run(registry.save(deployment()))
    assert error.value.__cause__ is None and error.value.__suppress_context__


@pytest.mark.parametrize("data", [
    b'{"schema_version":"1.0","targets":{},"targets":{}}',
    b'{"schema_version":"1.0","targets":{"weather-agent/v0":{"details":NaN}}}',
])
def test_invalid_registry_json_never_replaces_cache(tmp_path, data):
    store = RuntimeStore("daily", root=tmp_path)
    cache = store.outbox("registry")
    with store.ownership():
        cache.save_progress("deployment-registry", document())
        with pytest.raises(QualityError, match="registry_format_invalid"):
            asyncio.run(DeploymentRegistry(Blob(data), cache).load())
    assert cache.read("deployment-registry") == document()


@pytest.fixture
def azure_errors(monkeypatch):
    class AzureError(Exception):
        pass

    class HttpResponseError(AzureError):
        def __init__(self, code=None, status=404):
            super().__init__("Synthetic private provider diagnostic")
            self.error_code = code
            self.status_code = status

    class ResourceNotFoundError(HttpResponseError):
        pass

    exceptions = ModuleType("azure.core.exceptions")
    exceptions.AzureError = AzureError
    exceptions.HttpResponseError = HttpResponseError
    exceptions.ResourceNotFoundError = ResourceNotFoundError
    core = ModuleType("azure.core")
    core.MatchConditions = SimpleNamespace(IfNotModified="synthetic-if-not-modified")
    monkeypatch.setitem(sys.modules, "azure.core", core)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", exceptions)
    return exceptions


@pytest.fixture
def azure_blob():
    return AzureRegistryBlob(Environment(
        "daily", "account", "project", "https://example.invalid/api/projects/project",
        "/synthetic/telemetry", "storage", "registry", "swedencentral", "SwedenCentral",
    ))


@pytest.mark.parametrize("code", ["BlobNotFound", "ContainerNotFound", "AccountNotFound", None])
def test_azure_missing_blob_is_not_a_missing_container_or_account(azure_errors, azure_blob, code):
    class Client:
        async def download_blob(self):
            raise azure_errors.ResourceNotFoundError(code)

    azure_blob._client = Client()
    if code == "BlobNotFound":
        assert asyncio.run(azure_blob.read()) is None
    else:
        with pytest.raises(QualityError, match="registry_resource_missing") as error:
            asyncio.run(azure_blob.read())
        assert error.value.status == 404
        assert error.value.__cause__ is None and error.value.__suppress_context__


def test_missing_container_never_empties_previous_registry_cache(tmp_path, azure_blob, azure_errors):
    class Client:
        async def download_blob(self):
            raise azure_errors.ResourceNotFoundError("ContainerNotFound")

    azure_blob._client = Client()
    store = RuntimeStore("daily", root=tmp_path)
    cache = store.outbox("registry")
    with store.ownership():
        cache.save_progress("deployment-registry", document(deployment()))
        with pytest.raises(QualityError, match="registry_resource_missing"):
            asyncio.run(DeploymentRegistry(azure_blob, cache).load())
    assert cache.read("deployment-registry") == document(deployment())


@pytest.mark.parametrize("etag", [None, '"native-etag"'])
def test_azure_write_uses_native_create_or_etag_precondition(azure_errors, azure_blob, etag):
    calls = []

    class Client:
        async def upload_blob(self, data, **options):
            calls.append((data, options))
            return {"etag": '"next-native-etag"'}

    azure_blob._client = Client()
    assert asyncio.run(azure_blob.write(b"synthetic", etag)) == '"next-native-etag"'
    assert calls == [(b"synthetic", {"overwrite": etag is not None, **(
        {"etag": etag, "match_condition": "synthetic-if-not-modified"} if etag is not None else {}
    )})]


@pytest.mark.parametrize("status,accepted", [(403, False), (409, False), (412, False), (408, None), (503, None)])
def test_azure_write_errors_have_safe_acceptance_semantics(azure_errors, azure_blob, status, accepted):
    class Client:
        async def upload_blob(self, data, **options):
            raise azure_errors.HttpResponseError("SyntheticCode", status)

    azure_blob._client = Client()
    with pytest.raises(QualityError, match="registry_write_failed") as error:
        asyncio.run(azure_blob.write(b"synthetic", None))
    assert error.value.status == status and error.value.request_accepted is accepted
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_azure_client_disables_implicit_write_retries(azure_blob, monkeypatch):
    kwargs = {}
    identity = ModuleType("azure.identity.aio")
    identity.AzureCliCredential = lambda: "synthetic-credential"
    blob = ModuleType("azure.storage.blob.aio")

    def construct(**options):
        kwargs.update(options)
        return SimpleNamespace()

    blob.BlobClient = construct
    monkeypatch.setitem(sys.modules, "azure.identity.aio", identity)
    monkeypatch.setitem(sys.modules, "azure.storage.blob.aio", blob)
    azure_blob._get_client()
    assert kwargs["retry_total"] == 0
    assert kwargs["credential"] == "synthetic-credential"
    assert kwargs["container_name"] == "deployment-registries"
    assert kwargs["blob_name"] == "swedencentral-g30/runner-v1/daily.json"


@pytest.mark.parametrize("etag", ["", "*", 42, None])
def test_invalid_download_etag_never_replaces_cache(tmp_path, etag):
    store = RuntimeStore("daily", root=tmp_path)
    cache = store.outbox("registry")
    blob = Blob(json.dumps(document()).encode())
    blob.etag = etag
    with store.ownership():
        cache.save_progress("deployment-registry", document(deployment()))
        with pytest.raises(QualityError, match="registry_etag_invalid"):
            asyncio.run(DeploymentRegistry(blob, cache).load())
    assert cache.read("deployment-registry") == document(deployment())
