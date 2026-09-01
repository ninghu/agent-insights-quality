from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from agent_insights_quality.live import LiveRuntime
from agent_insights_quality.profiles import RuntimeProfile
from agent_insights_quality.util import ContractError
from agent_insights_quality.validation_coordinator import (
    _cleanup_context,
    _runner,
    cleanup_test_agent_validation,
)


@pytest.mark.parametrize(
    "state",
    [
        "LOCKED",
        "PREFLIGHT",
        "CREATING",
        "VALIDATING",
        "FINAL_CHECKS",
        "CLEANING",
        "CLEANUP_BLOCKED",
    ],
)
def test_full_cleanup_accepts_incomplete_lifecycle_without_prepared_gate(
    monkeypatch,
    tmp_path,
    state,
) -> None:
    active = {
        "cycle_id": "cycle",
        "state": state,
        "repository": "synthetic/example",
        "pr_number": 63,
        "runtime_topology": {"agents": []},
        "resources": [],
        "cleanup": {"exact_clean": False},
    }
    record = SimpleNamespace(value=active)

    class Lock:
        def __init__(self, _path) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Journal:
        def __init__(self, *, lock) -> None:
            del lock

        def read_active(self):
            return record

    class Reconciler:
        def __init__(self, **_kwargs) -> None:
            pass

        def reconcile(self, **_kwargs):
            active["state"] = "FAILED_CLEAN"
            active["cleanup"]["exact_clean"] = True
            return "FAILED_CLEAN"

    monkeypatch.setattr(
        "agent_insights_quality.validation_coordinator.validation_runtime_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_coordinator.LocalValidationLock",
        Lock,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_coordinator.LifecycleJournal",
        Journal,
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_coordinator._load_prepared",
        lambda *_args, **_kwargs: pytest.fail("cleanup used prepared gate"),
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_coordinator._cleanup_context",
        lambda _active: {
            "profile": object(),
            "operator": SimpleNamespace(token_provider=lambda _scope: "token"),
            "policy": object(),
        },
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_coordinator.AzureValidationCleanupBackend",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        "agent_insights_quality.validation_coordinator.ValidationReconciler",
        Reconciler,
    )

    result = cleanup_test_agent_validation(cycle_id="cycle")

    assert result == {
        "status": "failed_clean",
        "cycle_id": "cycle",
        "exact_clean": True,
    }


def test_cleanup_profile_uses_original_bound_substrate(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.validation_coordinator.local_azure_operator",
        lambda: SimpleNamespace(token_provider=lambda _scope: "token"),
    )
    active = {
        "cycle_id": "cycle",
        "project": {"name": "aiq-staging-swedencentral"},
        "substrate": {
            "account_name": "aiq-staging-swedencentral",
            "account_resource_id": "/synthetic/account",
            "registry_name": "syntheticregistry",
            "storage_account_name": "syntheticstorage",
            "telemetry_resource_id": "/synthetic/telemetry",
        },
    }

    context = _cleanup_context(active)

    assert context["profile"].account_resource_id == "/synthetic/account"
    assert (
        context["profile"].application_insights_resource_id
        == "/synthetic/telemetry"
    )


def test_shard_runner_skips_global_traffic_ledger_but_live_runtime_uses_it(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[str] = []

    class Ledger:
        def __init__(self, profile_name):
            calls.append(f"open:{profile_name}")

        @staticmethod
        def mark_started(*_args, **_kwargs):
            calls.append("started")

    monkeypatch.setattr("agent_insights_quality.live.TrafficLedger", Ledger)
    profile = RuntimeProfile(
        name="staging",
        project_name="aiq-staging",
        project_endpoint="https://example.invalid",
        insights_endpoint="https://example.invalid",
        application_insights_resource_id="/synthetic/telemetry",
        registry_path=Path("synthetic-registry.json"),
    )
    context = {
        "profile": profile,
        "operator": SimpleNamespace(token_provider=lambda _scope: "token"),
        "authorities": [],
        "policy": SimpleNamespace(trace_hydration_stabilization_seconds=1),
    }
    traffic = tmp_path / "traffic.json"
    traffic.write_text(
        '[{"id":"synthetic","request":{"body":{"input":"synthetic"}}}]',
        encoding="utf-8",
    )

    shard_runtime = _runner(
        context,
        record_resource=lambda _resource: None,
    )._runtime
    shard_runtime._invoke_group = lambda *_args: (_ for _ in ()).throw(
        ContractError("synthetic stop")
    )
    with pytest.raises(ContractError, match="synthetic stop"):
        shard_runtime.invoke_version(
            agent_name="weather-agent-baseline",
            agent_type="prompt",
            foundry_version="1",
            traffic_path=traffic,
            seed=1,
        )
    assert calls == []

    runtime = LiveRuntime(profile)
    runtime._invoke_group = shard_runtime._invoke_group
    with pytest.raises(ContractError, match="synthetic stop"):
        runtime.invoke_version(
            agent_name="weather-agent-baseline",
            agent_type="prompt",
            foundry_version="1",
            traffic_path=traffic,
            seed=1,
        )
    assert calls == ["open:staging", "started"]
