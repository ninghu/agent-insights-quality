from __future__ import annotations

from datetime import date
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from agent_insights_quality.models import AgentResult
from agent_insights_quality.registry import version_entry
from agent_insights_quality.util import ROOT, ContractError, content_hash, read_json


def run_id(report_date: date, rerun: int = 0) -> str:
    base = f"aiq-{report_date:%Y%m%d}"
    return f"{base}-r{rerun:02d}" if rerun else base


def build_manifest(
    *,
    report_date: date,
    profile: str,
    rerun: int,
    insight_lookback_hours: float,
    telemetry_resource_set: str,
    catalog_hashes: dict[str, str],
    selected: dict[str, list[str]],
    registry: dict[str, Any],
    results: list[AgentResult],
) -> dict[str, Any]:
    result_by_agent = {item.agent_name: item for item in results}
    agents: list[dict[str, Any]] = []
    for agent_name in selected:
        result = result_by_agent[agent_name]
        baseline_registry = version_entry(registry, agent_name, "v0")
        agents.append(
            {
                "name": agent_name,
                "monitor_reference": content_hash(
                    {"monitor_id": registry["agents"][agent_name]["monitor_id"]}
                ),
                "baseline": {
                    "logical_version": "v0",
                    "foundry_version": baseline_registry["foundry_version"],
                    "content_digest": baseline_registry["content_digest"],
                    **_result_payload(result.baseline),
                },
                "issues": [
                    {
                        "issue_id": issue_id,
                        "logical_version": issue_id,
                        **version_entry(registry, agent_name, issue_id),
                        **_result_payload(issue_result),
                    }
                    for issue_id, issue_result in zip(
                        selected[agent_name],
                        result.issues,
                        strict=True,
                    )
                ],
            }
        )
    manifest: dict[str, Any] = {
        "schema_version": "2.0.0",
        "run_id": run_id(report_date, rerun),
        "profile": profile,
        "report_date": report_date.isoformat(),
        "insight_lookback_hours": insight_lookback_hours,
        "telemetry_resource_set": telemetry_resource_set,
        "catalog_hashes": catalog_hashes,
        "agents": agents,
        "manifest_hash": "",
    }
    manifest["manifest_hash"] = content_hash(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    )
    validate_manifest(manifest)
    return manifest


def _result_payload(result: Any) -> dict[str, Any]:
    return {
        "status": result.status,
        "operation_ids": result.operation_ids,
        "insight_references": result.insight_references,
        "window_start": result.window_start,
        "window_end": result.window_end,
        "error_code": result.error_code,
        "endpoint_request_count": result.endpoint_request_count,
        "endpoint_response_count": result.endpoint_response_count,
        "endpoint_usable_response_count": result.endpoint_usable_response_count,
        "semantic_assertion_count": result.semantic_assertion_count,
        "semantic_assertions_passed": result.semantic_assertions_passed,
        "trace_contract_verified": result.trace_contract_verified,
        "evidence_reference": (
            content_hash(
                {
                    "agent_version": result.observed_insight.agent_version,
                    "category": result.observed_insight.category,
                    "severity": result.observed_insight.severity,
                    "linked_operation_ids": result.observed_insight.linked_operation_ids,
                    "trace_count": result.observed_insight.trace_count,
                }
            )
            if result.observed_insight is not None
            else None
        ),
    }


def validate_manifest(manifest: dict[str, Any]) -> None:
    schema = read_json(ROOT / "schemas" / "run-manifest.schema.json")
    errors = list(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(manifest)
    )
    if errors:
        raise ContractError(f"Run manifest is invalid: {errors[0].message}")
    expected = content_hash(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    )
    if manifest["manifest_hash"] != expected:
        raise ContractError("Run manifest hash does not match its content")
