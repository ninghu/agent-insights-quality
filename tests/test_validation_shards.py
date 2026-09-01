from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from agent_insights_quality.catalogs import load_catalogs
from agent_insights_quality.util import ContractError, content_hash
from agent_insights_quality.validation_coordinator import (
    INVOKE_SHARD_CONCURRENCY,
    _invoke_worker_capacity,
)
from agent_insights_quality.validation_manifest import authority_specs
from agent_insights_quality.validation_quota import CapacityPlan
from agent_insights_quality.validation_shards import (
    ValidationShardStore,
    compose_shard_authorities,
    import_shard_resources,
    shard_root,
    validate_shard_assignment,
)


def _authority_ids() -> list[str]:
    agents, issues = load_catalogs()
    return [item.authority_id for item in authority_specs(agents, issues)]


def _authorities():
    agents, issues = load_catalogs()
    return authority_specs(agents, issues)


def _packages() -> list[dict]:
    authority_ids = _authority_ids()
    groups = [authority_ids[index::10] for index in range(10)]
    packages = []
    for index, group in enumerate(groups, start=1):
        package = {
            "schema_version": "1.0.0",
            "kind": "test-agent-validation-shard-package",
            "shard_id": index,
            "authority_ids": group,
            "binding": {
                "repository": "synthetic/example",
                "pr_number": 63,
                "commit_sha": "a" * 40,
                "validation_digest": "sha256:" + ("b" * 64),
                "execution_matrix_digest": "sha256:" + ("c" * 64),
                "runtime_topology_digest": "sha256:" + ("d" * 64),
                "project_id": "synthetic-project",
                "authorities": [],
            },
            "invocation_digest": content_hash({"shard": index}),
            "authorities": [
                {"authority_id": authority_id} for authority_id in group
            ],
        }
        package["artifact_digest"] = content_hash(package)
        packages.append(package)
    return packages


def test_shard_assignment_is_explicit_catalog_authority() -> None:
    authorities = _authorities()
    assert [
        item.authority_id
        for item in validate_shard_assignment(1, ["issue-001"], authorities)
    ] == ["issue-001"]
    with pytest.raises(ContractError, match="assignment is invalid"):
        validate_shard_assignment(
            1,
            ["issue-001", "issue-001"],
            authorities,
        )
    with pytest.raises(ContractError, match="unknown authority"):
        validate_shard_assignment(1, ["research-agent/v0"], authorities)


def test_shard_runtime_namespaces_do_not_collide(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_shards.validation_runtime_root",
        lambda: tmp_path,
    )
    first = shard_root(
        repository="synthetic/example",
        pr_number=63,
        cycle_id="cycle",
        shard_id=1,
    )
    second = shard_root(
        repository="synthetic/example",
        pr_number=63,
        cycle_id="cycle",
        shard_id=2,
    )
    assert first != second
    assert first.name == "shard-01"
    assert second.name == "shard-02"


def test_compose_requires_ten_exact_nonoverlapping_shards() -> None:
    packages = _packages()
    composed = compose_shard_authorities(
        packages,
        _authorities(),
    )
    assert len(composed) == 41

    duplicate = copy.deepcopy(packages)
    duplicate[1]["authority_ids"][0] = duplicate[0]["authority_ids"][0]
    duplicate[1]["authorities"][0]["authority_id"] = duplicate[0][
        "authorities"
    ][0]["authority_id"]
    duplicate[1]["artifact_digest"] = content_hash(
        {key: value for key, value in duplicate[1].items() if key != "artifact_digest"}
    )
    with pytest.raises(ContractError, match="bindings are inconsistent"):
        compose_shard_authorities(
            duplicate,
            _authorities(),
        )

    mismatched = copy.deepcopy(packages)
    mismatched[3]["binding"]["commit_sha"] = "e" * 40
    mismatched[3]["artifact_digest"] = content_hash(
        {
            key: value
            for key, value in mismatched[3].items()
            if key != "artifact_digest"
        }
    )
    with pytest.raises(ContractError, match="binding"):
        compose_shard_authorities(
            mismatched,
            _authorities(),
        )


def test_package_binds_exact_invocation_digest(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_shards.validation_runtime_root",
        lambda: tmp_path,
    )
    prepared = {
        "repository": "synthetic/example",
        "pr_number": 63,
        "commit_sha": "a" * 40,
        "cycle_id": "cycle",
        "digests": {
            "validation_digest": "sha256:" + ("b" * 64),
            "execution_matrix_digest": "sha256:" + ("c" * 64),
            "runtime_topology_digest": "sha256:" + ("d" * 64),
        },
        "project": {"provider_id": "synthetic-project"},
        "runtime_topology": {
            "agents": [
                {
                    "authority_id": "weather-agent/v0",
                    "canonical_agent": "weather-agent",
                    "runtime_agent_name": "weather-agent-baseline",
                    "runtime_agent_version": "server-version-1",
                    "provider_agent_id": "agent/weather",
                    "provider_agent_version_id": "agent/weather/versions/1",
                    "provider_content_digest": "sha256:" + ("e" * 64),
                }
            ]
        },
    }
    store = ValidationShardStore(
        prepared=prepared,
        shard_id=1,
        authority_ids=["weather-agent/v0"],
    )
    store.begin_invocation()
    store.record_authority({"authority_id": "weather-agent/v0"})
    invocation = store.complete_invocation()
    package = store.write_package(
        authorities=[{"authority_id": "weather-agent/v0"}],
    )
    assert package["invocation_digest"] == invocation["artifact_digest"]


def test_eight_invoke_workers_do_not_multiply_prepared_capacity() -> None:
    capacity = CapacityPlan(
        measured_rpm=1000,
        measured_tpm=1_000_000,
        measured_at="2026-08-29T00:00:00Z",
        reserved_percent=20,
        reserved_rpm=200,
        reserved_tpm=200_000,
        available_rpm=800,
        available_tpm=800_000,
        outer_request_envelope=1,
        worst_case_inner_model_calls=1,
        worst_case_inner_tokens=1,
        endpoint_concurrency=8,
        provisioning_concurrency=8,
        telemetry_query_concurrency=4,
        runtime_attempt_concurrency=1,
        inner_model_call_limit=4,
        plan_digest="sha256:" + ("f" * 64),
    )
    allocation = _invoke_worker_capacity(capacity, [_authorities()[0]])
    assert allocation[0] * INVOKE_SHARD_CONCURRENCY <= capacity.available_rpm
    assert allocation[1] * INVOKE_SHARD_CONCURRENCY <= capacity.available_tpm


def test_invocation_rerun_preserves_ledger_until_exact_cleanup(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_shards.validation_runtime_root",
        lambda: tmp_path,
    )
    prepared = _prepared_baseline()
    store = ValidationShardStore(
        prepared=prepared,
        shard_id=1,
        authority_ids=["weather-agent/v0"],
    )
    store.begin_invocation()
    store.record_resource({"intent_reference": "private-ledger-entry"})
    store.record_authority({"authority_id": "weather-agent/v0"})
    retained = store.read_invocations()

    with pytest.raises(ContractError, match="active ledger"):
        store.begin_invocation()
    assert store.read_invocations() == retained

    store.write_cleanup(
        invocation_digest=retained["artifact_digest"],
        retained_count=0,
    )
    restarted = store.begin_invocation()
    assert restarted["resources"] == []
    assert restarted["invocations"] == []
    with pytest.raises(ContractError, match="cleanup is not reusable"):
        store.record_authority({"authority_id": "weather-agent/v0"})
        store.begin_invocation()


def test_shard_resource_import_is_exact_idempotent() -> None:
    intent = content_hash({"resource": "response"})
    create = {
        "state": "create_intent",
        "kind": "stored_response",
        "intent_reference": intent,
        "deterministic_name": "response-intent",
        "authority_id": "weather-agent/v0",
        "parent_id": "agent/weather",
        "runtime_kind": "prompt",
        "discovery_key": f"weather-agent-baseline|{intent}",
    }
    created = {
        **create,
        "state": "created",
        "provider_id": "response-1",
        "deterministic_name": "response-1",
    }

    class Controller:
        def __init__(self) -> None:
            self.active = SimpleNamespace(
                value={"ownership_nonce": "nonce", "resources": []}
            )
            self.applied = 0

        def dynamic_resource_event(self, event, *, now) -> None:
            del now
            self.applied += 1
            if event["state"] == "create_intent":
                self.active.value["resources"].append(
                    {
                        **event,
                        "provider_id": intent,
                        "ownership_nonce": "nonce",
                        "cleanup_method": "explicit",
                    }
                )
            else:
                self.active.value["resources"][0].update(event)

    artifact = {"shard_id": 1, "resources": [create, created]}
    controller = Controller()
    import_shard_resources(controller, [artifact], now=lambda: None)
    import_shard_resources(controller, [artifact], now=lambda: None)
    assert controller.applied == 2

    mismatched = copy.deepcopy(artifact)
    mismatched["resources"][1]["provider_id"] = "response-2"
    with pytest.raises(ContractError, match="provider binding changed"):
        import_shard_resources(controller, [mismatched], now=lambda: None)


def _prepared_baseline() -> dict:
    return {
        "repository": "synthetic/example",
        "pr_number": 63,
        "commit_sha": "a" * 40,
        "cycle_id": "cycle",
        "digests": {
            "validation_digest": "sha256:" + ("b" * 64),
            "execution_matrix_digest": "sha256:" + ("c" * 64),
            "runtime_topology_digest": "sha256:" + ("d" * 64),
        },
        "project": {"provider_id": "synthetic-project"},
        "runtime_topology": {
            "agents": [
                {
                    "authority_id": "weather-agent/v0",
                    "canonical_agent": "weather-agent",
                    "runtime_agent_name": "weather-agent-baseline",
                    "runtime_agent_version": "server-version-1",
                    "provider_agent_id": "agent/weather",
                    "provider_agent_version_id": "agent/weather/versions/1",
                    "provider_content_digest": "sha256:" + ("e" * 64),
                }
            ]
        },
    }
