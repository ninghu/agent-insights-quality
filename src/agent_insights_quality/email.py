from __future__ import annotations

import html
import re
import subprocess
from base64 import urlsafe_b64encode
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from uuid import UUID

from jsonschema import Draft202012Validator

from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.profiles import RESOURCE_GROUP, RuntimeProfile
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    atomic_json,
    content_hash,
    read_json,
    read_yaml,
    runtime_root,
)

_PUBLIC_REPORT_BASE_URL = (
    "https://github.com/ninghu/agent-insights-quality/blob/main/"
)
_PUBLIC_ISSUE_CATALOG_URL = _PUBLIC_REPORT_BASE_URL + "ISSUE_CATALOG.md"
_QUALITY_BAR_URL = _PUBLIC_REPORT_BASE_URL + "docs/QUALITY_BAR.md#quality-score"
_INSIGHT_RESULTS_URL = _PUBLIC_REPORT_BASE_URL + "docs/INSIGHT_RESULTS.md"
_OUTLOOK_TEXT_STYLE = (
    "font-family:Segoe UI,Arial,sans-serif;font-size:13px;line-height:19px;"
)
_STATUS_STYLES = {
    "PASS": {
        "background": "#e6f4ea",
        "foreground": "#0b6a0b",
    },
    "FAIL": {
        "background": "#fde7e9",
        "foreground": "#a4262c",
    },
    "INCOMPLETE": {
        "background": "#fff4ce",
        "foreground": "#8a5700",
    },
}
_AGENT_TYPES = {
    "weather-agent": "prompt",
    "healthcare-agent": "prompt",
    "finance-agent": "hosted_code",
    "travel-agent": "hosted_code",
    "support-ticket-agent": "hosted_container",
}
_AGENT_OWNERS = {
    item["name"]: item["owner"]
    for item in read_yaml(ROOT / "catalogs" / "AGENT_CATALOG.yaml")["agents"]
}
_PROJECT_NAMES = {
    "daily": "agent-insights-quality",
    "staging": "agent-insights-quality-staging",
}


def _validated_recipient(value: str) -> str:
    recipient = value.strip()
    if (
        not recipient.isascii()
        or len(recipient) > 254
        or re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@microsoft\.com",
            recipient,
            re.IGNORECASE | re.ASCII,
        )
        is None
    ):
        raise ContractError(
            "Report recipient must be exactly one reviewed microsoft.com address"
        )
    return recipient


def resolve_recipient() -> str:
    reviewed_recipient = str(
        read_yaml(ROOT / "config" / "reporting.yaml").get("recipient") or ""
    ).strip()
    if reviewed_recipient != "agentinsightsteam@microsoft.com":
        raise ContractError("Report recipient does not match the reviewed team mailbox")
    override_path = runtime_root() / "config" / "email-recipient.json"
    recipient = reviewed_recipient
    if override_path.is_file():
        override = read_json(override_path)
        if (
            override.get("schema_version") != "1.0.0"
            or override.get("purpose") != "daily_test"
            or set(override) != {"schema_version", "purpose", "recipient"}
        ):
            raise ContractError("Private test email recipient override is invalid")
        recipient = str(override.get("recipient") or "").strip()
    return _validated_recipient(recipient)


def create_request(
    report: dict[str, Any],
    recipient: str,
    *,
    project_link: str | None = None,
    agent_links: Mapping[str, str] | None = None,
    dashboard_link: str | None = None,
    adx_publication: Mapping[str, Any] | None = None,
    work_items: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    recipient = _validated_recipient(recipient)
    if report.get("profile") == "daily":
        if dashboard_link is None:
            raise ContractError("Daily email requires the ADX dashboard link")
        if adx_publication is None or adx_publication.get("status") not in {
            "published",
            "already_published",
            "failed",
        }:
            raise ContractError("Daily email requires explicit ADX publication status")
    score = _overall_score(report)
    subject = (
        f"[Agent Insights Quality] {report['status']} - {score} - "
        f"{report['report_date']} - {report['summary']['issues_correct']}/"
        f"{report['summary']['issues_expected']} issues"
    )
    body = _render_html(
        report,
        project_link=project_link,
        agent_links=agent_links,
        dashboard_link=dashboard_link,
        adx_publication=adx_publication,
        work_items=work_items,
    )
    digest = content_hash(
        {"recipient": recipient.lower(), "subject": subject, "html": body}
    )
    return {
        "schema_version": "2.0.0",
        "channel": "copilot_email",
        "recipient": recipient,
        "subject": subject,
        "html": body,
        "content_digest": digest,
        "send_once": True,
        "retry_ambiguous": False,
    }


def import_receipt(
    request: dict[str, Any],
    receipt_path: Path,
    output: Path,
) -> None:
    receipt = read_json(receipt_path)
    schema = read_json(ROOT / "schemas" / "email-receipt.schema.json")
    errors = list(Draft202012Validator(schema).iter_errors(receipt))
    if errors:
        raise ContractError(f"Email receipt is invalid: {errors[0].message}")
    if receipt["content_digest"] != request["content_digest"]:
        raise ContractError("Email receipt content digest does not match the request")
    _validate_receipt_semantics(receipt, request["content_digest"])
    atomic_json(output, receipt)


def _validate_receipt_semantics(
    receipt: dict[str, Any],
    expected_digest: str,
) -> None:
    if receipt["content_digest"] != expected_digest:
        raise ContractError("Email receipt content digest does not match the request")
    if receipt["status"] == "sent" and not receipt.get("provider_reference"):
        raise ContractError("Confirmed delivery requires an opaque provider reference")
    if receipt["status"] in {"sent", "unknown"} and receipt["retry_allowed"]:
        raise ContractError("Sent or ambiguous delivery cannot be retried")


def validate_published_receipt(path: Path, expected_digest: str) -> None:
    receipt = read_json(path)
    schema = read_json(ROOT / "schemas" / "email-receipt.schema.json")
    errors = list(Draft202012Validator(schema).iter_errors(receipt))
    if errors:
        raise ContractError(f"Published email receipt is invalid: {errors[0].message}")
    _validate_receipt_semantics(receipt, expected_digest)
    if receipt["status"] != "sent":
        raise ContractError("Published report requires one final confirmed email delivery")


def build_runtime_links(
    profile: RuntimeProfile,
    agent_names: Sequence[str],
) -> tuple[str, dict[str, str]]:
    match = re.fullmatch(
        r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/.+",
        profile.application_insights_resource_id,
        flags=re.IGNORECASE,
    )
    if match is None or match.group(2).casefold() != RESOURCE_GROUP.casefold():
        raise ContractError("Runtime profile has no canonical Azure resource context")
    subscription = match.group(1)
    try:
        subscription_token = (
            urlsafe_b64encode(UUID(subscription).bytes)
            .decode("ascii")
            .rstrip("=")
        )
    except ValueError as error:
        raise ContractError("Runtime subscription is not a canonical UUID") from error
    process = subprocess.run(
        [
            azure_cli(),
            "account",
            "show",
            "--query",
            "tenantId",
            "--output",
            "tsv",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if process.returncode != 0:
        raise ContractError("Authenticated Azure tenant could not be resolved")
    try:
        tenant_id = str(UUID(process.stdout.strip()))
    except ValueError as error:
        raise ContractError("Authenticated Azure tenant is not canonical") from error
    components = (
        RESOURCE_GROUP,
        profile.account_name,
        profile.project_name,
    )
    if any(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._()~-]{0,127}", value) is None
        for value in components
    ):
        raise ContractError("Runtime link context contains an invalid path component")
    route = (
        f"https://ai.azure.com/nextgen/r/{subscription_token},"
        f"{RESOURCE_GROUP},,{profile.account_name},{profile.project_name}"
    )
    query = urlencode({"tid": tenant_id})
    project_link = f"{route}/home?{query}"
    agent_links = {
        name: f"{route}/build/agents/{quote(name, safe='')}/build?{query}"
        for name in agent_names
    }
    return project_link, agent_links


def _overall_score(report: dict[str, Any]) -> str:
    summary = report["summary"]
    score = summary.get("quality_score")
    if score is None:
        return "N/A"
    return f"{score:g}/100"


def _section_heading(title: str) -> str:
    return (
        '<h2 style="margin:0 0 14px 0;color:#12304a;font-family:Segoe UI,Arial,'
        f'sans-serif;font-size:20px;line-height:26px;">{html.escape(title)}</h2>'
    )


def _data_table(
    headers: tuple[str, ...],
    rows: list[tuple[str, ...]],
    widths: tuple[int, ...],
) -> str:
    header = "".join(
        f'<th align="left" width="{width}%" style="padding:10px 12px;'
        f'border:1px solid #d6deea;color:#12304a;vertical-align:top;'
        f'{_OUTLOOK_TEXT_STYLE}font-weight:700;">'
        f"{html.escape(label)}</th>"
        for label, width in zip(headers, widths, strict=True)
    )
    body = "".join(
        "<tr>"
        + "".join(
            '<td style="padding:11px 12px;border:1px solid #d6deea;'
            f'color:#334155;vertical-align:top;{_OUTLOOK_TEXT_STYLE}">'
            f"{html.escape(value)}</td>"
            for value in row
        )
        + "</tr>"
        for row in rows
    )
    return (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="width:100%;border-collapse:collapse;{_OUTLOOK_TEXT_STYLE}">'
        f'<tr bgcolor="#e8eef7">{header}</tr>{body}</table>'
    )


def _grade_rows(report: dict[str, Any]) -> list[tuple[str, str]]:
    issues = report.get("issues", [])
    issue_cards = [
        card
        for item in issues
        for card in item.get("assessment", {}).get("card_evaluations", [])
    ]
    correct = sum(card.get("finding_type") == "MATCHED" for card in issue_cards)
    partial = sum(
        card.get("finding_type") == "PARTIAL" for card in issue_cards
    )
    incorrect = sum(
        card.get("finding_type") == "MISMATCHED" for card in issue_cards
    )
    noise = int(report["summary"]["noise_cards"])
    missing = sum(item.get("detail") == "MISSING" for item in issues)
    rows = [
        ("Overall judgment", report["status"]),
        ("Expected issue insights", str(report["summary"]["issues_expected"])),
        ("Observed Insights", str(report["summary"]["observed_cards"])),
        ("Fully correct Insights", str(correct)),
        ("Partially Correct Insights", str(partial)),
        ("Incorrect related Insights", str(incorrect)),
        ("Noise/duplicate Insights", str(noise)),
        ("Missing expected issues", str(missing)),
    ]
    if report["status"] == "INCOMPLETE":
        rows.insert(
            1,
            (
                "Run status reason",
                _incomplete_reason(report["summary"].get("incomplete_reasons", [])),
            ),
        )
    return rows


def _insight_results_link() -> str:
    return (
        f'<p style="margin:0 0 18px 0;color:#334155;{_OUTLOOK_TEXT_STYLE}">'
        '<a style="color:#0067b8;text-decoration:underline;font-weight:600;" '
        f'href="{_INSIGHT_RESULTS_URL}">How to read results</a></p>'
    )


def _incomplete_reason(reasons: list[str]) -> str:
    labels = {
        "clean_window_not_empty": (
            "Clean window blocked by pre-existing telemetry inside the required "
            "three-hour lookback; no Agent traffic was sent."
        ),
        "monitor_reset_failed": "Agent Insights monitor reset failed before traffic.",
        "clean_window_failed": "Clean-window telemetry verification failed.",
        "invocation_failed": "One or more deployed Agent endpoint invocations failed.",
        "telemetry_failed": "Natural telemetry did not arrive or correlate completely.",
        "trace_contract_failed": "Trace-contract verification failed.",
        "insight_run_failed": "One or more Agent Insights runs failed.",
        "assessment_evidence_incomplete": (
            "Independent assessment evidence was insufficient for a trusted score."
        ),
        "runtime_evidence_incomplete": (
            "Endpoint response counts or natural trace evidence were incomplete."
        ),
    }
    if not reasons:
        return "Validated runtime evidence was incomplete."
    return " ".join(labels.get(reason, reason.replace("_", " ").capitalize()) for reason in reasons)


def _working_capabilities(report: dict[str, Any]) -> list[tuple[str, str]]:
    summary = report["summary"]
    issues = report.get("issues", [])
    details = Counter(item.get("detail") for item in issues)
    useful = int(summary["issues_correct"]) + details["PARTIAL"]
    rows: list[tuple[str, str]] = []
    if useful:
        rows.append(
            (
                "Useful diagnostic signal",
                f"{useful} issue findings contained useful customer signal; "
                f"{summary['issues_correct']} met the strict quality bar.",
            )
        )
    if summary["issues_correct"]:
        rows.append(
            (
                "Finding content",
                f"All {summary['issues_correct']} fully correct findings passed "
                "root cause, title, description, category, severity, proposed fix, "
                "and linked-trace checks.",
            )
        )
    if summary["baseline_passed"]:
        rows.append(
            (
                "Baseline health",
                f"{summary['baseline_passed']} of 5 healthy Agent versions produced "
                "zero findings.",
            )
        )
    if not summary.get("incomplete", False):
        rows.append(
            (
                "Evidence coverage",
                f"All 5 baselines and {summary['issues_expected']} issue targets had "
                "complete endpoint and trace evidence.",
            )
        )
    if not rows:
        rows.append(
            (
                "No trusted capability conclusion",
                "Validated evidence was incomplete; observed and missing findings "
                "remain untrusted.",
            )
        )
    return rows


def _improvement_rows(report: dict[str, Any]) -> list[tuple[str, str, str]]:
    issues = report.get("issues", [])
    baseline = report.get("baseline", [])
    rows: list[tuple[str, str, str]] = []
    baseline_failures = [
        item
        for item in baseline
        if item.get("assessment", {}).get("verdict") != "clean"
        or item.get("insight_count", 0)
    ]
    if baseline_failures:
        rows.append(
            (
                "Healthy baseline findings",
                f"{len(baseline_failures)} of 5 baselines were not clean.",
                "Healthy Agent versions should produce zero findings.",
            )
        )
    missing = [item for item in issues if item.get("detail") == "MISSING"]
    if missing:
        rows.append(
            (
                "Expected findings were missed",
                f"{len(missing)} single-root issues produced no attributable card.",
                "Produce one attributable finding for every proven issue.",
            )
        )
    incorrect = [
        item
        for item in issues
        if item.get("result") == "FAIL"
        and item.get("detail") not in {"MISSING", "NOISE", "DUPLICATE"}
    ]
    if incorrect:
        rows.append(
            (
                "Finding content was incomplete or inaccurate",
                f"{len(incorrect)} findings did not pass every required field.",
                "Match root cause, title, description, category, severity, fix, "
                "and traces.",
            )
        )
    noise_cards = int(report["summary"].get("noise_cards", 0))
    if noise_cards:
        rows.append(
            (
                "Noise",
                f"{noise_cards} false-positive, unrelated, or duplicate cards.",
                "Return only distinct findings attributable to the current tested issue.",
            )
        )
    if not rows:
        rows.append(
            (
                "No product-quality gap observed",
                "Every baseline and selected issue met the reviewed contract.",
                "Preserve the current behavior and reviewed catalogs.",
            )
        )
    return rows


def _summary_narrative(report: dict[str, Any]) -> tuple[str, str]:
    summary = report["summary"]
    if report["status"] == "INCOMPLETE":
        return (
            _incomplete_reason(summary.get("incomplete_reasons", [])),
            "No quality score or product conclusion was produced from this run.",
        )
    return (
        f"The run expected {summary['issues_expected']} issue Insights and zero "
        f"baseline Insights, and observed {summary['observed_cards']} distinct cards.",
        f"Strict quality-bar matching found {summary['issues_correct']} of "
        f"{summary['issues_expected']} expected problems. "
        f"{summary['baseline_passed']} of 5 healthy baselines were clean, and "
        f"{summary.get('noise_cards', 0)} noise cards were recorded.",
    )


def _agent_report_url(report: dict[str, Any], agent_name: str) -> str:
    profile = report.get("profile")
    base = (
        "reports/daily/" + report["report_date"].replace("-", "/")
        if profile == "daily"
        else "reports/staging/"
        + report["report_date"].replace("-", "/")
        + f"/{report['run_id']}"
    )
    return f"{_PUBLIC_REPORT_BASE_URL}{base}/agents/{agent_name}.md"


def _private_project_source_link(
    report: dict[str, Any],
    project_link: str | None,
) -> str:
    project = _PROJECT_NAMES.get(report.get("profile", ""), "qualification project")
    if project_link is None:
        value = html.escape(project)
    else:
        value = (
            f'<a style="color:#0067b8;text-decoration:underline;font-weight:600;" '
            f'href="{html.escape(project_link, quote=True)}">{html.escape(project)}</a>'
        )
    return (
        f'<p style="margin:0 0 18px 0;color:#334155;{_OUTLOOK_TEXT_STYLE}">'
        f"Foundry Project: {value}</p>"
    )


def _dashboard_source_link(
    dashboard_link: str | None,
    adx_publication: Mapping[str, Any] | None,
) -> str:
    if dashboard_link is None:
        return ""
    value = (
        '<a style="color:#0067b8;text-decoration:underline;font-weight:600;" '
        f'href="{html.escape(dashboard_link, quote=True)}">'
        "Open quality trend dashboard</a>"
    )
    warning = ""
    if adx_publication is not None and adx_publication.get("status") == "failed":
        warning = (
            '<p style="margin:0 0 18px 0;padding:10px 12px;'
            f'background-color:#fff4ce;color:#8a5700;{_OUTLOOK_TEXT_STYLE}">'
            "ADX publication failed for this run, so today's result might not yet "
            "appear on the dashboard.</p>"
        )
    return (
        f'<p style="margin:0 0 18px 0;color:#334155;{_OUTLOOK_TEXT_STYLE}">'
        f"Quality trend dashboard: {value}</p>{warning}"
    )


def _agent_rows(
    report: dict[str, Any],
    agent_links: Mapping[str, str],
) -> str:
    rows = []
    for baseline in sorted(
        report.get("baseline", []),
        key=lambda item: (
            _AGENT_TYPES.get(item["agent"], "agent").casefold(),
            item["agent"].casefold(),
        ),
    ):
        name = baseline["agent"]
        agent_link = agent_links.get(name)
        agent_link_html = (
            f'<a style="color:#0067b8;text-decoration:underline;font-weight:600;" '
            f'href="{html.escape(agent_link, quote=True)}">Open agent</a>'
            if agent_link
            else '<span style="color:#64748b;">Not available</span>'
        )
        report_link_html = (
            '<a style="color:#0067b8;text-decoration:underline;font-weight:600;" '
            f'href="{html.escape(_agent_report_url(report, name), quote=True)}">'
            "View report</a>"
        )
        issue_links = ", ".join(
            f'<a style="color:#0067b8;text-decoration:underline;" '
            f'href="{_PUBLIC_ISSUE_CATALOG_URL}#{item["issue_id"]}">'
            f'{html.escape(item["issue_id"])}</a>'
            for item in report.get("issues", [])
            if item.get("agent") == name
        ) or '<span style="color:#64748b;">None</span>'
        rows.append(
            "<tr>"
            '<td style="padding:11px 12px;border:1px solid #d6deea;'
            'color:#1f2937;line-height:18px;">'
            f"<strong>{html.escape(name)}</strong></td>"
            '<td style="padding:11px 12px;border:1px solid #d6deea;'
            f'color:#334155;">{html.escape(_AGENT_TYPES.get(name, "agent"))}</td>'
            '<td style="padding:11px 12px;border:1px solid #d6deea;'
            f'color:#334155;">{html.escape(_AGENT_OWNERS[name])}</td>'
            '<td style="padding:11px 12px;border:1px solid #d6deea;">'
            f"{agent_link_html}</td>"
            '<td style="padding:11px 12px;border:1px solid #d6deea;'
            f'color:#334155;line-height:18px;">{issue_links}</td>'
            '<td style="padding:11px 12px;border:1px solid #d6deea;">'
            f"{report_link_html}</td>"
            "</tr>"
        )
    return "".join(rows)


def _work_items_section(
    work_items: Mapping[str, Any] | None,
) -> str:
    if work_items is None:
        return ""
    def table(items: Sequence[Mapping[str, Any]]) -> str:
        if not items:
            return (
                f'<p style="margin:0;color:#334155;{_OUTLOOK_TEXT_STYLE}">'
                "None.</p>"
            )
        rows = "".join(
            "<tr>"
            '<td style="padding:11px 12px;border:1px solid #d6deea;">'
            '<a style="color:#0067b8;text-decoration:underline;font-weight:600;" '
            f'href="{html.escape(str(item["url"]), quote=True)}">'
            f'{html.escape(str(item["id"]))}</a></td>'
            + "".join(
                '<td style="padding:11px 12px;border:1px solid #d6deea;'
                f'color:#334155;vertical-align:top;{_OUTLOOK_TEXT_STYLE}">'
                f"{html.escape(str(item[field]))}</td>"
                for field in ("type", "title", "assigned_to", "state")
            )
            + "</tr>"
            for item in items
        )
        headers = "".join(
            f'<th align="left" width="{width}%" style="padding:10px 12px;'
            "border:1px solid #d6deea;color:#12304a;vertical-align:top;"
            f'{_OUTLOOK_TEXT_STYLE}font-weight:700;">{label}</th>'
            for label, width in zip(
                ("ID", "Type", "Title", "Owner", "State"),
                (8, 12, 40, 25, 15),
                strict=True,
            )
        )
        return (
            '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
            f'style="width:100%;border-collapse:collapse;{_OUTLOOK_TEXT_STYLE}">'
            f'<tr bgcolor="#e8eef7">{headers}</tr>{rows}</table>'
        )

    active = work_items.get("active_items", [])
    closed = work_items.get("closed_yesterday_items", [])
    closed_date = str(work_items.get("closed_business_date") or "")
    return (
        '<tr><td style="padding:24px 32px 38px 32px;">'
        + _section_heading("Quality work items")
        + f'<h3 style="margin:0 0 10px 0;color:#12304a;{_OUTLOOK_TEXT_STYLE}'
        'font-size:16px;">Active</h3>'
        + table(active)
        + f'<h3 style="margin:24px 0 10px 0;color:#12304a;{_OUTLOOK_TEXT_STYLE}'
        f'font-size:16px;">Closed yesterday ({html.escape(closed_date)})</h3>'
        + table(closed)
        + f'<p style="margin:12px 0 0 0;color:#64748b;{_OUTLOOK_TEXT_STYLE}">'
        "Only exact Quality tags are included; Removed items are excluded.</p>"
        + "</td></tr>"
    )


def _render_html(
    report: dict[str, Any],
    *,
    project_link: str | None = None,
    agent_links: Mapping[str, str] | None = None,
    dashboard_link: str | None = None,
    adx_publication: Mapping[str, Any] | None = None,
    work_items: Mapping[str, Any] | None = None,
) -> str:
    status_style = _STATUS_STYLES[report["status"]]
    score = _overall_score(report)
    summary = _summary_narrative(report)
    rows = _agent_rows(report, agent_links or {})
    body = (
        '<!doctype html><html><body bgcolor="#f3f6fa" '
        'style="margin:0;padding:0;background-color:#f3f6fa;font-family:Segoe UI,'
        'Arial,sans-serif;color:#1f2937;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="100%" bgcolor="#f3f6fa" style="width:100%;background-color:#f3f6fa;">'
        '<tr><td align="center" style="padding:24px 12px;">'
        '<!--[if mso]><table role="presentation" cellpadding="0" cellspacing="0" '
        'border="0" width="1160"><tr><td><![endif]-->'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="100%" bgcolor="#ffffff" style="width:100%;max-width:1160px;'
        'background-color:#ffffff;border:1px solid #dfe6ef;'
        'border-collapse:collapse;">'
        '<tr><td bgcolor="#12304a" style="padding:34px 32px 30px 32px;'
        'background-color:#12304a;">'
        '<h1 style="margin:0 0 8px 0;color:#ffffff;font-family:Segoe UI,Arial,'
        'sans-serif;font-size:32px;line-height:39px;font-weight:700;">'
        "Agent Insights quality</h1>"
        '<p style="margin:0 0 14px 0;color:#dbeafe;font-size:17px;line-height:24px;">'
        f"{html.escape(report.get('profile', 'daily').title())} qualification report "
        f"&middot; {html.escape(report['report_date'])}</p>"
        f'<span style="display:inline-block;padding:5px 10px;background-color:'
        f'{status_style["background"]};color:{status_style["foreground"]};'
        'font-size:12px;line-height:16px;font-weight:700;">'
        f"Quality Score: {html.escape(score)} "
        f'(<a style="color:inherit;text-decoration:underline;" '
        f'href="{_QUALITY_BAR_URL}">How Scoring Works</a>) &middot; '
        f"{html.escape(report['status'])}</span>"
        "</td></tr>"
        '<tr><td style="padding:28px 32px 0 32px;">'
        + _section_heading("Summary")
        + "".join(
            f'<p style="margin:0 0 12px 0;color:#334155;'
            f'{_OUTLOOK_TEXT_STYLE}">{html.escape(paragraph)}</p>'
            for paragraph in summary
        )
        + _private_project_source_link(report, project_link)
        + _dashboard_source_link(dashboard_link, adx_publication)
        + _data_table(("Grade", "Findings"), _grade_rows(report), (38, 62))
        + _insight_results_link()
        + "</td></tr>"
        '<tr><td style="padding:30px 32px 0 32px;">'
        + _section_heading("What is working")
        + _data_table(
            ("Capability", "Evidence"),
            _working_capabilities(report),
            (28, 72),
        )
        + "</td></tr>"
        '<tr><td style="padding:24px 32px 0 32px;">'
        + _section_heading("What needs improvement")
        + _data_table(
            ("Product gap", "What happened", "Needed behavior"),
            _improvement_rows(report),
            (24, 43, 33),
        )
        + "</td></tr>"
        '<tr><td style="padding:24px 32px 38px 32px;">'
        + _section_heading("Test Agents")
        + '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="width:100%;border-collapse:collapse;font-size:13px;">'
        '<tr bgcolor="#e8eef7">'
        '<th align="left" width="18%" style="padding:10px 12px;'
        'border:1px solid #d6deea;color:#12304a;">Test agent</th>'
        '<th align="left" width="12%" style="padding:10px 12px;'
        'border:1px solid #d6deea;color:#12304a;">Type</th>'
        '<th align="left" width="14%" style="padding:10px 12px;'
        'border:1px solid #d6deea;color:#12304a;">Owner</th>'
        '<th align="left" width="12%" style="padding:10px 12px;'
        'border:1px solid #d6deea;color:#12304a;">Agent</th>'
        '<th align="left" width="30%" style="padding:10px 12px;'
        'border:1px solid #d6deea;color:#12304a;">Tested issues</th>'
        '<th align="left" width="14%" style="padding:10px 12px;'
        'border:1px solid #d6deea;color:#12304a;">Report</th></tr>'
        + rows
        + "</table></td></tr>"
        + _work_items_section(work_items)
        + "</table>"
        "<!--[if mso]></td></tr></table><![endif]-->"
        "</td></tr></table></body></html>"
    )
    headings = tuple(re.findall(r"<h2[^>]*>(.*?)</h2>", body))
    expected_headings = (
        "Summary",
        "What is working",
        "What needs improvement",
        "Test Agents",
    ) + (("Quality work items",) if work_items is not None else ())
    if headings != expected_headings:
        raise ContractError("Email contains an unexpected section layout")
    return body
