from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality.catalogs import (
    catalog_hashes,
    load_catalogs,
    agent_model_contract,
)
from agent_insights_quality.registry import (
    REGISTRY_CONTAINER,
    _run_registry_command,
    load_registry,
    publish_registry,
    sync_registry,
)
from agent_insights_quality.util import ContractError


def _registry(profile: str) -> dict:
    agents, issues = load_catalogs()
    return {
        "schema_version": "2.0.0",
        "profile": profile,
        "environment_id": "swedencentral-g30",
        "location": "swedencentral",
        "account_name": f"aiq-{profile}-swedencentral",
        "project_name": f"aiq-{profile}-swedencentral",
        "telemetry_resource_set": "g30",
        "test_region": "SwedenCentral",
        "test_agent_model": agent_model_contract(agents),
        "catalog_hashes": catalog_hashes(agents, issues),
        "agents": {
            agent["name"]: {
                "monitor_id": f"monitor-{agent['name']}",
                "versions": {
                    logical: {
                        "foundry_version": str(index + 1),
                        "content_digest": "sha256:" + f"{index + 1:064x}",
                    }
                    for index, logical in enumerate(["v0", *agent["issue_ids"]])
                },
            }
            for agent in agents["agents"]
        },
    }


def test_registry_is_profile_isolated(tmp_path: Path) -> None:
    agents, issues = load_catalogs()
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(_registry("daily")), encoding="utf-8")
    loaded = load_registry(
        path,
        profile="daily",
        catalog_hashes=catalog_hashes(agents, issues),
    )
    assert loaded["project_name"] == "aiq-daily-swedencentral"
    with pytest.raises(ContractError, match="different profile"):
        load_registry(
            path,
            profile="staging",
            catalog_hashes=catalog_hashes(agents, issues),
        )
    invalid = _registry("daily")
    invalid["agents"]["unexpected-agent"] = invalid["agents"].pop(
        "support-ticket-agent"
    )
    path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(ContractError, match="inventory"):
        load_registry(
            path,
            profile="daily",
            catalog_hashes=catalog_hashes(agents, issues),
        )


def test_registry_syncs_through_private_blob_storage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "daily.json"
    profile = SimpleNamespace(
        name="daily",
        registry_path=path,
        registry_storage_account_name="aiqsweartsynthetic",
        environment_id="swedencentral-g30",
    )
    calls = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        if "download" in arguments:
            destination = Path(arguments[arguments.index("--file") + 1])
            destination.write_text(
                json.dumps(_registry("daily")),
                encoding="utf-8",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "agent_insights_quality.registry.subprocess.run",
        run,
    )
    sync_registry(profile)
    assert json.loads(path.read_text(encoding="utf-8"))["profile"] == "daily"
    publish_registry(profile)
    assert "download" in calls[0]
    assert "upload" in calls[1]
    assert calls[0][calls[0].index("--account-name") + 1] == "aiqsweartsynthetic"
    assert calls[1][calls[1].index("--account-name") + 1] == "aiqsweartsynthetic"
    assert calls[0][calls[0].index("--container-name") + 1] == REGISTRY_CONTAINER
    assert calls[1][calls[1].index("--container-name") + 1] == REGISTRY_CONTAINER
    assert "--auth-mode" in calls[0]
    assert "--auth-mode" in calls[1]


def test_registry_command_retries_transient_failures(monkeypatch) -> None:
    attempts = 0

    def run(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise subprocess.TimeoutExpired("az", 120)
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr("agent_insights_quality.registry.subprocess.run", run)
    monkeypatch.setattr("agent_insights_quality.registry.time.sleep", lambda _: None)
    assert _run_registry_command(["az"]).returncode == 0
    assert attempts == 2
