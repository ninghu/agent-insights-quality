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
    TraceAssertionEvidence,
    VersionResult,
)
from agent_insights_quality.live import (
    _normalize_fixture,
    _semantic_assertion_names,
    _trace_assertion_names,
)
from agent_insights_quality.run_manifest import _result_payload, build_manifest
from agent_insights_quality.util import ROOT, ContractError, content_hash, read_json


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
            "trace_assertion_count": 0,
            "trace_assertions_passed": 0,
            "trace_assertion_results": [],
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
    traffic_path: Path | None = None,
) -> dict:
    prompt = agent_type == "prompt"
    if traffic_path is None:
        summaries = _request_summaries(prompt=prompt)
    else:
        summaries = []
        for index, raw in enumerate(read_json(traffic_path)["requests"]):
            fixture = _normalize_fixture(raw)
            names = _semantic_assertion_names(fixture["semantic_assertions"])
            trace_names = _trace_assertion_names(fixture["trace_assertions"])
            summaries.append(
                {
                    "request_index": index,
                    "response_count": 1,
                    "usable_response": True,
                    "semantic_assertion_count": len(names),
                    "semantic_assertions_passed": len(names),
                    "assertion_results": [
                        {"assertion": name, "passed": True} for name in names
                    ],
                    "trace_assertion_count": len(trace_names),
                    "trace_assertions_passed": len(trace_names),
                    "trace_assertion_results": [
                        {"assertion": name, "passed": True}
                        for name in trace_names
                    ],
                    "activation_gate": fixture["activation_gate"],
                    "direct_terminal_response_count": int(prompt),
                    "function_call_count": 0,
                }
            )
    request_count = len(summaries)
    return {
        "logical_version": logical_version,
        "foundry_version": foundry_version,
        "content_digest": "sha256:" + "a" * 64,
        "status": "passed" if logical_version == "v0" else "observed",
        "operation_ids": [f"{index + 1:032x}" for index in range(request_count)],
        "insight_references": (
            [] if logical_version == "v0" else ["sha256:" + "b" * 64]
        ),
        "window_start": "2026-08-28T10:00:00+00:00",
        "window_end": "2026-08-28T10:01:00+00:00",
        "error_code": None,
        "endpoint_request_count": request_count,
        "endpoint_response_count": request_count,
        "endpoint_usable_response_count": request_count,
        "semantic_assertion_count": sum(
            item["semantic_assertion_count"] for item in summaries
        ),
        "semantic_assertions_passed": sum(
            item["semantic_assertions_passed"] for item in summaries
        ),
        "trace_assertion_count": sum(
            item["trace_assertion_count"] for item in summaries
        ),
        "trace_assertions_passed": sum(
            item["trace_assertions_passed"] for item in summaries
        ),
        "trace_contract_verified": True,
        "trace_behavior_summary": {
            "operation_count": request_count,
            "tool_call_counts": {},
            "tool_response_count": 0,
            "assistant_response_count": request_count,
            "explicit_terminal_success_count": request_count,
            "explicit_terminal_output_count": request_count,
            "terminal_response_count": request_count,
            "terminal_output_count": request_count,
            "unhandled_error_count": 0,
        },
        "endpoint_request_summaries": summaries,
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
                trace_assertion_count=1,
                trace_assertions_passed=1,
                trace_assertion_results=(
                    TraceAssertionEvidence("tool_scope_mismatch", True),
                ),
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
    trace_assertions = payload["endpoint_request_summaries"][0][
        "trace_assertion_results"
    ]
    assert trace_assertions == [
        {"assertion": "tool_scope_mismatch", "passed": True}
    ]
    serialized = json.dumps(payload, sort_keys=True)
    assert "private-argument" not in serialized
    assert "private-response" not in serialized


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
            issues=[
                VersionResult(
                    logical_version=issue_id,
                    foundry_version="1",
                    status="skipped_baseline",
                )
                for issue_id in agent["issue_ids"]
            ],
        )
        for agent in agents["agents"]
    ]
    registry = {
        "agents": {
            agent["name"]: {
                "monitor_id": f"monitor-{agent['name']}",
                "versions": {
                    logical_version: {
                        "foundry_version": "1",
                        "content_digest": "sha256:" + "a" * 64,
                    }
                    for logical_version in ["v0", *agent["issue_ids"]]
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
        selected={
            agent["name"]: list(agent["issue_ids"])
            for agent in agents["agents"]
        },
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
    issue_by_id = {item["id"]: item for item in issues["issues"]}
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
                "framework": agent["framework"],
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
                            traffic_path=(
                                ROOT
                                / issue_by_id[issue_id]["implementation"]
                                / "traffic.json"
                            ),
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
        "schema_version": "2.0.0",
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
                    "card_evaluations": [],
                },
                "evidence_reference": "sha256:" + "c" * 64,
                "shadow_v2_primary": None,
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
            "shadow_quality_score": {
                "formula": "coverage_quality_precision_v2",
                "automation_authority": False,
                "counts": {
                    "expected_issues": 36,
                    "detected_issues": 0,
                    "correct_diagnosis_primaries": 0,
                    "generated_issue_cards": 0,
                    "baseline_noise_cards": 0,
                },
                "components": {
                    "coverage": 0.0,
                    "diagnosis_recall": 0.0,
                    "selected_card_quality": 0.0,
                    "useful_coverage": 0.0,
                    "precision": None,
                },
                "score": 0.0,
                "gate_failures": [
                    "score",
                    "coverage",
                    "diagnosis_recall",
                    "precision",
                ],
            },
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
    assert report["summary"]["shadow_quality_score"]["score"] == 0.0
    assert receipt["qualification_status"] == "PASS"
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
    hosted_manifest = deepcopy(manifest)
    travel = next(
        agent
        for agent in hosted_manifest["agents"]
        if agent["name"] == "travel-agent"
    )
    switch_issue = next(
        issue for issue in travel["issues"] if issue["issue_id"] == "issue-028"
    )
    switch_summary = next(
        summary
        for summary in switch_issue["endpoint_request_summaries"]
        if summary["activation_gate"]
    )
    switch_summary["activation_gate"] = False
    hosted_manifest["manifest_hash"] = content_hash(
        {
            key: value
            for key, value in hosted_manifest.items()
            if key != "manifest_hash"
        }
    )
    hosted_report = deepcopy(report)
    hosted_report["manifest_reference"] = hosted_manifest["manifest_hash"]
    with pytest.raises(ContractError, match="authoritative traffic"):
        create_promotion_receipt(
            report=hosted_report,
            registry=registry,
            manifest=hosted_manifest,
            issue_catalog=issues,
            human_reviewed=True,
        )
