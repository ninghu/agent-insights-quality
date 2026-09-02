from __future__ import annotations

import html
import re
import subprocess
from base64 import urlsafe_b64encode
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from uuid import UUID

from jsonschema import Draft202012Validator

from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.profiles import RESOURCE_GROUP, RuntimeProfile
from agent_insights_quality.progress import ProgressReporter
from agent_insights_quality.report_summary import (
    improvement_rows,
    working_capabilities,
)
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    atomic_json,
    atomic_text,
    content_hash,
    read_json,
    read_yaml,
    runtime_root,
)

_PROGRESS = ProgressReporter("aiq-email")
_PUBLIC_REPORT_BASE_URL = (
    "https://github.com/ninghu/agent-insights-quality/blob/main/"
)
_PUBLIC_ISSUE_CATALOG_URL = _PUBLIC_REPORT_BASE_URL + "ISSUE_CATALOG.md"
_QUALITY_BAR_URL = _PUBLIC_REPORT_BASE_URL + "docs/QUALITY_BAR.md#quality-score"
_OUTLOOK_TEXT_STYLE = (
    "font-family:Segoe UI,Arial,sans-serif;font-size:13px;line-height:19px;"
)
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
    "daily": "aiq-daily-swedencentral",
    "staging": "aiq-staging-swedencentral",
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


def write_private_report_preview(
    request: Mapping[str, Any],
    path: Path,
) -> None:
    try:
        path.resolve().relative_to(runtime_root().resolve())
    except ValueError as error:
        raise ContractError("Report preview must remain in the private runtime root") from error
    rendered = request.get("html")
    if not isinstance(rendered, str) or not rendered.startswith("<!doctype html>"):
        raise ContractError("Report preview request does not contain valid HTML")
    atomic_text(path, rendered)


def resolve_recipient(*, test_run: bool = False) -> str:
    reviewed_recipient = str(
        read_yaml(ROOT / "config" / "reporting.yaml").get("recipient") or ""
    ).strip()
    if reviewed_recipient != "agentinsightsteam@microsoft.com":
        raise ContractError("Report recipient does not match the reviewed team mailbox")
    if not test_run:
        return _validated_recipient(reviewed_recipient)
    override_path = runtime_root() / "config" / "email-recipient.json"
    if not override_path.is_file():
        raise ContractError("Test runs require a private test email recipient")
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
    test_run: bool = False,
) -> dict[str, Any]:
    recipient = _validated_recipient(recipient)
    if report.get("profile") == "daily" and test_run:
        if dashboard_link is not None or adx_publication != {
            "status": "skipped_test",
            "error_code": None,
        }:
            raise ContractError(
                "Test email requires ADX and dashboard publication to be skipped"
            )
    elif report.get("profile") == "daily":
        if dashboard_link is None:
            raise ContractError("Daily email requires the ADX dashboard link")
        if adx_publication is None or adx_publication.get("status") not in {
            "published",
            "already_published",
            "failed",
        }:
            raise ContractError("Daily email requires explicit ADX publication status")
    score = _overall_score(report)
    subject_prefix = "[TEST] " if test_run else ""
    subject = (
        f"{subject_prefix}[Agent Insights Quality] {score} - "
        f"{report['report_date']} - {report['summary']['issues_correct']}/"
        f"{report['summary']['issues_expected']} issues"
    )
    if report.get("profile") == "daily":
        subject = f"{subject} - {report['test_region']}"
    body = _render_html(
        report,
        project_link=project_link,
        agent_links=agent_links,
        dashboard_link=dashboard_link,
        adx_publication=adx_publication,
        work_items=work_items,
        test_run=test_run,
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
        "delivery_mode": "test_email_only" if test_run else "official",
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
    with _PROGRESS.heartbeat("Azure tenant resolution") as outcome:
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
            outcome.fail()
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
    return f"{summary['quality_score']:g}/100"


def _score_comparison(report: dict[str, Any]) -> str:
    comparison = report.get("score_comparison")
    if not isinstance(comparison, dict):
        return " (change N/A)"
    delta = comparison["delta"]
    sign = "+" if delta > 0 else ""
    return f" ({sign}{delta:g})"


def _section_heading(title: str) -> str:
    return (
        '<h2 style="margin:0 0 14px 0;color:#12304a;font-family:Segoe UI,Arial,'
        f'sans-serif;font-size:20px;line-height:26px;">{html.escape(title)}</h2>'
    )


def _data_table(
    headers: tuple[str, ...],
    rows: list[tuple[str, ...]],
    widths: tuple[int, ...],
    *,
    raw_cells: set[tuple[int, int]] | None = None,
) -> str:
    raw_cells = raw_cells or set()
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
            f"{value if (row_index, column_index) in raw_cells else html.escape(value)}</td>"
            for column_index, value in enumerate(row)
        )
        + "</tr>"
        for row_index, row in enumerate(rows)
    )
    return (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="width:100%;border-collapse:collapse;{_OUTLOOK_TEXT_STYLE}">'
        f'<tr bgcolor="#e8eef7">{header}</tr>{body}</table>'
    )


def _grade_rows(report: dict[str, Any]) -> list[tuple[str, str]]:
    summary = report["summary"]
    return [
        (
            "Quality score",
            f"{summary['quality_score']:g} / 100{_score_comparison(report)}",
        ),
        (
            "Expected issues",
            f"{summary['issues_correct']} correct / {summary['issues_expected']} "
            f"({summary['issues_incorrect']} incorrect, "
            f"{summary['issues_missing']} missing)",
        ),
        (
            "Extra cards",
            f"{summary['noise_cards']} noise, "
            f"{summary['duplicate_cards']} duplicate",
        ),
        (
            "Scoring",
            '<a style="color:#0067b8;text-decoration:underline;font-weight:600;" '
            f'href="{_QUALITY_BAR_URL}">How Scoring Works</a>',
        ),
    ]


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


def _test_region_line(report: dict[str, Any]) -> str:
    region = report["test_region"]
    return (
        f'<p style="margin:0 0 18px 0;color:#334155;{_OUTLOOK_TEXT_STYLE}">'
        f"Test Region: {html.escape(region)}</p>"
    )


def _dashboard_source_link(
    dashboard_link: str | None,
    adx_publication: Mapping[str, Any] | None,
) -> str:
    if adx_publication is not None and adx_publication.get("status") == "skipped_test":
        return (
            '<p style="margin:0 0 18px 0;padding:10px 12px;'
            f'background-color:#dbeafe;color:#12304a;{_OUTLOOK_TEXT_STYLE}">'
            "Test run: this result was intentionally not published to ADX or the "
            "quality trend dashboard.</p>"
        )
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


def _insight_engine_improvement_link(
    report: dict[str, Any], *, test_run: bool = False
) -> str:
    """Stable public link to the living Insight Engine improvement memory.

    Only an Official Daily run may reference the stable public document; the
    isolated Optional Daily Test never mutates or links a public URL.
    """
    if report.get("profile") != "daily" or test_run:
        return ""
    url = f"{_PUBLIC_REPORT_BASE_URL}reports/insight-engine-improvement.md"
    return (
        f'<p style="margin:16px 0 0 0;color:#334155;{_OUTLOOK_TEXT_STYLE}">'
        f'<a style="color:#0067b8;text-decoration:underline;font-weight:600;" '
        f'href="{html.escape(url, quote=True)}">'
        "View Insight Engine Improvement Report</a></p>"
    )


def _agent_rows(
    report: dict[str, Any],
    agent_links: Mapping[str, str],
    *,
    test_run: bool = False,
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
            '<span style="color:#64748b;">Not published</span>'
            if test_run
            else
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
    test_run: bool = False,
) -> str:
    rows = _agent_rows(report, agent_links or {}, test_run=test_run)
    test_banner = (
        '<tr><td style="padding:18px 32px;background-color:#dbeafe;'
        f'color:#12304a;font-weight:700;{_OUTLOOK_TEXT_STYLE}">'
        "TEST RUN &mdash; email-only delivery; no ADX publication or pull request."
        "</td></tr>"
        if test_run
        else ""
    )
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
        "Agent Insights Quality</h1>"
        '<p style="margin:0 0 14px 0;color:#dbeafe;font-size:17px;line-height:24px;">'
        f"{html.escape(report.get('profile', 'daily').title())} qualification report "
        f"&middot; {html.escape(report['report_date'])}</p>"
        "</td></tr>"
        + test_banner
        + '<tr><td style="padding:28px 32px 0 32px;">'
        + _section_heading("Summary")
        + _dashboard_source_link(dashboard_link, adx_publication)
        + _data_table(
            ("Summary", "Result"),
            _grade_rows(report),
            (38, 62),
            raw_cells={(3, 1)},
        )
        + "</td></tr>"
        '<tr><td style="padding:30px 32px 0 32px;">'
        + _section_heading("What is working")
        + _data_table(
            ("Capability", "Evidence"),
            working_capabilities(report),
            (28, 72),
        )
        + "</td></tr>"
        '<tr><td style="padding:24px 32px 0 32px;">'
        + _section_heading("What needs improvement")
        + _data_table(
            ("Product gap", "What happened", "Needed behavior"),
            improvement_rows(report),
            (24, 43, 33),
        )
        + "</td></tr>"
        '<tr><td style="padding:24px 32px 38px 32px;">'
        + _section_heading("Test Agents")
        + _private_project_source_link(report, project_link)
        + _test_region_line(report)
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
        + "</table>"
        + _insight_engine_improvement_link(report, test_run=test_run)
        + "</td></tr>"
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
