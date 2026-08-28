from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

from agent_insights_quality.catalogs import (
    catalog_hashes,
    load_catalogs,
    agent_model_contract,
    source_integrity_digest,
)
from agent_insights_quality.provisioning import (
    create_promotion_receipt,
    validate_promotion_receipt,
)
from agent_insights_quality.models import (
    AgentResult,
    RequestCompletionEvidence,
    SemanticAssertionEvidence,
    VersionResult,
)
from agent_insights_quality.run_manifest import _result_payload, build_manifest
from agent_insights_quality.util import ContractError
from agent_insights_quality.util import content_hash


def _request_summaries(
    *,
    prompt: bool = False,
    activation: bool = False,
) -> list[dict]:
    return [
        {
            "request_index": index,
            "response_count": 1,
            "usable_response": True,
            "semantic_assertion_count": 1,
            "semantic_assertions_passed": 1,
            "assertion_results": [
                {"assertion": "synthetic_contract", "passed": True}
            ],
            "activation_gate": activation,
            "direct_terminal_response_count": int(prompt),
            "function_call_count": 0,
        }
        for index in range(5)
    ]


def _version_evidence(
    logical_version: str,
    foundry_version: str,
    *,
    agent_type: str,
) -> dict:
    prompt = agent_type == "prompt"
    return {
        "logical_version": logical_version,
        "foundry_version": foundry_version,
        "content_digest": "sha256:" + "a" * 64,
        "status": "passed" if logical_version == "v0" else "observed",
        "operation_ids": [f"{index + 1:032x}" for index in range(5)],
        "insight_references": (
            [] if logical_version == "v0" else ["sha256:" + "b" * 64]
        ),
        "window_start": "2026-08-28T10:00:00+00:00",
        "window_end": "2026-08-28T10:01:00+00:00",
        "error_code": None,
        "endpoint_request_count": 5,
        "endpoint_response_count": 5,
        "endpoint_usable_response_count": 5,
        "semantic_assertion_count": 5,
        "semantic_assertions_passed": 5,
        "trace_contract_verified": True,
        "trace_behavior_summary": {
            "operation_count": 5,
            "tool_call_counts": {},
            "tool_response_count": 0,
            "assistant_response_count": 5,
            "explicit_terminal_success_count": 5,
            "explicit_terminal_output_count": 5,
            "terminal_response_count": 5,
            "terminal_output_count": 5,
            "unhandled_error_count": 0,
        },
        "endpoint_request_summaries": _request_summaries(
            prompt=prompt,
            activation=prompt and logical_version != "v0",
        ),
        "evidence_reference": (
            None if logical_version == "v0" else "sha256:" + "c" * 64
        ),
    }


def test_manifest_request_assertions_are_json_arrays() -> None:
    result = VersionResult(
        logical_version="v0",
        foundry_version="1",
        status="passed",
        endpoint_request_summaries=[
            RequestCompletionEvidence(
                request_index=0,
                response_count=1,
                usable_response=True,
                semantic_assertion_count=1,
                semantic_assertions_passed=1,
                assertion_results=(
                    SemanticAssertionEvidence("synthetic_contract", True),
                ),
                activation_gate=False,
                direct_terminal_response_count=1,
                function_call_count=0,
            )
        ],
    )
    payload = _result_payload(result)
    assertions = payload["endpoint_request_summaries"][0][
        "assertion_results"
    ]
    assert assertions == [
        {"assertion": "synthetic_contract", "passed": True}
    ]
    assert isinstance(assertions, list)


def test_build_manifest_validates_real_nested_evidence() -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    summary = RequestCompletionEvidence(
        request_index=0,
        response_count=1,
        usable_response=True,
        semantic_assertion_count=1,
        semantic_assertions_passed=1,
        assertion_results=(
            SemanticAssertionEvidence("synthetic_contract", True),
        ),
        activation_gate=False,
        direct_terminal_response_count=1,
        function_call_count=0,
    )
    results = [
        AgentResult(
            agent_name=agent["name"],
            baseline=VersionResult(
                logical_version="v0",
                foundry_version="1",
                status="passed",
                endpoint_request_count=1,
                endpoint_response_count=1,
                endpoint_usable_response_count=1,
                semantic_assertion_count=1,
                semantic_assertions_passed=1,
                trace_contract_verified=True,
                operation_ids=["a" * 32],
                window_start="2026-08-28T10:00:00+00:00",
                window_end="2026-08-28T10:01:00+00:00",
                endpoint_request_summaries=[summary],
            ),
            issues=[],
        )
        for agent in agents["agents"]
    ]
    registry = {
        "agents": {
            agent["name"]: {
                "monitor_id": f"monitor-{agent['name']}",
                "versions": {
                    "v0": {
                        "foundry_version": "1",
                        "content_digest": "sha256:" + "a" * 64,
                    }
                },
            }
            for agent in agents["agents"]
        }
    }
    manifest = build_manifest(
        report_date=date(2026, 8, 28),
        profile="staging",
        rerun=0,
        delivery_mode="official",
        insight_lookback_hours=0.1,
        telemetry_resource_set="g29",
        catalog_hashes=hashes,
        agent_catalog=agents,
        issue_catalog=issues,
        selected={agent["name"]: [] for agent in agents["agents"]},
        registry=registry,
        results=results,
    )
    assertions = manifest["agents"][0]["baseline"][
        "endpoint_request_summaries"
    ][0]["assertion_results"]
    assert isinstance(assertions, list)
    assert (
        manifest["source_integrity"]["contract_digest"]
        == source_integrity_digest(agents, issues)
    )


def test_daily_promotion_requires_reviewed_staging_digest(tmp_path: Path) -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    model = agent_model_contract(agents)
    digests = {
        f"{agent['name']}/{logical}": "sha256:" + f"{index + 1:064x}"
        for agent in agents["agents"]
        for index, logical in enumerate(["v0", *agent["issue_ids"]])
    }
    receipt = {
        "schema_version": "2.0.0",
        "profile": "staging",
        "qualified": True,
        "human_reviewed": True,
        "qualification_status": "FAIL",
        "quality_score": 47.1,
        "test_agent_model": model,
        "catalog_hashes": hashes,
        "artifact_manifest_hash": hashes["artifacts"],
        "version_content_digests": digests,
        "deployment_manifest_hash": content_hash(digests),
        "qualification_manifest_hash": "sha256:" + "c" * 64,
        "source_integrity_digest": source_integrity_digest(agents, issues),
        "report_reference": "sha256:" + "a" * 64,
    }
    path = tmp_path / "promotion.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    assert validate_promotion_receipt(path, hashes, model) == digests
    changed = dict(hashes)
    changed["issues"] = "sha256:" + "b" * 64
    with pytest.raises(ContractError, match="stale"):
        validate_promotion_receipt(path, changed, model)


def test_promotion_receipt_binds_all_staging_versions() -> None:
    agents, issues = load_catalogs()
    hashes = catalog_hashes(agents, issues)
    model = agent_model_contract(agents)
    registry = {
        "schema_version": "1.0.0",
        "profile": "staging",
        "project_name": "agent-insights-quality-staging",
        "test_agent_model": model,
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
        "schema_version": "4.0.0",
        "run_id": "aiq-20260824",
        "profile": "staging",
        "delivery_mode": "official",
        "report_date": "2026-08-24",
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
                "baseline": {
                    **_version_evidence(
                        "v0",
                        registry["agents"][agent["name"]]["versions"]["v0"][
                            "foundry_version"
                        ],
                        agent_type=agent["type"],
                    ),
                },
                "issues": [
                    {
                        "issue_id": issue_id,
                        **_version_evidence(
                            issue_id,
                            registry["agents"][agent["name"]]["versions"][
                                issue_id
                            ]["foundry_version"],
                            agent_type=agent["type"],
                        ),
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
        "source_integrity": manifest["source_integrity"],
        "baseline": [
            {
                "agent": agent["name"],
                "logical_version": "v0",
                "foundry_version": registry["agents"][agent["name"]][
                    "versions"
                ]["v0"]["foundry_version"],
                "status": "passed",
                "runtime_evidence_complete": True,
                "insight_count": 0,
                "assessment": {
                    "verdict": "clean",
                    "ownership": "none",
                    "finding_type": "MATCHED",
                    "ownership_reason": "No baseline Insight was observed.",
                    "confidence": 0.99,
                    "card_evaluations": [],
                },
            }
            for agent in agents["agents"]
        ],
        "issues": [
            {
                "issue_id": issue["id"],
                "agent": issue["agent"],
                "logical_version": issue["id"],
                "foundry_version": registry["agents"][issue["agent"]][
                    "versions"
                ][issue["id"]]["foundry_version"],
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
                "evidence_reference": "sha256:" + "c" * 64,
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
    assert receipt["qualification_manifest_hash"] == manifest["manifest_hash"]
    assert (
        receipt["source_integrity_digest"]
        == manifest["source_integrity"]["contract_digest"]
    )
    incomplete_manifest = deepcopy(manifest)
    first_issue = incomplete_manifest["agents"][0]["issues"][0]
    for summary in first_issue["endpoint_request_summaries"]:
        summary["activation_gate"] = False
    incomplete_manifest["manifest_hash"] = content_hash(
        {
            key: value
            for key, value in incomplete_manifest.items()
            if key != "manifest_hash"
        }
    )
    incomplete_report = deepcopy(report)
    incomplete_report["manifest_reference"] = incomplete_manifest["manifest_hash"]
    with pytest.raises(ContractError, match="activation evidence"):
        create_promotion_receipt(
            report=incomplete_report,
            registry=registry,
            manifest=incomplete_manifest,
            issue_catalog=issues,
            human_reviewed=True,
        )
