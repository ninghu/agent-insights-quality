from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_insights_quality.catalogs import catalog_hashes, load_catalogs
from agent_insights_quality.provisioning import (
    create_promotion_receipt,
    validate_promotion_receipt,
)
from agent_insights_quality.util import ContractError
from agent_insights_quality.util import content_hash


def test_daily_promotion_requires_reviewed_staging_digest(tmp_path: Path) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    digests = {
        f"{agent['name']}/{logical}": "sha256:" + f"{index + 1:064x}"
        for agent in agents["agents"]
        for index, logical in enumerate(["v0", *agent["issue_ids"]])
    }
    receipt = {
        "schema_version": "1.0.0",
        "profile": "staging",
        "qualified": True,
        "human_reviewed": True,
        "qualification_status": "FAIL",
        "quality_score": 47.1,
        "catalog_hashes": hashes,
        "artifact_manifest_hash": hashes["artifacts"],
        "version_content_digests": digests,
        "deployment_manifest_hash": content_hash(digests),
        "report_reference": "sha256:" + "a" * 64,
    }
    path = tmp_path / "promotion.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    assert validate_promotion_receipt(path, hashes) == digests
    changed = dict(hashes)
    changed["issues"] = "sha256:" + "b" * 64
    with pytest.raises(ContractError, match="stale"):
        validate_promotion_receipt(path, changed)


def test_promotion_receipt_binds_all_staging_versions() -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    registry = {
        "schema_version": "1.0.0",
        "profile": "staging",
        "project_name": "agent-insights-quality-staging",
        "catalog_hashes": hashes,
        "agents": {
            agent["name"]: {
                "monitor_id": f"monitor-{agent['name']}",
                "versions": {
                    logical: {
                        "foundry_version": f"{index + 1}",
                        "content_digest": "sha256:" + f"{index + 1:064x}",
                    }
                    for index, logical in enumerate(["v0", *agent["issue_ids"]])
                }
            }
            for agent in agents["agents"]
        },
    }
    manifest = {
        "schema_version": "1.0.0",
        "run_id": "aiq-20260824",
        "profile": "staging",
        "report_date": "2026-08-24",
        "catalog_hashes": hashes,
        "agents": [
            {
                "name": agent["name"],
                "baseline": {
                    "logical_version": "v0",
                    "foundry_version": registry["agents"][agent["name"]]["versions"][
                        "v0"
                    ]["foundry_version"],
                },
                "issues": [
                    {
                        "issue_id": issue_id,
                        "logical_version": issue_id,
                        "foundry_version": registry["agents"][agent["name"]]["versions"][
                            issue_id
                        ]["foundry_version"],
                    }
                    for issue_id in agent["issue_ids"]
                ],
            }
            for agent in agents["agents"]
        ],
        "manifest_hash": "",
    }
    manifest["manifest_hash"] = content_hash(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    )
    issue_by_id = {item["id"]: item for item in issues["issues"]}
    fields = {
        "root_cause": True,
        "title": True,
        "description": True,
        "category": True,
        "severity": True,
        "proposed_fix": True,
        "linked_traces": True,
    }
    report = {
        "schema_version": "1.0.0",
        "report_date": "2026-08-24",
        "run_id": "aiq-20260824",
        "profile": "staging",
        "manifest_reference": manifest["manifest_hash"],
        "status": "PASS",
        "catalog_hashes": hashes,
        "baseline": [
            {
                "agent": agent["name"],
                "status": "passed",
                "runtime_evidence_complete": True,
                "insight_count": 0,
                "assessment": {
                    "verdict": "clean",
                    "ownership": "none",
                    "finding_type": "MATCHED",
                    "ownership_reason": "No baseline Insight was observed.",
                    "confidence": 0.99,
                },
            }
            for agent in agents["agents"]
        ],
        "issues": [
            {
                "issue_id": issue["id"],
                "agent": issue["agent"],
                "title": issue["title"],
                "status": "observed",
                "runtime_evidence_complete": True,
                "result": "PASS",
                "detail": "MATCHED",
                "observed_count": 1,
                "assessment": {
                    "verdict": "correct",
                    "finding_type": "MATCHED",
                    "confidence": 0.99,
                    "ownership": "none",
                    "ownership_reason": "The expected Insight is fully correct.",
                    "fields": fields,
                },
                "evidence_reference": "sha256:" + "a" * 64,
            }
            for issue in issue_by_id.values()
        ],
        "summary": {
            "issues_expected": 36,
            "issues_correct": 36,
            "issues_partial": 0,
            "baseline_passed": 5,
            "quality_failures": 0,
            "incomplete": False,
            "noise_cards": 0,
            "unverified_cards": 0,
            "observed_cards": 36,
            "field_quality_score": 100,
            "clean_card_precision": 100,
            "quality_score": 100,
            "quality_threshold": 90,
            "quality_score_formula": "field_weighted_v1",
            "incomplete_reasons": [],
        },
        "delivery": {"content_digest": "sha256:" + "0" * 64},
    }
    receipt = create_promotion_receipt(
        report=report,
        registry=registry,
        manifest=manifest,
        issue_catalog=issues,
        human_reviewed=True,
    )
    assert len(receipt["version_content_digests"]) == 41
