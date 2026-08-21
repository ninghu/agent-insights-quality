"""Quality-memory reconciliation (implemented in a later phase)."""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import date, datetime
from typing import Any

from agent_insights_quality.contracts import (
    ContractError,
    MEMORY_SCHEMA,
    ROOT,
    SCHEMAS,
    TRUST_FAILURES,
    load_agent_manifests,
    load_scenario_catalog,
    validate_canonical_report_semantics,
    validate_daily_plan_semantics,
    validate_instance,
    validate_report_plan_binding,
)
from agent_insights_quality.artifact_io import content_hash
from agent_insights_quality.public_safety import require_public_artifact_safe


_REPORT_PATH = re.compile(
    r"^reports/daily/[0-9]{4}/[0-9]{2}/[0-9]{2}"
    r"(?:/aiq-[0-9]{8}-r[0-9]{2})?/report\.md$"
)


def issue_fingerprint(root_cause: str, engine_surface: str, validation_target: str) -> str:
    values = {
        "root_cause": " ".join(root_cause.casefold().split()),
        "engine_surface": " ".join(engine_surface.casefold().split()),
        "validation_target": " ".join(validation_target.casefold().split()),
    }
    if any(not value for value in values.values()):
        raise ContractError("Fingerprint inputs must be non-empty")
    return content_hash(values)


def _merge_set(issue: dict[str, Any], key: str, values: list[str]) -> None:
    issue[key] = sorted(set(issue[key]) | set(values))


def _new_issue(finding: dict[str, Any], report_date: str) -> dict[str, Any]:
    return {
        "fingerprint": finding["fingerprint"],
        "title": finding["title"],
        "description": finding["description"],
        "state": "new",
        "first_seen": report_date,
        "last_seen": report_date,
        "occurrence_count": 1,
        "consecutive_clean_complete_runs": 0,
        "affected_scenarios": sorted(set(finding["affected_scenarios"])),
        "affected_domains": sorted(set(finding["affected_domains"])),
        "affected_agent_types": sorted(set(finding["affected_agent_types"])),
        "engine_builds": sorted(set(finding["engine_builds"])),
        "generator_models": sorted(set(finding["generator_models"])),
        "judge_prompt_versions": sorted(set(finding["judge_prompt_versions"])),
        "last_primary_confidence": finding.get("primary_confidence"),
        "last_verifier_confidence": finding.get("verifier_confidence"),
        "evidence_references": sorted(set(finding["evidence_references"])),
        "ado": {
            "work_item_reference": None,
            "state": None,
            "last_sync": None,
        },
        "resolution_evidence": [],
        "regression_history": [],
    }


def reconcile_memory(
    memory: dict[str, Any],
    findings: list[dict[str, Any]],
    *,
    plan: dict[str, Any],
    report: dict[str, Any],
    run_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Reconcile confirmed findings while preserving immutable fingerprints and history."""
    validate_instance(memory, MEMORY_SCHEMA, "quality memory")
    require_public_artifact_safe(memory, "Quality memory")
    agents = load_agent_manifests()
    catalog = load_scenario_catalog({agent["id"] for agent in agents})
    validate_instance(plan, SCHEMAS / "daily-plan.schema.json", "daily plan")
    validate_daily_plan_semantics(plan, agents, catalog, "daily plan")
    validate_instance(report, SCHEMAS / "canonical-report.schema.json", "canonical report")
    validate_canonical_report_semantics(
        report,
        agents,
        catalog,
        "canonical report",
        expected_scenario_ids={
            assignment["scenario_id"] for assignment in plan["assignments"]
        },
    )
    validate_report_plan_binding(report, plan, "canonical report")
    report_id = report["report_id"]
    report_date = report["report_date"]
    generated_at = report["generated_at"]
    report_path = f"{plan['artifact_directory']}/report.md"
    if not _REPORT_PATH.fullmatch(report_path):
        raise ContractError("Quality memory report path must be a public daily report path")
    require_public_artifact_safe(report, "Canonical report for quality memory")
    for finding in findings:
        require_public_artifact_safe(finding, "Quality memory finding")
    complete = (
        report["scorecard"]["complete"]
        and report["status"] != "INCONCLUSIVE"
        and not (TRUST_FAILURES & set(report["scorecard"]["violations"]))
        and report["scorecard"]["counts"]["active_scenarios"] == len(plan["assignments"])
        and report["scorecard"]["counts"]["completed_scenarios"] == len(plan["assignments"])
        and {item["scenario_id"] for item in report["scenario_results"]}
        == {item["scenario_id"] for item in plan["assignments"]}
        and all(item["completed"] for item in report["scenario_results"])
    )
    stable_report = deepcopy(report)
    stable_report["memory_changes"] = []
    report_reference = content_hash(
        {
            "run_id": run_id,
            "plan": plan,
            "report": stable_report,
            "findings": findings,
        }
    )
    result = deepcopy(memory)
    matching = [
        item
        for item in result["processed_runs"]
        if item["report_id"] == report_id or item["run_id"] == run_id
    ]
    expected_run = {
        "report_id": report_id,
        "run_id": run_id,
        "report_date": report_date,
        "generated_at": generated_at,
        "report_reference": report_reference,
    }
    if matching:
        if len(matching) == 1 and matching[0] == expected_run:
            return result, []
        raise ContractError("report_id and run_id are immutable and cannot be reused")
    if not complete:
        return result, []
    if result["updated_at"] is not None and datetime.fromisoformat(
        generated_at.replace("Z", "+00:00")
    ) <= datetime.fromisoformat(result["updated_at"].replace("Z", "+00:00")):
        raise ContractError("Complete quality-memory runs must be processed in chronological order")
    latest_report_date = max(
        (date.fromisoformat(item["report_date"]) for item in result["processed_runs"]),
        default=None,
    )
    if latest_report_date is not None and date.fromisoformat(report_date) < latest_report_date:
        raise ContractError("Complete quality-memory report dates cannot move backward")
    result["processed_runs"].append(expected_run)
    result["processed_runs"].sort(
        key=lambda item: (item["generated_at"], item["report_id"], item["run_id"])
    )
    by_fingerprint = {item["fingerprint"]: item for item in result["issues"]}
    seen: set[str] = set()
    changes: list[dict[str, Any]] = []

    for finding in findings:
        expected = issue_fingerprint(
            finding["root_cause"],
            finding["engine_surface"],
            finding["validation_target"],
        )
        if finding.get("fingerprint", expected) != expected:
            raise ContractError("Finding fingerprint does not match its stable inputs")
        fingerprint = expected
        if fingerprint in seen:
            raise ContractError("A run may reconcile each fingerprint only once")
        seen.add(fingerprint)
        existing = by_fingerprint.get(fingerprint)
        if existing is None:
            normalized = dict(finding)
            normalized["fingerprint"] = fingerprint
            issue = _new_issue(normalized, report_date)
            result["issues"].append(issue)
            by_fingerprint[fingerprint] = issue
            changes.append({"fingerprint": fingerprint, "from": None, "to": "new"})
            continue

        prior = existing["state"]
        if prior == "resolved":
            existing["state"] = "regressed"
            existing["regression_history"].append(
                {"report_date": report_date, "report_path": report_path}
            )
        elif prior not in {"regressed"}:
            existing["state"] = "known"
        existing["last_seen"] = report_date
        existing["occurrence_count"] += 1
        existing["consecutive_clean_complete_runs"] = 0
        existing["title"] = finding["title"]
        existing["description"] = finding["description"]
        for key in (
            "affected_scenarios",
            "affected_domains",
            "affected_agent_types",
            "engine_builds",
            "generator_models",
            "judge_prompt_versions",
            "evidence_references",
        ):
            _merge_set(existing, key, finding[key])
        existing["last_primary_confidence"] = finding.get("primary_confidence")
        existing["last_verifier_confidence"] = finding.get("verifier_confidence")
        if existing["state"] != prior:
            changes.append(
                {"fingerprint": fingerprint, "from": prior, "to": existing["state"]}
            )

    if complete:
        selected_scenarios = {
            assignment["scenario_id"] for assignment in plan["assignments"]
        }
        for issue in result["issues"]:
            if issue["fingerprint"] in seen or issue["state"] == "resolved":
                continue
            if not set(issue["affected_scenarios"]).issubset(selected_scenarios):
                continue
            prior = issue["state"]
            issue["consecutive_clean_complete_runs"] += 1
            issue["resolution_evidence"].append(
                {"report_date": report_date, "report_path": report_path}
            )
            issue["state"] = (
                "resolved"
                if issue["consecutive_clean_complete_runs"] >= 3
                else "improving"
            )
            if issue["state"] != prior:
                changes.append(
                    {
                        "fingerprint": issue["fingerprint"],
                        "from": prior,
                        "to": issue["state"],
                    }
                )

    result["updated_at"] = generated_at
    result["issues"].sort(key=lambda item: item["fingerprint"])
    for record in result["processed_runs"]:
        require_public_artifact_safe(record, "Quality memory processed-run record")
    for issue in result["issues"]:
        require_public_artifact_safe(issue, "Quality memory issue record")
    require_public_artifact_safe(result, "Quality memory")
    validate_instance(result, MEMORY_SCHEMA, "quality memory")
    return result, changes


def render_memory_markdown(memory: dict[str, Any]) -> str:
    validate_instance(memory, MEMORY_SCHEMA, "quality memory")
    require_public_artifact_safe(memory, "Quality memory")
    lines = [
        "# Quality Memory",
        "",
        "<!-- Generated by `python -m agent_insights_quality generate-docs`; do not edit. -->",
        "",
        f"Last updated: `{memory['updated_at'] or 'never'}`",
        "",
    ]
    if not memory["issues"]:
        return "\n".join(lines + ["No quality issues have been recorded.", ""])
    lines.extend(
        [
            "| Fingerprint | State | Title | First seen | Last seen | Occurrences | Clean runs | ADO |",
            "| --- | --- | --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for issue in memory["issues"]:
        ado = issue["ado"]["work_item_reference"] or "N/A"
        lines.append(
            f"| `{issue['fingerprint']}` | {issue['state']} | {issue['title']} | "
            f"{issue['first_seen']} | {issue['last_seen']} | {issue['occurrence_count']} | "
            f"{issue['consecutive_clean_complete_runs']} | {ado} |"
        )
    markdown = "\n".join(lines + [""])
    require_public_artifact_safe(markdown, "Quality memory Markdown")
    return markdown
