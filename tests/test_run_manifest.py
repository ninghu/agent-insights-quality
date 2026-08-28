from __future__ import annotations

from copy import deepcopy

import pytest

from agent_insights_quality.run_manifest import (
    OFFICIAL_DELIVERY,
    TEST_EMAIL_ONLY_DELIVERY,
    validate_manifest,
)
from agent_insights_quality.util import ContractError, content_hash


def _manifest(
    *,
    run_id: str = "aiq-20260828",
    profile: str = "daily",
    delivery_mode: str = OFFICIAL_DELIVERY,
) -> dict:
    version = {
        "logical_version": "v0",
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
    agent_contracts = [
        ("weather-agent", "prompt", "direct_prompt", "forbidden"),
        ("healthcare-agent", "prompt", "direct_prompt", "forbidden"),
        (
            "finance-agent",
            "hosted_code",
            "standard_assistant_message",
            "not_applicable",
        ),
        (
            "travel-agent",
            "hosted_code",
            "standard_assistant_message",
            "not_applicable",
        ),
        (
            "support-ticket-agent",
            "hosted_custom_container",
            "explicit_span_attributes",
            "not_applicable",
        ),
    ]
    value = {
        "schema_version": "4.0.0",
        "run_id": run_id,
        "profile": profile,
        "delivery_mode": delivery_mode,
        "report_date": "2026-08-28",
        "insight_lookback_hours": 0.1,
        "telemetry_resource_set": "g29",
        "catalog_hashes": {
            "agents": "sha256:" + "b" * 64,
            "issues": "sha256:" + "c" * 64,
            "artifacts": "sha256:" + "d" * 64,
        },
        "source_integrity": {
            "verified": True,
            "contract_digest": "sha256:" + "e" * 64,
        },
        "agents": [
            {
                "name": name,
                "type": agent_type,
                "baseline_contract": {
                    "request_count": 5,
                    "terminal_response": terminal,
                    "semantic_assertions": (
                        "required_per_request"
                        if agent_type == "prompt"
                        else "required"
                    ),
                    "function_calling": function_calling,
                },
                "monitor_reference": "sha256:" + "f" * 64,
                "baseline": dict(version),
                "issues": [],
            }
            for name, agent_type, terminal, function_calling in agent_contracts
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
