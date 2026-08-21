from __future__ import annotations

import html
import os
import re
from copy import deepcopy
from datetime import date
from typing import Any, Mapping
from agent_insights_quality.contracts import (
    ContractError,
    SCHEMAS,
    SCORECARD_SCHEMA,
    load_agent_manifests,
    load_scenario_catalog,
    validate_canonical_report_semantics,
    validate_instance,
)
from agent_insights_quality.links import validate_agent_insights_url
from agent_insights_quality.artifact_io import content_hash, verified_hash
from agent_insights_quality.judging import AUTO_BUG_CONFIDENCE


SECTION_TITLES = (
    "Summary",
    "What we are doing well",
    "Gaps and regressions",
    "Test agents and Agent Insights links",
)
MAIL_TRANSPORT_ORDER = (
    "connected_copilot_mail",
    "microsoft_graph",
    "local_outlook_com",
)
_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+)$")


def _human_validation_reason(items: list[dict[str, Any]]) -> str:
    reasons: set[str] = set()
    for item in items:
        if item["verdict"] == "partially_useful":
            reasons.add("partially useful judgment")
        if (
            item["verifier_verdict"] is not None
            and item["verifier_verdict"] != item["verdict"]
        ):
            reasons.add("primary/verifier disagreement")
        if item["confidence"] < AUTO_BUG_CONFIDENCE or (
            item["verifier_confidence"] is not None
            and item["verifier_confidence"] < AUTO_BUG_CONFIDENCE
        ):
            reasons.add("low-confidence judgment")
        if item["novel"]:
            reasons.add("novel finding")
        if not item["fix_verifiable"]:
            reasons.add("unverifiable fix")
    if not reasons:
        return "N/A"
    return "Required: " + "; ".join(sorted(reasons)) + "."


def validate_report_consistency(
    report: dict[str, Any],
    *,
    validate_catalog: bool = True,
) -> None:
    validate_instance(report, SCHEMAS / "canonical-report.schema.json", "canonical report")
    validate_instance(report["scorecard"], SCORECARD_SCHEMA, "canonical report scorecard")
    if validate_catalog:
        agents = load_agent_manifests()
        catalog = load_scenario_catalog({agent["id"] for agent in agents})
        validate_canonical_report_semantics(
            report, agents, catalog, "canonical report"
        )
    if report["status"] != report["scorecard"]["verdict"]:
        raise ContractError("Canonical report status contradicts its scorecard")
    if report["status"] == "INCONCLUSIVE" and report["failure"] is None:
        raise ContractError("INCONCLUSIVE report requires explicit failure details")
    if report["status"] != "INCONCLUSIVE" and report["failure"] is not None:
        raise ContractError("Conclusive report cannot contain failure details")
    if report["report_id"] != report["plan_id"]:
        raise ContractError("Canonical report identity contradicts its plan")
    if report["status"] == "INCONCLUSIVE" and report["scorecard"]["complete"]:
        raise ContractError("INCONCLUSIVE report cannot claim completeness")
    if report["status"] != "INCONCLUSIVE" and not report["scorecard"]["complete"]:
        raise ContractError("Conclusive report requires a complete scorecard")
    scenarios_by_agent: dict[str, set[str]] = {}
    for result in report["scenario_results"]:
        scenarios_by_agent.setdefault(result["agent_id"], set()).add(
            result["scenario_id"]
        )
    for agent in report["agents"]:
        relevant = [
            item
            for item in report["field_judgments"]
            if item["scenario_id"] in scenarios_by_agent.get(agent["id"], set())
        ]
        expected_reason = _human_validation_reason(relevant)
        if agent["human_validation"] != expected_reason:
            raise ContractError(
                "Human validation must be derived from semantic judgments"
            )


def render_report_markdown(report: dict[str, Any]) -> str:
    validate_report_consistency(report)
    score = report["scorecard"]
    counts = score["counts"]
    rates = score["rates"]
    lines = [
        f"# Agent Insights Quality Report - {report['report_date']}",
        "",
        f"- Report: `{report['report_id']}`",
        f"- Status: **{report['status']}**",
        f"- Engine: `{report['engine']['build']}` / `{report['engine']['generator_model']}`",
        f"- Complete: `{str(score['complete']).lower()}`",
        "",
        report["summary"],
        "",
        "## Numeric scorecard",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for name, value in counts.items():
        lines.append(f"| {name.replace('_', ' ').title()} | {value} |")
    for name, value in rates.items():
        lines.append(f"| {name.replace('_', ' ').title()} | {value:.3f} |")
    lines.extend(
        [
            "",
            "## Gate violations",
            "",
            ", ".join(f"`{item}`" for item in score["violations"]) or "None.",
            "",
            "## Scenario results",
            "",
            "| Scenario | Agent | Completed | Verdict | Insights |",
            "| --- | --- | --- | --- | ---: |",
        ]
    )
    for result in report["scenario_results"]:
        lines.append(
            f"| `{result['scenario_id']}` | `{result['agent_id']}` | "
            f"{result['completed']} | {result['verdict']} | "
            f"{len(result['insight_references'])} |"
        )
    lines.extend(
        [
            "",
            "## Field judgments",
            "",
            "| Scenario | Insight | Attribute results |",
            "| --- | --- | --- |",
        ]
    )
    for judgment in report["field_judgments"]:
        attributes = ", ".join(
            f"{name}={'pass' if passed else 'fail'}"
            for name, passed in sorted(judgment["attributes"].items())
        )
        lines.append(
            f"| `{judgment['scenario_id']}` | `{judgment['insight_reference']}` | "
            f"{attributes} |"
        )
    collection = report["collection_analysis"]
    diagnostics = report["diagnostics"]
    lines.extend(
        [
            "",
            "## Collection analysis",
            "",
            "| Distinct | Duplicates | Fragments | Umbrellas | Stale version |",
            "| ---: | ---: | ---: | ---: | ---: |",
            f"| {collection['distinct']} | {collection['duplicates']} | "
            f"{collection['fragments']} | {collection['umbrellas']} | "
            f"{collection['stale_version']} |",
            "",
            "## Efficiency diagnostics",
            "",
            "| Engine latency ms | Model calls | Tokens |",
            "| ---: | ---: | ---: |",
            f"| {diagnostics['engine_latency_ms'] if diagnostics['engine_latency_ms'] is not None else 'N/A'} | "
            f"{diagnostics['model_calls'] if diagnostics['model_calls'] is not None else 'N/A'} | "
            f"{diagnostics['tokens'] if diagnostics['tokens'] is not None else 'N/A'} |",
        ]
    )
    lines.extend(
        [
            "",
            "## Test agents",
            "",
            "| Agent | Type | Insights reference | Human validation |",
            "| --- | --- | --- | --- |",
        ]
    )
    for agent in report["agents"]:
        lines.append(
            f"| `{agent['id']}` | `{agent['type']}` | "
            f"`{agent['insights_reference']}` | {agent['human_validation']} |"
        )
    lines.extend(
        [
            "",
            "## Memory changes",
            "",
            "| Fingerprint | From | To |",
            "| --- | --- | --- |",
        ]
    )
    for change in report["memory_changes"]:
        lines.append(
            f"| `{change['fingerprint']}` | {change['from'] or 'N/A'} | {change['to']} |"
        )
    lines.extend(
        [
            "",
            "## Bug actions",
            "",
            "| Fingerprint | Action | Work item reference |",
            "| --- | --- | --- |",
        ]
    )
    for action in report["bug_actions"]:
        lines.append(
            f"| `{action['fingerprint']}` | {action['action']} | "
            f"{action['work_item_reference'] or 'N/A'} |"
        )
    return "\n".join(lines + [""])


def render_trend(reports: list[dict[str, Any]], *, limit: int = 14) -> dict[str, Any]:
    if limit < 1 or limit > 90:
        raise ContractError("Trend limit must be between 1 and 90")
    unique: dict[str, dict[str, Any]] = {}
    for report in reports:
        validate_report_consistency(report, validate_catalog=False)
        unique[report["report_date"]] = report
    selected = [unique[key] for key in sorted(unique)[-limit:]]
    trend = {
        "schema_version": "1.0.0",
        "days": [
            {
                "report_date": report["report_date"],
                "status": report["status"],
                "trusted_insight_rate": (
                    None
                    if report["status"] == "INCONCLUSIVE"
                    else report["scorecard"]["rates"]["precision"]
                ),
                "report_path": (
                    "reports/daily/"
                    + report["report_date"].replace("-", "/")
                    + (
                        f"/{report['report_id']}"
                        if report["report_id"]
                        != f"aiq-{report['report_date'].replace('-', '')}"
                        else ""
                    )
                    + "/report.md"
                ),
            }
            for report in selected
        ],
    }
    validate_instance(trend, SCHEMAS / "trend.schema.json", "trend")
    return trend


def resolve_recipient(
    config: dict[str, Any],
    environ: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    """Resolve Actions addresses or a test-mode authenticated-user mailbox handoff."""
    environ = environ if environ is not None else os.environ
    if config.get("mode") not in {"test", "production"}:
        raise ContractError("Reporting mode must be test or production")
    variables = config.get("recipient_variables")
    if variables is not None and config.get("recipient_variable") != variables.get(config["mode"]):
        raise ContractError("Reporting recipient variable does not match the selected mode")
    variable = config["recipient_variable"]
    address = environ.get(variable)
    if not address and config["mode"] == "test":
        return {
            "mode": "authenticated_user",
            "address": None,
            "source": "connected_microsoft_mailbox",
        }
    if not address:
        raise ContractError(f"Protected recipient variable is not available: {variable}")
    match = _EMAIL.fullmatch(address)
    if not match or match.group(1).casefold() != config["allowed_domain"].casefold():
        raise ContractError("Reporting recipient is outside the configured allowed domain")
    return {"mode": "address", "address": address, "source": variable}


_STATUS_STYLES = {
    "AT BAR": {
        "background": "#e6f4ea",
        "foreground": "#0b6a0b",
        "accent": "#107c10",
    },
    "NOT AT BAR": {
        "background": "#fde7e9",
        "foreground": "#a4262c",
        "accent": "#c50f1f",
    },
    "INCONCLUSIVE": {
        "background": "#fff4ce",
        "foreground": "#8a5700",
        "accent": "#d29200",
    },
}


def _trend_table(trend: dict[str, Any]) -> str:
    validate_instance(trend, SCHEMAS / "trend.schema.json", "trend")
    rows = []
    for day in trend["days"][-14:]:
        style = _STATUS_STYLES[day["status"]]
        rate_value = day["trusted_insight_rate"]
        rate = "N/A" if rate_value is None else f"{rate_value * 100:.0f}%"
        bar = "&mdash;"
        if rate_value is not None:
            bar_width = round(rate_value * 100)
            bar = (
                '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
                'width="100%" style="width:100%;border-collapse:collapse;">'
                "<tr>"
                f'<td width="{bar_width}%" bgcolor="{style["accent"]}" '
                'style="height:8px;line-height:8px;font-size:1px;">&nbsp;</td>'
                f'<td width="{100 - bar_width}%" bgcolor="#e8eef7" '
                'style="height:8px;line-height:8px;font-size:1px;">&nbsp;</td>'
                "</tr></table>"
            )
        rows.append(
            "<tr>"
            '<td style="padding:9px 10px;border:1px solid #d6deea;'
            'color:#334155;white-space:nowrap;">'
            f"{html.escape(day['report_date'])}</td>"
            f'<td bgcolor="{style["background"]}" style="padding:9px 10px;'
            f'border:1px solid #d6deea;color:{style["foreground"]};'
            'font-weight:700;white-space:nowrap;">'
            f"{html.escape(day['status'])}</td>"
            '<td style="padding:9px 10px;border:1px solid #d6deea;">'
            f"{bar}</td>"
            '<td style="padding:9px 10px;border:1px solid #d6deea;'
            'text-align:right;color:#334155;font-weight:600;">'
            f"{rate}</td>"
            "</tr>"
        )
    return (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="width:100%;border-collapse:collapse;font-size:13px;">'
        '<tr bgcolor="#e8eef7">'
        '<th align="left" style="padding:9px 10px;border:1px solid #d6deea;'
        'color:#12304a;">Date</th>'
        '<th align="left" style="padding:9px 10px;border:1px solid #d6deea;'
        'color:#12304a;">Result</th>'
        '<th align="left" style="padding:9px 10px;border:1px solid #d6deea;'
        'color:#12304a;">Trusted insight trend</th>'
        '<th align="right" style="padding:9px 10px;border:1px solid #d6deea;'
        'color:#12304a;">Rate</th></tr>'
        + "".join(rows)
        + "</table>"
    )


def _section_heading(title: str) -> str:
    return (
        '<h2 style="margin:0 0 14px 0;color:#12304a;font-family:Segoe UI,Arial,'
        f'sans-serif;font-size:20px;line-height:26px;">{html.escape(title)}</h2>'
    )


def _bullet_list(values: list[str]) -> str:
    return (
        '<ul style="margin:0;padding:0 0 0 22px;color:#334155;">'
        + "".join(
            '<li style="margin:0 0 9px 0;padding-left:3px;line-height:21px;">'
            f"{html.escape(value)}</li>"
            for value in values
        )
        + "</ul>"
    )


def _quality_conclusion(status: str) -> str:
    return {
        "AT BAR": (
            "Agent Insights met the strict daily quality bar with complete, "
            "validated evidence."
        ),
        "NOT AT BAR": (
            "Agent Insights did not meet the strict daily quality bar; the gaps "
            "below require attention."
        ),
        "INCONCLUSIVE": (
            "No quality conclusion can be made because the validated evidence "
            "set is incomplete."
        ),
    }[status]


def render_email_html(
    report: dict[str, Any],
    trend: dict[str, Any],
    agent_links: Mapping[str, str],
) -> tuple[str, str]:
    validate_report_consistency(report)
    validate_instance(trend, SCHEMAS / "trend.schema.json", "trend")
    current_rows = [
        day for day in trend["days"] if day["report_date"] == report["report_date"]
    ]
    expected_rate = (
        None
        if report["status"] == "INCONCLUSIVE"
        else report["scorecard"]["rates"]["precision"]
    )
    expected_path = (
        "reports/daily/"
        + report["report_date"].replace("-", "/")
        + (
            f"/{report['report_id']}"
            if report["report_id"] != f"aiq-{report['report_date'].replace('-', '')}"
            else ""
        )
        + "/report.md"
    )
    if (
        len(current_rows) != 1
        or current_rows[0]["status"] != report["status"]
        or current_rows[0]["trusted_insight_rate"] != expected_rate
        or current_rows[0]["report_path"] != expected_path
    ):
        raise ContractError("Current trend entry contradicts the canonical report")
    expected_agents = {agent["id"] for agent in report["agents"]}
    if set(agent_links) != expected_agents:
        raise ContractError("Direct email must contain a runtime Agent Insights link for every agent")
    for agent in report["agents"]:
        validate_agent_insights_url(agent_links[agent["id"]], agent["name"])
    score = report["scorecard"]
    counts = score["counts"]
    signal = (
        f"{counts['new_issues']} new, {counts['regressed_issues']} regressed"
        if counts["new_issues"] or counts["regressed_issues"]
        else f"{counts['completed_scenarios']}/{counts['active_scenarios']} scenarios"
    )
    subject = (
        f"[Agent Insights Quality] {report['status']} - {report['report_date']} - {signal}"
    )
    bug_signal = sum(
        action["action"] in {"created", "updated", "reopened"}
        for action in report["bug_actions"]
    )
    good = [
        f"{counts['true_positives']} expected findings were fully correct.",
        f"{counts['resolved_issues']} tracked gaps resolved.",
    ]
    if counts["healthy_insights"] == 0:
        good.append("Healthy controls produced no insights.")
    gaps = [
        f"{counts['false_negatives']} expected findings were missed.",
        f"{counts['false_positives']} produced findings were not fully trusted.",
        f"{counts['healthy_insights']} unexpected insights appeared on healthy controls.",
        f"{counts['regressed_issues']} tracked gaps regressed.",
        f"{bug_signal} private bug actions are ready or completed.",
    ]
    status_style = _STATUS_STYLES[report["status"]]
    rows = []
    for agent in report["agents"]:
        rows.append(
            "<tr>"
            '<td style="padding:11px 12px;border:1px solid #d6deea;'
            'color:#1f2937;line-height:18px;">'
            f"<strong>{html.escape(agent['name'])}</strong><br>"
            '<span style="color:#64748b;font-size:12px;">'
            f"{html.escape(agent['id'])}</span></td>"
            '<td style="padding:11px 12px;border:1px solid #d6deea;'
            f'color:#334155;">{html.escape(agent["type"])}</td>'
            '<td style="padding:11px 12px;border:1px solid #d6deea;">'
            f'<a style="color:#0067b8;text-decoration:underline;font-weight:600;" '
            f'href="{html.escape(agent_links[agent["id"]], quote=True)}">'
            "Open Agent Insights</a></td>"
            '<td style="padding:11px 12px;border:1px solid #d6deea;'
            f'color:#334155;line-height:18px;">{html.escape(agent["human_validation"])}</td>'
            "</tr>"
        )
    body = (
        '<!doctype html><html><body bgcolor="#f3f6fa" '
        'style="margin:0;padding:0;background-color:#f3f6fa;font-family:Segoe UI,'
        'Arial,sans-serif;color:#1f2937;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="100%" bgcolor="#f3f6fa" style="width:100%;background-color:#f3f6fa;">'
        '<tr><td align="center" style="padding:24px 12px;">'
        "<!--[if mso]><table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" "
        "border=\"0\" width=\"760\"><tr><td><![endif]-->"
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="100%" bgcolor="#ffffff" style="width:100%;max-width:760px;'
        'background-color:#ffffff;border:1px solid #dfe6ef;border-collapse:collapse;">'
        '<tr><td bgcolor="#12304a" style="padding:34px 38px 30px 38px;'
        'background-color:#12304a;">'
        '<h1 style="margin:0 0 8px 0;color:#ffffff;font-family:Segoe UI,Arial,'
        'sans-serif;font-size:32px;line-height:39px;font-weight:700;">'
        "Agent Insights quality</h1>"
        '<p style="margin:0 0 14px 0;color:#dbeafe;font-size:17px;line-height:24px;">'
        f"Daily qualification report &middot; {html.escape(report['report_date'])}</p>"
        f'<span style="display:inline-block;padding:5px 10px;background-color:'
        f'{status_style["background"]};color:{status_style["foreground"]};'
        'font-size:12px;line-height:16px;font-weight:700;">'
        f"{html.escape(report['status'])}</span>"
        '<p style="margin:15px 0 0 0;color:#aebfd0;font-size:12px;line-height:18px;">'
        f"Report {html.escape(report['report_id'])} &middot; Build "
        f"{html.escape(report['engine']['build'])}</p>"
        "</td></tr>"
        '<tr><td style="padding:24px 38px 0 38px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="100%" bgcolor="#eaf4ff" style="width:100%;background-color:#eaf4ff;'
        'border-left:5px solid #0078d4;border-collapse:collapse;">'
        '<tr><td style="padding:18px 20px;color:#12304a;font-size:16px;line-height:24px;">'
        f"<strong>{html.escape(_quality_conclusion(report['status']))}</strong>"
        "</td></tr></table></td></tr>"
        '<tr><td style="padding:28px 38px 0 38px;">'
        + _section_heading(SECTION_TITLES[0])
        + '<p style="margin:0 0 12px 0;color:#334155;font-size:15px;line-height:23px;">'
        f"{html.escape(report['summary'])}</p>"
        '<p style="margin:0 0 18px 0;color:#475569;font-size:14px;line-height:21px;">'
        f"{counts['completed_scenarios']} of {counts['active_scenarios']} scenarios "
        f"completed; {counts['true_positives']} correct, "
        f"{counts['partially_useful']} partially useful, and "
        f"{counts['false_positives']} incorrect or noisy.</p>"
        + _trend_table(trend)
        + "</td></tr>"
        '<tr><td style="padding:30px 38px 0 38px;">'
        + _section_heading(SECTION_TITLES[1])
        + _bullet_list(good)
        + "</td></tr>"
        '<tr><td style="padding:24px 38px 0 38px;">'
        + _section_heading(SECTION_TITLES[2])
        + _bullet_list(gaps)
        + "</td></tr>"
        '<tr><td style="padding:24px 38px 38px 38px;">'
        + _section_heading(SECTION_TITLES[3])
        + '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="width:100%;border-collapse:collapse;font-size:13px;">'
        '<tr bgcolor="#e8eef7">'
        '<th align="left" style="padding:10px 12px;border:1px solid #d6deea;'
        'color:#12304a;">Test agent</th>'
        '<th align="left" style="padding:10px 12px;border:1px solid #d6deea;'
        'color:#12304a;">Type</th>'
        '<th align="left" style="padding:10px 12px;border:1px solid #d6deea;'
        'color:#12304a;">Agent Insights</th>'
        '<th align="left" style="padding:10px 12px;border:1px solid #d6deea;'
        'color:#12304a;">Human validation</th></tr>'
        + "".join(rows)
        + "</table></td></tr></table>"
        "<!--[if mso]></td></tr></table><![endif]-->"
        "</td></tr></table></body></html>"
    )
    if tuple(re.findall(r"<h2[^>]*>(.*?)</h2>", body)) != SECTION_TITLES:
        raise ContractError("Email must contain exactly the four approved sections")
    return subject, body


def build_email_send_request(
    subject: str,
    body: str,
    recipient: dict[str, str | None],
) -> dict[str, Any]:
    content_digest = content_hash(
        {"recipient": recipient, "subject": subject, "html": body}
    )
    request = {
        "schema_version": "1.0.0",
        "channel": "connected_microsoft_mail",
        "recipient": deepcopy(recipient),
        "subject": subject,
        "html": body,
        "state": "unsent",
        "retry_delays_seconds": [60, 300, 900],
        "attempt_count": 0,
        "content_digest": content_digest,
        "transport_strategy": {
            "attempt_order": list(MAIL_TRANSPORT_ORDER),
            "graph_requires_authorization": True,
            "local_outlook_host_id": "local",
            "local_outlook_requires_authenticated_user": True,
            "local_outlook_requires_sent_items_verification": True,
            "stop_after_first_confirmed_success": True,
            "logic_app_forbidden": True,
        },
    }
    request["request_hash"] = content_hash(request)
    validate_instance(
        request,
        SCHEMAS / "email-send-request.schema.json",
        "email send request",
    )
    return request


def create_email_send_request(
    report: dict[str, Any],
    trend: dict[str, Any],
    agent_links: Mapping[str, str],
    recipient: dict[str, str | None],
) -> dict[str, Any]:
    subject, body = render_email_html(report, trend, agent_links)
    return build_email_send_request(subject, body, recipient)


def import_email_receipt(
    request: dict[str, Any],
    receipt: dict[str, Any],
) -> dict[str, Any]:
    validate_instance(
        request,
        SCHEMAS / "email-send-request.schema.json",
        "email send request",
    )
    verified_hash(request, "request_hash", "email send request")
    expected_content_digest = content_hash(
        {
            "recipient": request["recipient"],
            "subject": request["subject"],
            "html": request["html"],
        }
    )
    if request["content_digest"] != expected_content_digest:
        raise ContractError("Email handoff content digest is invalid")
    validate_instance(
        receipt,
        SCHEMAS / "email-receipt.schema.json",
        "email receipt",
    )
    if receipt.get("request_hash") != request.get("request_hash"):
        raise ContractError("Email receipt does not match its send request")
    if receipt["content_digest"] != request["content_digest"]:
        raise ContractError("Email receipt content digest does not match the handoff")

    attempts = receipt["attempts"]
    transports = [attempt["transport"] for attempt in attempts]
    if transports != list(MAIL_TRANSPORT_ORDER[: len(transports)]):
        raise ContractError("Email transports must follow the no-duplicate fallback order")
    if any(
        attempt["content_digest"] != request["content_digest"]
        for attempt in attempts
    ):
        raise ContractError("Every email attempt must preserve the same content digest")
    sent_attempts = [
        (index, attempt)
        for index, attempt in enumerate(attempts)
        if attempt["state"] == "sent"
    ]
    if len(sent_attempts) > 1 or (
        sent_attempts and sent_attempts[0][0] != len(attempts) - 1
    ):
        raise ContractError("Email transport attempts must stop after first confirmed success")

    for attempt in attempts:
        transport = attempt["transport"]
        if attempt["state"] == "sent":
            if attempt["error"] is not None or not attempt["provider_reference"]:
                raise ContractError(
                    "Successful email attempt requires opaque confirmation and no error"
                )
        elif attempt["provider_reference"] is not None or not attempt["error"]:
            raise ContractError(
                "Unsuccessful email attempt requires an error and no confirmation"
            )
        if transport == "microsoft_graph" and attempt["state"] != "unauthorized":
            if not attempt["authorization_confirmed"]:
                raise ContractError("Microsoft Graph mail requires confirmed authorization")
        if transport == "local_outlook_com":
            if (
                attempt["host_id"] != "local"
                or request["recipient"]["mode"] != "authenticated_user"
                or not attempt["mailbox_match_verified"]
            ):
                raise ContractError(
                    "Local Outlook requires hostId=local and the authenticated-user mailbox"
                )
            if attempt["state"] == "sent" and not attempt["sent_items_verified"]:
                raise ContractError(
                    "Local Outlook delivery requires Sent Items verification"
                )

    if receipt["state"] == "sent":
        if len(sent_attempts) != 1:
            raise ContractError("Sent email receipt requires one confirmed transport")
        successful = sent_attempts[0][1]
        if (
            receipt["successful_transport"] != successful["transport"]
            or receipt["provider_reference"] != successful["provider_reference"]
            or not receipt["provider_reference"]
        ):
            raise ContractError("Sent email receipt requires matching opaque confirmation")
    elif sent_attempts or receipt["successful_transport"] is not None:
        raise ContractError("Failed email receipt cannot claim a successful transport")
    if receipt["state"] == "failed" and receipt["provider_reference"] is not None:
        raise ContractError("Failed email receipt cannot contain provider confirmation")
    return deepcopy(receipt)
