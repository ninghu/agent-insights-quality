from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_insights_quality.validation_coordinator import (
    _cleanup_context,
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
