from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_insights_quality.catalogs import catalog_hashes, load_catalogs
from agent_insights_quality.registry import load_registry
from agent_insights_quality.util import ContractError


def _registry(profile: str) -> dict:
    agents, issues = load_catalogs()
    return {
        "schema_version": "1.0.0",
        "profile": profile,
        "project_name": (
            "agent-insights-quality"
            if profile == "daily"
            else "agent-insights-quality-staging"
        ),
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
    assert loaded["project_name"] == "agent-insights-quality"
    with pytest.raises(ContractError, match="different profile"):
        load_registry(
            path,
            profile="staging",
            catalog_hashes=catalog_hashes(agents, issues),
        )
