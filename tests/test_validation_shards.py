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
from agent_insights_quality.validation_runtime import DeployedRuntime
from agent_insights_quality.validation_shards import (
    ValidationDeploymentShardStore,
    ValidationShardStore,
    compose_shard_authorities,
    import_shard_resources,
    shard_root,
    validate_shard_assignment,
)


def _authorities():
    agents, issues = load_catalogs()
    return authority_specs(agents, issues)


def _prepared() -> dict:
    runtime = []
    for index, authority in enumerate(_authorities(), start=1):
        runtime.append(
            {
                "authority_id": authority.authority_id,
                "canonical_agent": authority.canonical_agent,
                "logical_version": authority.logical_version,
                "runtime_agent_name": f"synthetic-agent-{index}",
                "runtime_agent_version": "1",
                "provider_agent_id": f"agent-{index}",
                "provider_agent_version_id": f"version-{index}",
                "provider_content_digest": f"sha256:{index:064x}",
            }
        )
    return {
        "repository": "synthetic/example",
        "pr_number": 63,
        "commit_sha": "a" * 40,
        "run_id": "validation-0123456789ab",
        "digests": {
            "validation_digest": "sha256:" + ("b" * 64),
            "execution_matrix_digest": "sha256:" + ("c" * 64),
            "runtime_topology_digest": "sha256:" + ("d" * 64),
        },
        "project": {"provider_id": "synthetic-project"},
        "runtime_topology": {"agents": runtime},
    }


def test_shard_assignment_is_explicit_catalog_authority() -> None:
    authorities = _authorities()
    assert [
        item.authority_id
        for item in validate_shard_assignment(1, ["issue-001"], authorities)
    ] == ["issue-001"]
    with pytest.raises(ContractError, match="assignment is invalid"):
        validate_shard_assignment(1, ["issue-001", "issue-001"], authorities)


def test_shard_runtime_namespaces_do_not_collide(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_shards.validation_runtime_root",
        lambda: tmp_path,
    )
    first = shard_root(
        repository="synthetic/example",
        pr_number=63,
        run_id="validation-0123456789ab",
        shard_id=1,
    )
    second = shard_root(
        repository="synthetic/example",
        pr_number=63,
        run_id="validation-0123456789ab",
        shard_id=2,
    )
    assert first != second


def test_compose_requires_exact_nonoverlapping_selected_shards() -> None:
    authorities = _authorities()[:12]
    groups = [
        [item.authority_id for item in authorities][index::10]
        for index in range(10)
    ]
    packages = []
    for shard_id, group in enumerate(groups, start=1):
        package = {
            "shard_id": shard_id,
            "authority_ids": group,
            "binding": {
                "repository": "synthetic/example",
                "pr_number": 63,
                "commit_sha": "a" * 40,
                "run_id": "validation-0123456789ab",
                "validation_digest": "sha256:" + ("b" * 64),
                "execution_matrix_digest": "sha256:" + ("c" * 64),
                "runtime_topology_digest": "sha256:" + ("d" * 64),
                "project_id": "synthetic-project",
                "authorities": [],
            },
            "authorities": [
                {"authority_id": authority_id} for authority_id in group
            ],
        }
        packages.append(package)
    assert len(compose_shard_authorities(packages, authorities)) == 12
    duplicate = copy.deepcopy(packages)
    duplicate[1]["authority_ids"][0] = duplicate[0]["authority_ids"][0]
    duplicate[1]["authorities"][0] = duplicate[0]["authorities"][0]
    with pytest.raises(ContractError, match="bindings are inconsistent"):
        compose_shard_authorities(duplicate, authorities)


def test_invocation_store_resumes_retained_partial_ledger(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_shards.validation_runtime_root",
        lambda: tmp_path,
    )
    store = ValidationShardStore(
        prepared=_prepared(),
        shard_id=1,
        authority_ids=["weather-agent/v0"],
        fence=lambda: None,
    )
    store.begin_invocation()
    store.record_authority({"authority_id": "weather-agent/v0"})
    retained = store.begin_invocation()
    assert retained["status"] == "invoking"
    assert retained["invocations"] == [{"authority_id": "weather-agent/v0"}]
    assert store.complete_invocation()["status"] == "invoked"


def test_deployment_store_writes_immutable_per_authority_receipts(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_shards.validation_runtime_root",
        lambda: tmp_path,
    )
    store = ValidationDeploymentShardStore(
        prepared=_prepared(),
        shard_id=1,
        authority_ids=["weather-agent/v0"],
        desired_state_digest="sha256:" + ("e" * 64),
        fence=lambda: None,
    )
    runtime = DeployedRuntime(
        authority_id="weather-agent/v0",
        runtime_kind="prompt",
        runtime_agent_name="weather-agent-baseline",
        runtime_agent_version="1",
        provider_agent_id="agent",
        provider_agent_version_id="version",
        provider_content_digest="sha256:" + ("f" * 64),
        hosted_identity_id=None,
        hosted_blueprint_id=None,
        hosted_deployment_id=None,
        runtime_principal_id=None,
        telemetry_identity_id="version",
        connection_ids=(),
    )
    receipt = store.write_authority(
        authority_id=runtime.authority_id,
        runtime=runtime,
        resources=[],
    )
    assert receipt["receipt_digest"].startswith("sha256:")
    assert store.completed_authority_ids() == {"weather-agent/v0"}
    assert store.complete()["authority_ids"] == ["weather-agent/v0"]


def test_stale_worker_is_fenced_before_receipt_write(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_shards.validation_runtime_root",
        lambda: tmp_path,
    )

    def stale() -> None:
        raise ContractError("Stale validation worker is fenced")

    store = ValidationDeploymentShardStore(
        prepared=_prepared(),
        shard_id=1,
        authority_ids=["weather-agent/v0"],
        desired_state_digest="sha256:" + ("e" * 64),
        fence=stale,
    )
    runtime = DeployedRuntime(
        authority_id="weather-agent/v0",
        runtime_kind="prompt",
        runtime_agent_name="weather-agent-baseline",
        runtime_agent_version="1",
        provider_agent_id="agent",
        provider_agent_version_id="version",
        provider_content_digest="sha256:" + ("f" * 64),
        hosted_identity_id=None,
        hosted_blueprint_id=None,
        hosted_deployment_id=None,
        runtime_principal_id=None,
        telemetry_identity_id="version",
        connection_ids=(),
    )
    with pytest.raises(ContractError, match="Stale validation worker"):
        store.write_authority(
            authority_id=runtime.authority_id,
            runtime=runtime,
            resources=[],
        )
    assert not list(tmp_path.rglob("*.json"))


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
        "retention": "retained",
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
                    }
                )
            else:
                self.active.value["resources"][0].update(event)

    artifact = {"shard_id": 1, "resources": [create, created]}
    controller = Controller()
    import_shard_resources(controller, [artifact], now=lambda: None)
    import_shard_resources(controller, [artifact], now=lambda: None)
    assert controller.applied == 2


def test_shard_resource_import_allows_hosted_identity_principal_alias() -> None:
    identity_intent = content_hash({"resource": "identity"})
    principal_intent = content_hash({"resource": "principal"})
    authority_id = "issue-017"
    resources = [
        {
            "state": "create_intent",
            "kind": kind,
            "intent_reference": intent,
            "provider_id": intent,
            "authority_id": authority_id,
            "parent_id": None,
            "ownership_nonce": "nonce",
        }
        for kind, intent in (
            ("hosted_identity", identity_intent),
            ("runtime_principal", principal_intent),
        )
    ]

    class Controller:
        def __init__(self) -> None:
            self.active = SimpleNamespace(
                value={"ownership_nonce": "nonce", "resources": resources}
            )

        def dynamic_resource_event(self, event, *, now) -> None:
            del now
            item = next(
                value
                for value in self.active.value["resources"]
                if value["intent_reference"] == event["intent_reference"]
            )
            item.update(event)

    artifact = {
        "shard_id": 1,
        "resources": [
            {
                **resources[0],
                "state": "created",
                "provider_id": "shared-identity",
            },
            {
                **resources[1],
                "state": "created",
                "provider_id": "shared-identity",
            },
        ],
    }
    controller = Controller()
    import_shard_resources(controller, [artifact], now=lambda: None)
    assert {
        item["provider_id"] for item in controller.active.value["resources"]
    } == {"shared-identity"}


def test_shard_resource_import_rejects_identity_alias_across_authorities() -> None:
    identity_intent = content_hash({"resource": "identity"})
    principal_intent = content_hash({"resource": "principal"})
    resources = [
        {
            "state": "created",
            "kind": "hosted_identity",
            "intent_reference": identity_intent,
            "provider_id": "shared-identity",
            "authority_id": "issue-017",
            "parent_id": None,
            "ownership_nonce": "nonce",
        },
        {
            "state": "create_intent",
            "kind": "runtime_principal",
            "intent_reference": principal_intent,
            "provider_id": principal_intent,
            "authority_id": "issue-018",
            "parent_id": None,
            "ownership_nonce": "nonce",
        },
    ]
    controller = SimpleNamespace(
        active=SimpleNamespace(
            value={"ownership_nonce": "nonce", "resources": resources}
        )
    )
    artifact = {
        "shard_id": 1,
        "resources": [
            {
                **resources[1],
                "state": "created",
                "provider_id": "shared-identity",
            }
        ],
    }
    with pytest.raises(
        ContractError,
        match="Validation shard resource provider binding changed",
    ):
        import_shard_resources(controller, [artifact], now=lambda: None)


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
