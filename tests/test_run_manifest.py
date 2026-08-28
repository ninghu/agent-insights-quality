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
from agent_insights_quality.selection import select_daily, select_full
from agent_insights_quality.util import ContractError, content_hash


def _manifest(
    *,
    run_id: str = "aiq-20260828",
    profile: str = "daily",
    delivery_mode: str = OFFICIAL_DELIVERY,
    report_date: str = "2026-08-28",
) -> dict:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    selected = (
        select_daily(
            date.fromisoformat(report_date),
            agents,
            issues,
            hashes["issues"],
        )
        if profile == "daily"
        else select_full(agents)
    )

    def version(logical_version: str) -> dict:
        value = {
            "logical_version": logical_version,
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
            "trace_contract_verified": False,
            "trace_behavior_summary": {},
            "endpoint_request_summaries": [],
            "evidence_reference": None,
        }
        if logical_version != "v0":
            value["issue_id"] = logical_version
        return value

    value = {
        "schema_version": "4.0.0",
        "run_id": run_id,
        "profile": profile,
        "delivery_mode": delivery_mode,
        "report_date": report_date,
        "insight_lookback_hours": 0.1,
        "telemetry_resource_set": "g29",
        "catalog_hashes": hashes,
        "source_integrity": {
            "verified": True,
            "contract_digest": source_integrity_digest(agents, issues),
        },
        "agents": [
            {
                "name": agent["name"],
                "type": agent["type"],
                "baseline_contract": agent["baseline_contract"],
                "monitor_reference": "sha256:" + "f" * 64,
                "baseline": version("v0"),
                "issues": [
                    version(issue_id) for issue_id in selected[agent["name"]]
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


def _rehash(manifest: dict) -> None:
    manifest["manifest_hash"] = content_hash(
        {key: item for key, item in manifest.items() if key != "manifest_hash"}
    )


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
    _rehash(manifest)
    with pytest.raises(ContractError, match="Run manifest is invalid"):
        validate_manifest(manifest)


def test_manifest_rejects_inconsistent_request_aggregates() -> None:
    manifest = _manifest()
    manifest["agents"][0]["baseline"]["endpoint_response_count"] = 1
    _rehash(manifest)
    with pytest.raises(ContractError, match="aggregate endpoint evidence"):
        validate_manifest(manifest)


def test_manifest_rejects_negative_evidence_counts() -> None:
    manifest = _manifest()
    manifest["agents"][0]["baseline"]["endpoint_request_count"] = -1
    _rehash(manifest)
    with pytest.raises(ContractError, match="Run manifest is invalid"):
        validate_manifest(manifest)


def test_prompt_manifest_requires_per_request_assertions() -> None:
    manifest = _manifest()
    manifest["agents"][0]["baseline_contract"]["semantic_assertions"] = "required"
    _rehash(manifest)
    with pytest.raises(ContractError, match="Run manifest is invalid"):
        validate_manifest(manifest)


def test_issue_manifest_requires_issue_identity_and_issue_status() -> None:
    manifest = _manifest()
    manifest["agents"][0]["issues"][0].pop("issue_id")
    _rehash(manifest)
    with pytest.raises(ContractError, match="Run manifest is invalid"):
        validate_manifest(manifest)


def test_manifest_rejects_fabricated_catalog_and_source_hashes() -> None:
    manifest = _manifest()
    manifest["catalog_hashes"]["agents"] = "sha256:" + "0" * 64
    _rehash(manifest)
    with pytest.raises(ContractError, match="catalog hashes"):
        validate_manifest(manifest)

    manifest = _manifest()
    manifest["source_integrity"]["contract_digest"] = "sha256:" + "0" * 64
    _rehash(manifest)
    with pytest.raises(ContractError, match="source integrity"):
        validate_manifest(manifest)


def test_daily_manifest_rejects_zero_or_fabricated_issue_inventory() -> None:
    manifest = _manifest()
    manifest["agents"][0]["issues"] = []
    _rehash(manifest)
    with pytest.raises(ContractError, match="issue selection"):
        validate_manifest(manifest)

    manifest = _manifest()
    agents, _ = load_catalogs()
    selected_ids = {
        issue["issue_id"] for issue in manifest["agents"][0]["issues"]
    }
    omitted = next(
        issue_id
        for issue_id in agents["agents"][0]["issue_ids"]
        if issue_id not in selected_ids
    )
    issue = manifest["agents"][0]["issues"][0]
    issue["issue_id"] = omitted
    issue["logical_version"] = omitted
    _rehash(manifest)
    with pytest.raises(ContractError, match="issue selection"):
        validate_manifest(manifest)


def test_manifest_rejects_issue_reassignment() -> None:
    manifest = _manifest()
    first = manifest["agents"][0]["issues"][0]
    second = manifest["agents"][1]["issues"][0]
    first["issue_id"], second["issue_id"] = second["issue_id"], first["issue_id"]
    first["logical_version"] = first["issue_id"]
    second["logical_version"] = second["issue_id"]
    _rehash(manifest)
    with pytest.raises(ContractError, match="issue selection"):
        validate_manifest(manifest)


def test_staging_manifest_requires_full_issue_inventory() -> None:
    manifest = _manifest(
        run_id="aiq-20260828-r01",
        profile="staging",
    )
    manifest["agents"][-1]["issues"].pop()
    _rehash(manifest)
    with pytest.raises(ContractError, match="issue selection"):
        validate_manifest(manifest)
