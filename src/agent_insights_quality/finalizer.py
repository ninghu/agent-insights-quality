from __future__ import annotations

import html
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from agent_insights_quality.contracts import (
    ContractError,
    SCHEMAS,
    load_agent_manifests,
    load_scenario_catalog,
    validate_canonical_report_semantics,
    validate_daily_plan_semantics,
    validate_instance,
    validate_report_plan_binding,
)
from agent_insights_quality.generated_paths import validate_generated_paths
from agent_insights_quality.links import RuntimeLinkContext
from agent_insights_quality.planning import (
    generate_daily_plan,
    render_plan_markdown,
    serialize_plan,
)
from agent_insights_quality.public_safety import require_public_artifact_safe
from agent_insights_quality.reporting import (
    build_email_send_request,
    create_email_send_request,
    render_report_markdown,
    render_trend,
    validate_report_consistency,
)
from agent_insights_quality.reporting.model import attach_structured_report_context
from agent_insights_quality.artifact_io import (
    content_hash,
    write_bytes_atomic,
    write_json,
)


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
                "run_id": assignment["run_id"],
                "version_sequence": {
                    "phase": assignment["version_sequence"][-1]["phase"],
                    "version_digest": assignment["version_sequence"][-1]["digest"],
                },
                "agent_version_digest": assignment["version_sequence"][-1]["digest"],
                "completed": assignment["scenario_id"] in completed_ids,
                "expected_count": assignment["expected"]["finding_count"],
                "observed_count": 0,
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
    report = attach_structured_report_context(report, plan)
    validate_report_consistency(report)
    validate_report_plan_binding(report, plan, "canonical report")
    return report


def _failure_section_heading(title: str) -> str:
    return (
        '<h2 style="margin:0 0 14px;color:#12304a;font-family:Segoe UI,Arial,'
        f'sans-serif;font-size:20px;line-height:26px;">{html.escape(title)}</h2>'
    )


def render_failure_email_html(report: dict[str, Any]) -> str:
    validate_report_consistency(report)
    if report["status"] != "INCONCLUSIVE" or report["failure"] is None:
        raise ContractError("Failure email requires an INCONCLUSIVE failure report")
    failure = report["failure"]
    rows = "".join(
        "<tr>"
        '<td style="padding:10px;border:1px solid #d6deea;">'
        f"{html.escape(agent['id'])}</td>"
        '<td style="padding:10px;border:1px solid #d6deea;">'
        f"{html.escape(agent['name'])}</td>"
        '<td style="padding:10px;border:1px solid #d6deea;">'
        f"{html.escape(agent['type'])}</td>"
        '<td style="padding:10px;border:1px solid #d6deea;">N/A</td>'
        '<td style="padding:10px;border:1px solid #d6deea;">N/A</td></tr>'
        for agent in report["agents"]
    )
    return (
        '<!doctype html><html><body bgcolor="#f3f6fa" style="margin:0;padding:0;'
        'background-color:#f3f6fa;font-family:Segoe UI,Arial,sans-serif;color:#1f2937;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="100%" bgcolor="#f3f6fa"><tr><td align="center" style="padding:24px 12px;">'
        "<!--[if mso]><table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" "
        "border=\"0\" width=\"760\"><tr><td><![endif]-->"
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="100%" bgcolor="#ffffff" style="width:100%;max-width:760px;'
        'background-color:#ffffff;border:1px solid #dfe6ef;border-collapse:collapse;">'
        '<tr><td bgcolor="#12304a" style="padding:30px 38px;background-color:#12304a;">'
        '<h1 style="margin:0;color:#ffffff;font-size:30px;line-height:38px;">'
        "Agent Insights quality</h1>"
        '<p style="margin:10px 0 0;color:#fff4ce;font-weight:700;">INCONCLUSIVE</p>'
        "</td></tr>"
        '<tr><td style="padding:28px 38px 0;">'
        + _failure_section_heading("Summary")
        + "<p>No quality conclusion can be made because the validated evidence set is incomplete.</p>"
        "<p>Expected findings: N/A; observed findings: N/A. "
        f"Last confirmed stage: {html.escape(failure['last_confirmed_stage'])}.</p>"
        "</td></tr>"
        '<tr><td style="padding:24px 38px 0;">'
        + _failure_section_heading("What we are doing well")
        + "<p>N/A - quality controls were not evaluated from incomplete evidence.</p>"
        "</td></tr>"
        '<tr><td style="padding:24px 38px 0;">'
        + _failure_section_heading("Gaps and regressions")
        + f"<p>{html.escape(failure['reason'])}</p>"
        f"<p>Next action: {html.escape(failure['next_action'])}</p>"
        "<p>Email state: unsent. The direct-mail handoff may retry after 60, 300, "
        "and 900 seconds, must stop after the first confirmed success, and must import "
        "a provider receipt before claiming delivery.</p>"
        "</td></tr>"
        '<tr><td style="padding:24px 38px 38px;">'
        + _failure_section_heading("Test agents and Agent Insights links")
        + '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="width:100%;border-collapse:collapse;font-size:13px;">'
        '<tr bgcolor="#e8eef7"><th>Agent ID</th><th>Test agent</th><th>Type</th>'
        "<th>Agent Insights page</th><th>Human validation recommended</th></tr>"
        f"{rows}</table></td></tr></table>"
        "<!--[if mso]></td></tr></table><![endif]-->"
        "</td></tr></table></body></html>"
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
    return write_daily_artifacts_to_reports_root(
        root / "reports",
        plan,
        report,
        failure_email=failure_email,
    )


def write_daily_artifacts_to_reports_root(
    reports_root: Path,
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
    validate_canonical_report_semantics(
        report,
        manifests,
        catalog,
        "canonical report",
        expected_scenario_ids={
            assignment["scenario_id"] for assignment in plan["assignments"]
        },
    )
    validate_report_plan_binding(report, plan, "canonical report")
    if report["report_date"] != plan["report_date"] or report["plan_id"] != plan["plan_id"]:
        raise ContractError("Daily report does not match the daily plan")
    relative_root = plan["artifact_directory"]
    relative_path = Path(relative_root)
    if not relative_path.parts or relative_path.parts[0] != "reports":
        raise ContractError("Daily artifact directory must remain under reports/")
    target = reports_root.joinpath(*relative_path.parts[1:])
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
    plan_markdown = render_plan_markdown(plan, catalog)
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
    plan_json_path = target / "plan.json"
    plan_markdown_path = target / "plan.md"
    plan_json = serialize_plan(plan)
    plan_markdown_bytes = plan_markdown.encode("ascii")
    if plan_json_path.exists() and plan_json_path.read_bytes() != plan_json:
        raise ContractError("Existing daily plan JSON differs from the immutable plan")
    if (
        plan_markdown_path.exists()
        and plan_markdown_path.read_bytes() != plan_markdown_bytes
    ):
        raise ContractError("Existing daily plan Markdown differs from the immutable plan")
    if not plan_json_path.exists():
        write_bytes_atomic(plan_json_path, plan_json)
    if not plan_markdown_path.exists():
        write_bytes_atomic(plan_markdown_path, plan_markdown_bytes)
    write_json(target / "report.json", report)
    write_bytes_atomic(target / "report.md", report_markdown.encode("ascii"))
    if failure_email is not None:
        if report["status"] != "INCONCLUSIVE":
            raise ContractError("Failure email can be written only for INCONCLUSIVE reports")
        write_bytes_atomic(target / "failure-email.html", failure_email.encode("ascii"))
    write_json(reports_root / "latest.json", report)
    write_bytes_atomic(reports_root / "latest.md", report_markdown.encode("ascii"))
    return target


def finalize_success(
    plan: dict[str, Any],
    report: dict[str, Any],
    prior_reports: list[dict[str, Any]],
    agent_links: Mapping[str, str],
    expected_link_context: RuntimeLinkContext,
    recipient: dict[str, str | None],
) -> dict[str, Any]:
    report = attach_structured_report_context(report, plan)
    trend = render_trend(prior_reports + [report])
    request = create_email_send_request(
        report, trend, agent_links, expected_link_context, recipient
    )
    public_report = deepcopy(report)
    public_report["delivery"] = {
        "state": "unsent",
        "request_reference": request["request_hash"],
    }
    validate_report_consistency(public_report)
    return {"report": public_report, "trend": trend, "email_send_request": request}
