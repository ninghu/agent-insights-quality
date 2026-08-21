from __future__ import annotations

import html
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from agent_insights_quality.contracts import (
    ContractError,
    ROOT,
    SCHEMAS,
    load_agent_manifests,
    load_scenario_catalog,
    validate_canonical_report_semantics,
    validate_daily_plan_semantics,
    validate_instance,
    validate_report_plan_binding,
)
from agent_insights_quality.generated_paths import validate_generated_paths
from agent_insights_quality.planning import generate_daily_plan
from agent_insights_quality.public_safety import require_public_artifact_safe
from agent_insights_quality.reporting import (
    build_email_send_request,
    create_email_send_request,
    render_email_html,
    render_report_markdown,
    render_trend,
    validate_report_consistency,
)
from agent_insights_quality.artifact_io import canonical_json, content_hash, write_json


def build_preflight_plan(report_date: str, generated_at: str) -> dict[str, Any]:
    date.fromisoformat(generated_at[:10])
    return generate_daily_plan(date.fromisoformat(report_date))


def _zero_scorecard(active: int, completed: int) -> dict[str, Any]:
    rate_names = (
        "high_severity_recall",
        "medium_severity_recall",
        "low_severity_recall",
        "overall_recall",
        "precision",
        "f1",
        "healthy_noise_rate",
        "category_accuracy",
        "severity_accuracy",
        "title_pass_rate",
        "description_pass_rate",
        "proposed_fix_pass_rate",
        "linked_trace_pass_rate",
        "evidence_localization_rate",
        "meaningfulness_rate",
        "actionability_rate",
        "distinctness_rate",
        "duplication_rate",
        "fragmentation_rate",
        "umbrella_rate",
        "cross_version_stale_rate",
    )
    return {
        "schema_version": "1.0.0",
        "verdict": "INCONCLUSIVE",
        "complete": False,
        "counts": {
            "active_scenarios": active,
            "completed_scenarios": completed,
            "true_positives": 0,
            "partially_useful": 0,
            "false_positives": 0,
            "false_negatives": 0,
            "healthy_insights": 0,
            "structural_failures": 0,
            "new_issues": 0,
            "known_issues": 0,
            "resolved_issues": 0,
            "regressed_issues": 0,
        },
        "rates": {name: 0.0 for name in rate_names},
        "violations": ["incomplete_catalog"],
    }


def build_failure_report(
    plan: dict[str, Any],
    failure: dict[str, Any],
    *,
    generated_at: str,
) -> dict[str, Any]:
    validate_instance(plan, SCHEMAS / "daily-plan.schema.json", "daily plan")
    manifests = load_agent_manifests()
    assignment_by_agent: dict[str, dict[str, Any]] = {}
    for assignment in plan["assignments"]:
        assignment_by_agent.setdefault(assignment["agent_id"], assignment)
    agents = []
    for manifest in manifests:
        assignment = assignment_by_agent.get(manifest["id"])
        version = (
            assignment["agent_version_digest"]
            if assignment
            else content_hash({"agent": manifest["id"], "state": "not-started"})
        )
        name = assignment["agent_name"] if assignment else manifest["id"]
        agents.append(
            {
                "id": manifest["id"],
                "name": name,
                "type": manifest["agent_type"],
                "version_digest": version,
                "insights_reference": content_hash(
                    {"plan": plan["plan_id"], "agent": manifest["id"]}
                ),
                "human_validation": "N/A",
            }
        )
    completed_ids = set(failure.get("completed_scenarios", []))
    report = {
        "schema_version": "1.0.0",
        "report_id": plan["plan_id"],
        "report_date": plan["report_date"],
        "generated_at": generated_at,
        "plan_id": plan["plan_id"],
        "status": "INCONCLUSIVE",
        "summary": (
            f"Qualification stopped in {failure['failed_phase']}: {failure['reason']}"
        ),
        "engine": deepcopy(plan["engine"]),
        "scorecard": _zero_scorecard(len(plan["assignments"]), len(completed_ids)),
        "agents": agents,
        "scenario_results": [
            {
                "scenario_id": assignment["scenario_id"],
                "agent_id": assignment["agent_id"],
                "agent_version_digest": assignment["agent_version_digest"],
                "completed": assignment["scenario_id"] in completed_ids,
                "verdict": "inconclusive",
                "insight_references": [],
            }
            for assignment in plan["assignments"]
        ],
        "field_judgments": [],
        "collection_analysis": {
            "distinct": 0,
            "duplicates": 0,
            "fragments": 0,
            "umbrellas": 0,
            "stale_version": 0,
        },
        "diagnostics": {
            "engine_latency_ms": None,
            "model_calls": None,
            "tokens": None,
        },
        "bug_actions": [],
        "memory_changes": [],
        "artifact_reference": failure["diagnostics_reference"]
        or content_hash({"plan": plan["plan_id"], "failure": failure["failed_phase"]}),
        "failure": {
            key: deepcopy(failure[key])
            for key in (
                "failed_phase",
                "last_confirmed_stage",
                "reason",
                "affected_agents",
                "diagnostics_reference",
                "next_action",
            )
        },
        "delivery": {"state": "unsent", "request_reference": None},
    }
    validate_report_consistency(report)
    return report


def render_failure_email_html(report: dict[str, Any]) -> str:
    validate_report_consistency(report)
    if report["status"] != "INCONCLUSIVE" or report["failure"] is None:
        raise ContractError("Failure email requires an INCONCLUSIVE failure report")
    failure = report["failure"]
    rows = "".join(
        f"<tr><td>{html.escape(agent['id'])}</td><td>{html.escape(agent['name'])}</td>"
        f"<td>{html.escape(agent['type'])}</td><td>N/A</td><td>N/A</td></tr>"
        for agent in report["agents"]
    )
    return (
        "<html><body><h1>INCONCLUSIVE</h1>"
        "<h2>Summary</h2>"
        f"<p>{html.escape(report['summary'])}</p>"
        f"<p>Last confirmed stage: {html.escape(failure['last_confirmed_stage'])}.</p>"
        "<h2>What we are doing well</h2>"
        "<p>No quality conclusion was inferred from incomplete evidence.</p>"
        "<h2>Gaps and regressions</h2>"
        f"<p>{html.escape(failure['reason'])}</p>"
        f"<p>Next action: {html.escape(failure['next_action'])}</p>"
        "<p>Email state: unsent. The direct-mail handoff may retry after 60, 300, "
        "and 900 seconds, must stop after the first confirmed success, and must import "
        "a provider receipt before claiming delivery.</p>"
        "<h2>Test agents and Agent Insights links</h2>"
        "<table><tr><th>Agent ID</th><th>Test agent</th><th>Type</th>"
        "<th>Agent Insights page</th><th>Human validation recommended</th></tr>"
        f"{rows}</table></body></html>"
    )


def create_failure_send_request(
    report: dict[str, Any],
    recipient: dict[str, str | None],
) -> dict[str, Any]:
    if report["failure"] is None:
        raise ContractError("Failure send request requires failure details")
    body = render_failure_email_html(report)
    request = build_email_send_request(
        (
            f"[Agent Insights Quality] INCONCLUSIVE - {report['report_date']} - "
            f"{report['failure']['failed_phase']}"
        ),
        body,
        recipient,
    )
    return request


def write_daily_artifacts(
    root: Path,
    plan: dict[str, Any],
    report: dict[str, Any],
    *,
    failure_email: str | None = None,
) -> Path:
    manifests = load_agent_manifests()
    catalog = load_scenario_catalog({item["id"] for item in manifests})
    validate_instance(plan, SCHEMAS / "daily-plan.schema.json", "daily plan")
    validate_daily_plan_semantics(plan, manifests, catalog, "daily plan")
    validate_report_consistency(report)
    validate_canonical_report_semantics(report, manifests, catalog, "canonical report")
    validate_report_plan_binding(report, plan, "canonical report")
    if report["report_date"] != plan["report_date"] or report["plan_id"] != plan["plan_id"]:
        raise ContractError("Daily report does not match the daily plan")
    relative_root = plan["artifact_directory"]
    target = root / Path(relative_root)
    generated = [
        f"{relative_root}/plan.json",
        f"{relative_root}/plan.md",
        f"{relative_root}/report.json",
        f"{relative_root}/report.md",
        "reports/latest.json",
        "reports/latest.md",
    ]
    if failure_email is not None:
        generated.append(f"{relative_root}/failure-email.html")
    validate_generated_paths(generated)
    plan_markdown = (
        f"# Agent Insights Quality Plan - {plan['report_date']}\n\n"
        f"- Plan: `{plan['plan_id']}`\n"
        f"- Catalog: `{plan['catalog_version']}`\n"
        f"- Assignments: {len(plan['assignments'])}\n"
    )
    report_markdown = render_report_markdown(report)
    require_public_artifact_safe(plan, "Canonical public plan")
    require_public_artifact_safe(report, "Canonical public report")
    if failure_email is not None:
        require_public_artifact_safe(failure_email, "Public failure email")
    for label, text in (
        ("plan.md", plan_markdown),
        ("report.md", report_markdown),
        ("failure-email.html", failure_email or ""),
    ):
        if text:
            require_public_artifact_safe(text, label)
    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "plan.json", plan)
    (target / "plan.md").write_bytes(plan_markdown.encode("ascii"))
    write_json(target / "report.json", report)
    (target / "report.md").write_bytes(report_markdown.encode("ascii"))
    if failure_email is not None:
        if report["status"] != "INCONCLUSIVE":
            raise ContractError("Failure email can be written only for INCONCLUSIVE reports")
        (target / "failure-email.html").write_bytes(failure_email.encode("ascii"))
    write_json(root / "reports" / "latest.json", report)
    (root / "reports" / "latest.md").write_bytes(report_markdown.encode("ascii"))
    return target


def finalize_success(
    report: dict[str, Any],
    prior_reports: list[dict[str, Any]],
    agent_links: Mapping[str, str],
    recipient: dict[str, str | None],
) -> dict[str, Any]:
    trend = render_trend(prior_reports + [report])
    request = create_email_send_request(report, trend, agent_links, recipient)
    public_report = deepcopy(report)
    public_report["delivery"] = {
        "state": "unsent",
        "request_reference": request["request_hash"],
    }
    validate_report_consistency(public_report)
    return {"report": public_report, "trend": trend, "email_send_request": request}
