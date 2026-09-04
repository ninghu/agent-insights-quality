from __future__ import annotations

from datetime import date
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from agent_insights_quality.catalogs import (
    catalog_hashes as current_catalog_hashes,
    load_catalogs,
    source_integrity_digest,
)
from agent_insights_quality.models import AgentResult, request_completion_payload
from agent_insights_quality.azure_regions import regions_match
from agent_insights_quality.registry import version_entry
from agent_insights_quality.selection import select_daily
from agent_insights_quality.util import ROOT, ContractError, content_hash, read_json
from agent_insights_quality.validation_rules import (
    execution_context,
    issue_observation_context,
)
from agent_insights_quality.validation_trace_gap_policy import (
    daily_target_decision,
    validate_trace_maturity_proof,
)

OFFICIAL_DELIVERY = "official"
TEST_EMAIL_ONLY_DELIVERY = "test_email_only"


def run_id(report_date: date, rerun: int = 0) -> str:
    base = f"aiq-{report_date:%Y%m%d}"
    return f"{base}-r{rerun:02d}" if rerun else base


def build_manifest(
    *,
    report_date: date,
    profile: str,
    rerun: int,
    delivery_mode: str,
    insight_lookback_hours: float,
    telemetry_resource_set: str,
    test_region: str,
    test_region_registry: str,
    catalog_hashes: dict[str, str],
    agent_catalog: dict[str, Any],
    issue_catalog: dict[str, Any],
    selected: dict[str, list[str]],
    registry: dict[str, Any],
    results: list[AgentResult],
) -> dict[str, Any]:
    result_by_agent = {item.agent_name: item for item in results}
    contract_by_agent = {
        item["name"]: item for item in agent_catalog["agents"]
    }
    issue_by_id = {item["id"]: item for item in issue_catalog["issues"]}
    agents: list[dict[str, Any]] = []
    for agent_name in selected:
        result = result_by_agent[agent_name]
        baseline_registry = version_entry(registry, agent_name, "v0")
        agents.append(
            {
                "name": agent_name,
                "type": contract_by_agent[agent_name]["type"],
                "framework": contract_by_agent[agent_name]["framework"],
                "baseline_contract": contract_by_agent[agent_name][
                    "baseline_contract"
                ],
                "monitor_reference": content_hash(
                    {"monitor_id": registry["agents"][agent_name]["monitor_id"]}
                ),
                "baseline": {
                    "logical_version": "v0",
                    "foundry_version": baseline_registry["foundry_version"],
                    "content_digest": baseline_registry["content_digest"],
                    **execution_context(
                        ROOT
                        / contract_by_agent[agent_name]["baseline_path"]
                        / "traffic.json"
                    ),
                    **_result_payload(result.baseline),
                },
                "issues": [
                    {
                        "issue_id": issue_id,
                        "logical_version": issue_id,
                        **version_entry(registry, agent_name, issue_id),
                        **issue_observation_context(
                            ROOT
                            / issue_by_id[issue_id]["implementation"]
                            / "traffic.json"
                        ),
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
        "schema_version": "6.0.0",
        "run_id": run_id(report_date, rerun),
        "profile": profile,
        "delivery_mode": delivery_mode,
        "report_date": report_date.isoformat(),
        "insight_lookback_hours": insight_lookback_hours,
        "telemetry_resource_set": telemetry_resource_set,
        "test_region": test_region,
        "test_region_registry": test_region_registry,
        "catalog_hashes": catalog_hashes,
        "source_integrity": {
            "verified": True,
            "contract_digest": source_integrity_digest(
                agent_catalog,
                issue_catalog,
            ),
        },
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
        "trace_assertion_count": result.trace_assertion_count,
        "trace_assertions_passed": result.trace_assertions_passed,
        "trace_contract_verified": result.trace_contract_verified,
        "trace_behavior_summary": result.trace_behavior_summary,
        "trace_maturity_proof": result.trace_maturity_proof,
        "role_pass_summary": result.role_pass_summary,
        "endpoint_request_summaries": [
            request_completion_payload(item)
            for item in result.endpoint_request_summaries
        ],
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
    if not regions_match(
        manifest["test_region"],
        manifest["test_region_registry"],
    ):
        raise ContractError(
            "Run manifest live Project region does not match the registry cross-check"
        )
    expected_agents = {
        "weather-agent",
        "healthcare-agent",
        "finance-agent",
        "travel-agent",
        "support-ticket-agent",
    }
    if {agent["name"] for agent in manifest["agents"]} != expected_agents:
        raise ContractError("Run manifest Agent identities are inconsistent")
    current_agents, current_issues = load_catalogs(require_paths=False)
    hashes = current_catalog_hashes(current_agents, current_issues)
    if manifest["catalog_hashes"] != hashes:
        raise ContractError("Run manifest catalog hashes are not current")
    if (
        manifest["source_integrity"]["contract_digest"]
        != source_integrity_digest(current_agents, current_issues)
    ):
        raise ContractError("Run manifest source integrity is not current")
    current_by_name = {
        agent["name"]: agent for agent in current_agents["agents"]
    }
    for agent in manifest["agents"]:
        current = current_by_name[agent["name"]]
        if (
            agent["type"] != current["type"]
            or agent["framework"] != current["framework"]
            or agent["baseline_contract"] != current["baseline_contract"]
        ):
            raise ContractError(
                f"{agent['name']} manifest contract is not current"
            )
        if (
            agent["baseline"]["logical_version"] != "v0"
            or "issue_id" in agent["baseline"]
        ):
            raise ContractError("Run manifest baseline identity is inconsistent")
        _validate_version_evidence(
            agent["baseline"],
            f"{agent['name']}/v0",
        )
        issue_ids = [issue["issue_id"] for issue in agent["issues"]]
        if len(issue_ids) != len(set(issue_ids)):
            raise ContractError("Run manifest issue identities are duplicated")
        for issue in agent["issues"]:
            if issue["issue_id"] != issue["logical_version"]:
                raise ContractError("Run manifest issue identity is inconsistent")
            _validate_version_evidence(
                issue,
                f"{agent['name']}/{issue['issue_id']}",
            )
        expected_contexts = {
            "v0": execution_context(
                ROOT / current["baseline_path"] / "traffic.json"
            ),
            **{
                issue_id: issue_observation_context(
                    ROOT
                    / next(
                        item["implementation"]
                        for item in current_issues["issues"]
                        if item["id"] == issue_id
                    )
                    / "traffic.json"
                )
                for issue_id in current["issue_ids"]
            },
        }
        for version in [agent["baseline"], *agent["issues"]]:
            logical_version = version["logical_version"]
            if {
                key: version[key]
                for key in expected_contexts[logical_version]
            } != expected_contexts[logical_version]:
                raise ContractError(
                    f"{agent['name']}/{logical_version} manifest execution "
                    "contract is not current"
                )
            observations = [
                item
                for item in version["endpoint_request_summaries"]
                if item["activation_gate"] is True
            ]
            summary_value = version.get("role_pass_summary")
            validate_trace_maturity_proof(version.get("trace_maturity_proof"))
            _, summary = daily_target_decision(
                target_role=(
                    "baseline" if logical_version == "v0" else "issue"
                ),
                validation_mode=str(version["validation_mode"]),
                n=int(version["n"]),
                k=int(version["k"]),
                required_surfaces=(
                    ["semantic", "trace"]
                    if logical_version == "v0"
                    else version["required_surfaces"]
                ),
                summaries=observations,
                identity_verified=version["trace_contract_verified"] is True,
            )
            if summary_value != summary:
                raise ContractError(
                    f"{agent['name']}/{logical_version} role-pass summary "
                    "is not current"
                )
    actual_selection = {
        agent["name"]: [issue["issue_id"] for issue in agent["issues"]]
        for agent in manifest["agents"]
    }
    expected_selection = (
        {
            agent["name"]: list(agent["issue_ids"])
            for agent in current_agents["agents"]
        }
        if manifest["profile"] == "staging"
        else select_daily(
            date.fromisoformat(manifest["report_date"]),
            current_agents,
            current_issues,
            hashes["issues"],
        )
    )
    if actual_selection != expected_selection:
        raise ContractError("Run manifest issue inventory is not current")
    if manifest["delivery_mode"] == TEST_EMAIL_ONLY_DELIVERY and (
        manifest["profile"] != "daily" or "-r" not in manifest["run_id"]
    ):
        raise ContractError(
            "Test email-only delivery requires a daily nonzero rerun identity"
        )
    expected = content_hash(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    )
    if manifest["manifest_hash"] != expected:
        raise ContractError("Run manifest hash does not match its content")


def _validate_version_evidence(value: dict[str, Any], label: str) -> None:
    summaries = value["endpoint_request_summaries"]
    if len(summaries) != value["endpoint_request_count"] or [
        item["request_index"] for item in summaries
    ] != list(range(len(summaries))):
        raise ContractError(f"{label} request summary coverage is inconsistent")
    if value["semantic_assertions_passed"] > value["semantic_assertion_count"]:
        raise ContractError(f"{label} semantic assertion totals are inconsistent")
    if value["trace_assertions_passed"] > value["trace_assertion_count"]:
        raise ContractError(f"{label} trace assertion totals are inconsistent")
    response_count = 0
    usable_count = 0
    assertion_count = 0
    assertions_passed = 0
    trace_assertion_count = 0
    trace_assertions_passed = 0
    for item in summaries:
        results = item["assertion_results"]
        trace_results = item["trace_assertion_results"]
        if (
            len(results) != item["semantic_assertion_count"]
            or sum(result["passed"] for result in results)
            != item["semantic_assertions_passed"]
            or item["semantic_assertions_passed"]
            > item["semantic_assertion_count"]
            or any(
                result.get("evidence_sufficient") not in {True, False}
                for result in results
            )
        ):
            raise ContractError(f"{label} request assertion evidence is inconsistent")
        if (
            len(trace_results) != item["trace_assertion_count"]
            or sum(result["passed"] for result in trace_results)
            != item["trace_assertions_passed"]
            or item["trace_assertions_passed"] > item["trace_assertion_count"]
            or any(
                result.get("evidence_sufficient") not in {True, False}
                for result in trace_results
            )
            or item.get("error_code")
            not in {None, "missing_evidence", "assertion_failed"}
        ):
            raise ContractError(
                f"{label} request trace assertion evidence is inconsistent"
            )
        response_count += item["response_count"]
        usable_count += int(item["usable_response"])
        assertion_count += item["semantic_assertion_count"]
        assertions_passed += item["semantic_assertions_passed"]
        trace_assertion_count += item["trace_assertion_count"]
        trace_assertions_passed += item["trace_assertions_passed"]
    if (
        response_count != value["endpoint_response_count"]
        or usable_count != value["endpoint_usable_response_count"]
        or assertion_count != value["semantic_assertion_count"]
        or assertions_passed != value["semantic_assertions_passed"]
        or trace_assertion_count != value["trace_assertion_count"]
        or trace_assertions_passed != value["trace_assertions_passed"]
    ):
        raise ContractError(f"{label} aggregate endpoint evidence is inconsistent")
    if value["trace_contract_verified"] and not value["operation_ids"]:
        raise ContractError(f"{label} verified trace evidence has no operations")
    trace_operation_count = value["trace_behavior_summary"].get(
        "operation_count"
    )
    if (
        trace_operation_count is not None
        and trace_operation_count != len(value["operation_ids"])
    ):
        raise ContractError(f"{label} trace operation count is inconsistent")
    if (value["window_start"] is None) != (value["window_end"] is None):
        raise ContractError(f"{label} operation window is incomplete")
    if value["status"] == "passed" and value["insight_references"]:
        raise ContractError(f"{label} passed baseline contains Insights")
    if value["status"] == "observed" and (
        len(value["insight_references"]) != 1
        or value["evidence_reference"] is None
    ):
        raise ContractError(f"{label} observed Insight evidence is inconsistent")
