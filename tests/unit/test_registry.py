import asyncio
import json

import pytest

from agent_insights_quality.contracts import Deployment
from agent_insights_quality.errors import QualityError
from agent_insights_quality.registry import DeploymentRegistry
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
