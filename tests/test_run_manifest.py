from __future__ import annotations

from copy import deepcopy
from datetime import date

import pytest

from agent_insights_quality.catalogs import (
    catalog_hashes,
    load_catalogs,
    source_integrity_digest,
)
from agent_insights_quality.run_manifest import (
    OFFICIAL_DELIVERY,
    TEST_EMAIL_ONLY_DELIVERY,
    validate_manifest,
)
from agent_insights_quality.selection import select_daily
from agent_insights_quality.util import ROOT, ContractError, content_hash
from agent_insights_quality.validation_rules import (
    execution_context,
    issue_observation_context,
)


def _manifest(
    *,
    run_id: str = "aiq-20260828",
    profile: str = "daily",
    delivery_mode: str = OFFICIAL_DELIVERY,
) -> dict:
    version_evidence = {
        "foundry_version": "1",
        "content_digest": "sha256:" + "a" * 64,
        "status": "inconclusive",
        "operation_ids": [],
        "insight_references": [],
        "window_start": None,
        "window_end": None,
        "error_code": "synthetic_incomplete",
        "endpoint_request_count": 0,
        "endpoint_response_count": 0,
        "endpoint_usable_response_count": 0,
        "semantic_assertion_count": 0,
        "semantic_assertions_passed": 0,
        "trace_assertion_count": 0,
        "trace_assertions_passed": 0,
        "trace_contract_verified": False,
        "trace_behavior_summary": {},
        "issue_trace_gap_acceptance": None,
        "endpoint_request_summaries": [],
        "evidence_reference": None,
    }
    agents, issues = load_catalogs()
    issue_by_id = {item["id"]: item for item in issues["issues"]}
    hashes = catalog_hashes(agents, issues)
    selection = (
        {agent["name"]: list(agent["issue_ids"]) for agent in agents["agents"]}
        if profile == "staging"
        else select_daily(
            date(2026, 8, 28),
            agents,
            issues,
            hashes["issues"],
        )
    )

    def baseline_version(agent: dict) -> dict:
        return {
            **version_evidence,
            "logical_version": "v0",
            **execution_context(
                ROOT / agent["baseline_path"] / "traffic.json"
            ),
        }

    def issue_version(issue_id: str) -> dict:
        value = {
            **version_evidence,
            **issue_observation_context(
                ROOT / issue_by_id[issue_id]["implementation"] / "traffic.json"
            ),
        }
        value.update(
            {
                "issue_id": issue_id,
                "logical_version": issue_id,
                "status": "skipped_baseline",
            }
        )
        return value
    value = {
        "schema_version": "6.0.0",
        "run_id": run_id,
        "profile": profile,
        "delivery_mode": delivery_mode,
        "report_date": "2026-08-28",
        "insight_lookback_hours": 0.1,
        "telemetry_resource_set": "g29",
        "test_region": "WestUS2",
        "test_region_registry": "WestUS2",
        "catalog_hashes": hashes,
        "source_integrity": {
            "verified": True,
            "contract_digest": source_integrity_digest(agents, issues),
        },
        "agents": [
            {
                "name": agent["name"],
                "type": agent["type"],
                "framework": agent["framework"],
                "baseline_contract": agent["baseline_contract"],
                "monitor_reference": "sha256:" + "f" * 64,
                "baseline": baseline_version(agent),
                "issues": [
                    issue_version(issue_id)
                    for issue_id in selection[agent["name"]]
                ],
            }
            for agent in agents["agents"]
        ],
        "manifest_hash": "",
    }
    value["manifest_hash"] = content_hash(
        {key: item for key, item in value.items() if key != "manifest_hash"}
    )
    return value


def test_manifest_accepts_official_and_isolated_test_delivery() -> None:
    validate_manifest(_manifest())
    validate_manifest(
        _manifest(
            run_id="aiq-20260828-r01",
            delivery_mode=TEST_EMAIL_ONLY_DELIVERY,
        )
    )


@pytest.mark.parametrize(
    ("run_id", "profile"),
    [
        ("aiq-20260828", "daily"),
        ("aiq-20260828-r01", "staging"),
    ],
)
def test_test_delivery_requires_daily_rerun_identity(
    run_id: str,
    profile: str,
) -> None:
    manifest = _manifest(
        run_id=run_id,
        profile=profile,
        delivery_mode=TEST_EMAIL_ONLY_DELIVERY,
    )
    with pytest.raises(ContractError, match="daily nonzero rerun"):
        validate_manifest(manifest)


def test_manifest_rejects_superseded_schema() -> None:
    manifest = deepcopy(_manifest())
    manifest["schema_version"] = "2.0.0"
    manifest["manifest_hash"] = content_hash(
        {key: item for key, item in manifest.items() if key != "manifest_hash"}
    )
    with pytest.raises(ContractError, match="Run manifest is invalid"):
        validate_manifest(manifest)


def test_manifest_rejects_live_project_registry_region_mismatch() -> None:
    manifest = _manifest()
    manifest["test_region_registry"] = "EastUS"
    manifest["manifest_hash"] = content_hash(
        {key: item for key, item in manifest.items() if key != "manifest_hash"}
    )
    with pytest.raises(ContractError, match="registry cross-check"):
        validate_manifest(manifest)


def test_manifest_rejects_inconsistent_request_aggregates() -> None:
    manifest = _manifest()
    manifest["agents"][0]["baseline"]["endpoint_response_count"] = 1
    manifest["manifest_hash"] = content_hash(
        {key: item for key, item in manifest.items() if key != "manifest_hash"}
    )
    with pytest.raises(ContractError, match="aggregate endpoint evidence"):
        validate_manifest(manifest)


def test_manifest_rejects_negative_evidence_counts() -> None:
    manifest = _manifest()
    manifest["agents"][0]["baseline"]["endpoint_request_count"] = -1
    manifest["manifest_hash"] = content_hash(
        {key: item for key, item in manifest.items() if key != "manifest_hash"}
    )
    with pytest.raises(ContractError, match="Run manifest is invalid"):
        validate_manifest(manifest)


def test_prompt_manifest_requires_per_request_assertions() -> None:
    manifest = _manifest()
    manifest["agents"][0]["baseline_contract"]["semantic_assertions"] = "required"
    manifest["manifest_hash"] = content_hash(
        {key: item for key, item in manifest.items() if key != "manifest_hash"}
    )
    with pytest.raises(ContractError, match="Run manifest is invalid"):
        validate_manifest(manifest)


def test_issue_manifest_requires_issue_identity_and_issue_status() -> None:
    manifest = _manifest()
    issue = deepcopy(manifest["agents"][0]["baseline"])
    issue["logical_version"] = "issue-001"
    issue["status"] = "passed"
    manifest["agents"][0]["issues"] = [issue]
    manifest["manifest_hash"] = content_hash(
        {key: item for key, item in manifest.items() if key != "manifest_hash"}
    )
    with pytest.raises(ContractError, match="Run manifest is invalid"):
        validate_manifest(manifest)


def test_manifest_rejects_fabricated_catalog_and_zero_issue_inventory() -> None:
    manifest = _manifest()
    manifest["catalog_hashes"]["issues"] = "sha256:" + "0" * 64
    manifest["manifest_hash"] = content_hash(
        {key: item for key, item in manifest.items() if key != "manifest_hash"}
    )
    with pytest.raises(ContractError, match="catalog hashes"):
        validate_manifest(manifest)
    manifest = _manifest()
    for agent in manifest["agents"]:
        agent["issues"] = []
    manifest["manifest_hash"] = content_hash(
        {key: item for key, item in manifest.items() if key != "manifest_hash"}
    )
    with pytest.raises(ContractError, match="issue inventory"):
        validate_manifest(manifest)
