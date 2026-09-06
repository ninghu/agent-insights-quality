"""Read-only renderers of the one aggregated result. No traffic or sink writes."""

from __future__ import annotations

from collections.abc import Iterable
from html import escape, unescape
import json
import re

from .privacy import public_projection, warning_text
from .report_context import ReportContextError, ReportMetadata, ReviewedReportContext
from .report_links import VerifiedScoringLink, validate_foundry_link
from .report_review import RetainedReviewContext
from .results import PlannedUnit, QualityResult, UnitId


def _identity(unit: dict) -> UnitId:
    return UnitId(**unit["unit_id"])


def _unit_name(unit: dict, context: dict) -> str:
    identity = _identity(unit)
    name = f"{identity.agent} / {identity.logical_version}"
    return f"{name} - {context[identity].title}" if context else name


def _inputs(result, allowed_units, report_context, metadata):
    plan = tuple(allowed_units)
    value = public_projection(result, allowed_units=plan)
    if report_context is not None and type(report_context) is not ReviewedReportContext:
        raise ReportContextError("report_context_invalid")
    if metadata is not None and type(metadata) is not ReportMetadata:
        raise ReportContextError("report_metadata_invalid")
    context = report_context.for_plan(plan) if report_context is not None else {}
    return value, context


def _metadata_lines(metadata: ReportMetadata | None) -> tuple[str, ...]:
    if metadata is None:
        return ()
    return (
        f"Report date: {metadata.report_date}",
        f"Region: {metadata.region_display}",
        f"Source commit: {metadata.source_revision}",
    )


def _markdown_text(text: str) -> str:
    # Escape markup, not content. Approval is established before rendering.
    text = escape(" ".join(text.splitlines()), quote=False)
    for character in ("\\", "`", "*", "[", "]", "|"):
        text = text.replace(character, "\\" + character)
    return re.sub(r"(?<!\w)_|_(?!\w)", r"\\_", text)


def render_markdown(
    result: QualityResult, *, allowed_units: Iterable[PlannedUnit],
    warnings: tuple[str, ...] = (),
    report_context: ReviewedReportContext | None = None,
    metadata: ReportMetadata | None = None,
    delivery_id: str | None = None,
) -> str:
    """Public-safe report. No private evidence/context parameter exists here."""
    return _render_markdown(
        result, allowed_units=allowed_units, warnings=warnings,
        report_context=report_context, metadata=metadata, delivery_id=delivery_id,
    )


def render_private_markdown(
    result: QualityResult, *, allowed_units: Iterable[PlannedUnit],
    review_context: RetainedReviewContext, warnings: tuple[str, ...] = (),
    report_context: ReviewedReportContext | None = None,
    metadata: ReportMetadata | None = None, delivery_id: str | None = None,
    agent: str | None = None,
) -> str:
    """Explicit private boundary; never used by public_artifacts or ADX."""
    if type(review_context) is not RetainedReviewContext:
        raise ReportContextError("report_review_context_invalid")
    return _render_markdown(
        result, allowed_units=allowed_units, warnings=warnings,
        report_context=report_context, metadata=metadata, delivery_id=delivery_id,
        private=review_context.for_result(result), agent=agent,
    )


_ASSESSMENT_LABEL = (
    r"(?:Saved assessment|(?:Initial assessment|Focused review|Resolved assessment)"
    r" \((?:correct|incorrect|unknown)\))"
)
_DETAIL_ENTRY = (
    r"<br><strong>Finding [1-9][0-9]*: " + _ASSESSMENT_LABEL
    + r"</strong><br>(?:[^<>]|<br>)*?"
)
_DETAILS = re.compile(
    r"<details><summary>Assessment details</summary>((?:" + _DETAIL_ENTRY + r")+)</details>"
)
_DETAIL_LABEL = re.compile(
    r"(<br><strong>Finding [1-9][0-9]*: " + _ASSESSMENT_LABEL + r"</strong><br>)"
)


def _assessment_details(entries: list[tuple[int, dict]]) -> str:
    body = []
    for index, card in entries:
        disagreement = card.get("disagreement")
        if disagreement:
            reasons = [
                (f"{label} ({disagreement[stage]['core']})", disagreement[stage]["reason"])
                for stage, label in (("initial", "Initial assessment"), ("review", "Focused review"))
            ]
            if card["reason"] not in {reason for _, reason in reasons}:
                reasons.append(("Resolved assessment (unknown)", card["reason"]))
        else:
            label = "Resolved assessment (unknown)" if card.get("core") == "unknown" else "Saved assessment"
            reasons = [(label, card["reason"])]
        for label, reason in reasons:
            # Raw HTML table cells need entities, not Markdown escapes. Keep all
            # reason text while preventing pipes/newlines from creating rows.
            text = escape(reason, quote=False).replace("|", "&#124;")
            text = text.replace("\r\n", "<br>").replace("\r", "<br>").replace("\n", "<br>")
            for character in ("\\", "`", "*", "[", "]", "\u2028", "\u2029"):
                text = text.replace(character, f"&#{ord(character)};")
            body.append(f"<br><strong>Finding {index}: {label}</strong><br>{text}")
    return "<details><summary>Assessment details</summary>" + "".join(body) + "</details>"


_FINDING_LABELS = {
    "expected_detection": "Correct",
    "noise": "Noise",
    "duplicate": "Duplicate",
    "unexpected_real": "Unexpected finding",
    "unknown": "Unconfirmed",
}


def _table_row(number: int, unit: dict, context: dict, detail: dict, *, private: bool) -> str:
    identity = _identity(unit)
    deployment = detail.get("deployment")
    version = (
        f"{deployment['provider_version']} ({identity.logical_version})"
        if deployment else f"{identity.logical_version} (deployment not recorded)"
    )
    expected = (
        "None (healthy baseline)" if unit["kind"] == "baseline" else
        context[identity].title if context else identity.logical_version
    )
    current = [finding for finding in unit["findings"] if finding["contribution"] == "current"]
    titles, verdicts, notes, details = [], [], [], []
    missed = unit["scorable"] and unit["kind"] == "issue" and not unit["counts"]["correct_issues"]
    if not unit["scorable"]:
        if any(
            detail.get("cards", {}).get(finding["card_alias"], {}).get("disagreement")
            for finding in current
        ):
            notes.append("Assessment disagreement; unit not scored.")
        else:
            reasons = ", ".join(unit["exclusion_reasons"])
            notes.append("Not scored: " + reasons + ". Not counted as a miss.")
        if detail.get("failure_code"):
            notes.append("Reason: " + detail["failure_code"] + ".")
    elif missed and private:
        observations = len(detail.get("observations", ()))
        notes.append(
            f"Observed {observations}/10; Insights did not detect."
            if observations else "Insights did not detect the expected defect."
        )
    roots = {}
    for index, finding in enumerate(current, 1):
        if finding["classification"] in {"expected_detection", "unexpected_real"}:
            roots.setdefault(finding["root_cause_alias"], index)
    for index, finding in enumerate(current, 1):
        alias = finding["card_alias"]
        card = detail.get("cards", {}).get(alias, {})
        label = _FINDING_LABELS[finding["classification"]]
        if not finding["scored"]:
            label += " (unscored)"
        title = card.get("title") or alias
        titles.append(f"{index}. {title}")
        verdicts.append(f"{index}. {label}")
        root = finding["root_cause_alias"]
        if finding["classification"] == "duplicate":
            notes.append(f"{index}: Same root as finding {roots[root]}.")
        if card.get("reason") and finding["classification"] in {"noise", "unknown", "unexpected_real"}:
            details.append((index, card))
    if not current:
        titles.append(
            "Not generated (trace readiness insufficient)"
            if detail.get("failure_code") == "trace_readiness_insufficient" else
            "Unavailable" if not unit["scorable"] else "None"
        )
        verdicts.append("Unscored" if not unit["scorable"] else "Insights miss" if missed else "-")
    elif missed and not private:
        verdicts.append("Insights miss")
    if private and detail.get("unavailable"):
        notes.append("Assessment details unavailable.")
    rendered_notes = [_markdown_text(note) for note in notes]
    if details:
        rendered_notes.append(_assessment_details(details))
    cells = [
        str(number), _markdown_text(version), _markdown_text(expected),
        "<br>".join(_markdown_text(title) for title in titles),
        "<br>".join(_markdown_text(verdict) for verdict in verdicts),
        "<br>".join(rendered_notes) or "-",
    ]
    return "| " + " | ".join(cells) + " |"


def _render_markdown(
    result, *, allowed_units, warnings, report_context, metadata, delivery_id, private=None, agent=None,
) -> str:
    value, context = _inputs(result, allowed_units, report_context, metadata)
    if delivery_id is not None and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", delivery_id) is None:
        raise ReportContextError("report_delivery_identity_invalid")
    counts, coverage = value["counts"], value["coverage"]
    score = f"{value['score']:.1f}/100" if value["score"] is not None else "Unmeasured (no quality score)"
    lines = [
        "# Agent Insights quality", "",
        f"Quality score: {score}. Detected: {counts['correct_issues']}/{counts['expected_issues']} scored issues; "
        f"Noise: {counts['noise_cards']}; Duplicate: {counts['duplicate_cards']}.",
        "", f"Coverage: {coverage['scored_issues']}/{coverage['planned_issues']} expected issues; "
        f"{coverage['scored_baselines']}/{coverage['planned_baselines']} expected baselines; "
        f"{coverage['excluded_units']} excluded units.",
        "", "Each row is one version run (10 attempts), not one attempt. "
        "Generated insights are new or updated findings from that run; unchanged historical cards are omitted.",
        "", "Unexpected finding means a correct non-target finding; it earns no expected-detection credit.",
    ]
    if not value["team_report_eligible"]:
        lines += ["", "Personal notice: no valid overall measurement. Counts describe the scored subset only."]
    agents = dict.fromkeys(unit["unit_id"]["agent"] for unit in value["units"])
    if agent is not None:
        if agent not in agents or private is None:
            raise ReportContextError("report_agent_invalid")
        units = [unit for unit in value["units"] if unit["unit_id"]["agent"] == agent]
        scored = [unit for unit in units if unit["scorable"]]
        lines[0] += " - " + _agent_name(agent)
        lines[2] = "Overall Daily " + lines[2][0].lower() + lines[2][1:]
        lines[4] = "Overall Daily " + lines[4][0].lower() + lines[4][1:]
        lines += [
            "", "This Agent (no separate score): "
            f"{sum(unit['counts']['correct_issues'] for unit in scored)}/"
            f"{sum(unit['counts']['expected_issues'] for unit in scored)} scored issues detected; "
            f"{sum(unit['counts']['noise_cards'] for unit in scored)} Noise; "
            f"{sum(unit['counts']['duplicate_cards'] for unit in scored)} Duplicate; "
            f"{len(units) - len(scored)} excluded units.",
        ]
        agents = (agent,)
    for agent in agents:
        units = [unit for unit in value["units"] if unit["unit_id"]["agent"] == agent]
        lines += ["", f'<a id="{agent}"></a>', f"## {_agent_name(agent)}"]
        if report_context:
            lines += ["", "**Assigned To:** " + _markdown_text(report_context.assignments[agent])]
        lines += [
            "", "| Run num | Agent version | Expected insight | Generated insight(s) | Assessment | Notes |",
            "| --- | --- | --- | --- | --- | --- |",
            *[_table_row(
                number, unit, context, private.get(_identity(unit), {}) if private is not None else {},
                private=private is not None,
            ) for number, unit in enumerate(units, 1)],
        ]
    if private is not None:
        lines += ["", "Expand Assessment details for full saved rationales, not new judgments or automatic "
                  "Agent-fix recommendations. Original evidence remains in the retained private artifacts."]
    if delivery_id:
        lines += ["", f"Run: {delivery_id}."]
    if metadata:
        lines += ["", " | ".join(_metadata_lines(metadata))]
    if warnings:
        lines += ["", *warning_text(warnings)]
    return "\n".join(lines).rstrip() + "\n"


def render_html(
    result: QualityResult, *, allowed_units: Iterable[PlannedUnit],
    warnings: tuple[str, ...] = (),
    report_context: ReviewedReportContext | None = None,
    metadata: ReportMetadata | None = None,
) -> str:
    return markdown_view(render_markdown(
        result, allowed_units=allowed_units, warnings=warnings,
        report_context=report_context, metadata=metadata,
    ))


def markdown_view(markdown: str) -> str:
    """Browser view of our emitted Markdown subset; never a second report model."""
    def text(line):
        line = re.sub(r"\\([\\`*\[\]|_])", r"\1", line)
        return escape(unescape(line))

    def inline(line):
        # Recognize exact generated markup before decoding untrusted entities.
        if line.startswith("**Assigned To:** "):
            return "<strong>Assigned To:</strong> " + text(line[len("**Assigned To:** "):])
        parts, end = [], 0
        for match in _DETAILS.finditer(line):
            parts.append("<br>".join(text(part) for part in line[end:match.start()].split("<br>")))
            body = []
            for part in _DETAIL_LABEL.split(match[1]):
                if _DETAIL_LABEL.fullmatch(part):
                    body.append(part)
                else:
                    body.append("<br>".join(escape(unescape(value)) for value in part.split("<br>")))
            parts.append("<details><summary>Assessment details</summary>" + "".join(body) + "</details>")
            end = match.end()
        parts.append("<br>".join(text(part) for part in line[end:].split("<br>")))
        return "".join(parts)

    def cells(line):
        result, start, slashes = [], 0, 0
        for index, character in enumerate(line):
            if character == "|" and slashes % 2 == 0:
                result.append(line[start:index].strip())
                start = index + 1
            slashes = slashes + 1 if character == "\\" else 0
        return [*result, line[start:].strip()]

    parts, table_rows = [], []
    def flush_table():
        if table_rows:
            widths = (6, 12, 18, 25, 12, 27) if len(table_rows[0]) == 6 else None
            parts.append(html_table(
                tuple(table_rows[0]), table_rows[1:],
                raw_cells={(row, column) for row in range(len(table_rows) - 1)
                           for column in range(len(table_rows[0]))},
                widths=widths,
            ))
            table_rows.clear()

    for line in markdown.splitlines():
        anchor = re.fullmatch(r'<a id="([a-z][a-z0-9-]*)"></a>', line)
        heading = re.fullmatch(r"(#{1,4}) (.*)", line)
        if line.startswith("| ") and line.endswith(" |"):
            row = cells(line[1:-1])
            if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in row):
                table_rows.append([inline(cell) for cell in row])
            continue
        flush_table()
        if anchor:
            parts.append(f'<a id="{anchor[1]}"></a>')
        elif heading:
            level = len(heading[1])
            parts.append(f"<h{level}>{inline(heading[2])}</h{level}>")
        elif line.strip():
            parts.append(f"<p>{inline(line)}</p>")
    flush_table()
    body = (
        f'<tr><td style="padding:24px 28px;{_FONT}">'
        '<p style="color:#64748b;">Browser view derived from the authoritative report.md.</p>'
        + "".join(parts) + "</td></tr>"
    )
    return _html_page(body, metadata=None)


_FONT = "font-family:Segoe UI,Arial,sans-serif;font-size:14px;line-height:21px;"


def _paragraph(text: str) -> str:
    return f'<p style="margin:8px 0;color:#475569;{_FONT}">{escape(text)}</p>'


def html_table(
    headers: tuple[str, ...], rows: list[tuple[str, ...]], *,
    raw_cells: set[tuple[int, int]] | None = None,
    widths: tuple[int, ...] | None = None,
) -> str:
    """Only explicitly constructed markup cells bypass escaping."""
    raw_cells = raw_cells or set()
    if widths is not None and (
        len(widths) != len(headers) or sum(widths) != 100
        or any(type(width) is not int or width <= 0 for width in widths)
    ):
        raise ReportContextError("report_table_invalid")
    columns = (
        "<colgroup>" + "".join(f'<col style="width:{width}%">' for width in widths) + "</colgroup>"
        if widths else ""
    )
    header = "".join(
        f'<th scope="col" align="left" style="padding:11px 12px;border-bottom:2px solid #cbd8e7;'
        f'color:#12304a;{_FONT}">{escape(label)}</th>' for label in headers
    )
    body = []
    for index, row in enumerate(rows):
        if len(row) != len(headers):
            raise ReportContextError("report_table_invalid")
        cells = "".join(
            f'<td style="padding:12px;border-bottom:1px solid #d6deea;vertical-align:top;'
            f'color:#334155;overflow-wrap:anywhere;word-wrap:break-word;{_FONT}">'
            f'{cell if (index, column) in raw_cells else escape(cell)}</td>'
            for column, cell in enumerate(row)
        )
        background = "#ffffff" if index % 2 == 0 else "#f8fafc"
        body.append(f'<tr bgcolor="{background}">{cells}</tr>')
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;table-layout:fixed;border-collapse:collapse;border:1px solid #d6deea;">'
        f'{columns}<thead><tr bgcolor="#e8eef7">{header}</tr></thead><tbody>{"".join(body)}</tbody></table>'
    )


def html_section(title: str, body: str, *, anchor: str | None = None) -> str:
    identity = f' id="{escape(anchor, quote=True)}"' if anchor else ""
    return (
        f'<tr><td style="padding:22px 28px 4px;{_FONT}">'
        f'<h2{identity} style="margin:0 0 12px;color:#12304a;font-size:20px;line-height:27px;">'
        f'{escape(title)}</h2>{body}</td></tr>'
    )


def _html_page(body: str, *, metadata: ReportMetadata | None, test_run: bool = False) -> str:
    date_line = (
        f"Daily report &middot; {escape(metadata.report_date)} &middot; {escape(metadata.region_display)}"
        if metadata else "Daily report"
    )
    test_banner = (
        '<tr><td bgcolor="#e8f2ff" style="padding:13px 28px;color:#164e80;'
        f'{_FONT}"><strong>TEST RUN</strong> &mdash; Private review only. '
        'No public report, trend publication or team email.</td></tr>'
        if test_run else ""
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Agent Insights Quality</title></head>'
        f'<body bgcolor="#f3f6fa" style="margin:0;padding:0;background:#f3f6fa;{_FONT}">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        '<tr><td align="center" style="padding:24px 10px;">'
        '<!--[if mso]><table role="presentation" width="960" cellpadding="0" cellspacing="0" '
        'border="0"><tr><td><![endif]-->'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'bgcolor="#ffffff" style="max-width:960px;width:100%;background:#ffffff;'
        'border:1px solid #d6deea;border-collapse:collapse;">'
        '<tr><td bgcolor="#12304a" style="padding:28px;color:#ffffff;border-top:5px solid #42b8c6;">'
        f'<p style="margin:0 0 8px;color:#b9d4eb;{_FONT}">DAILY QUALITY BRIEF</p>'
        '<h1 style="margin:0 0 10px;font-family:Segoe UI,Arial,sans-serif;font-size:28px;'
        f'line-height:35px;color:#ffffff;">Agent Insights Quality</h1>'
        f'<p style="margin:0;color:#dbeafe;{_FONT}">{date_line}</p></td></tr>'
        f'{test_banner}{body}<tr><td style="height:28px;"></td></tr></table>'
        '<!--[if mso]></td></tr></table><![endif]-->'
        '</td></tr></table></body></html>'
    )


def _agent_name(name: str) -> str:
    return name.removesuffix("-agent").replace("-", " ").title()


def _html_summary(value: dict, context: dict, details_href, scoring_link, warnings: tuple[str, ...], access_links=None) -> str:
    counts, coverage = value["counts"], value["coverage"]
    score = (
        '<strong style="font-size:28px;line-height:36px;color:#12304a;">'
        f'{value["score"]:.1f}</strong><span style="color:#64748b;"> / 100</span>'
        if value["score"] is not None else "Unmeasured (no quality score)"
    )
    rows = [
        ("Quality score", score),
        ("Expected issues", f'{coverage["planned_issues"]} expected; '
         f'{counts["correct_issues"]} detected, '
         f'{counts["expected_issues"] - counts["correct_issues"]} missed'
         + (f'; {coverage["planned_issues"] - coverage["scored_issues"]} unscored'
            if coverage["planned_issues"] != coverage["scored_issues"] else "")),
        ("Noise / Duplicate", f'{counts["noise_cards"]} Noise; {counts["duplicate_cards"]} Duplicate'),
        ("How Scoring Works",
         f'<a href="{escape(scoring_link.href, quote=True)}" style="color:#0067b8;">Scoring rules on GitHub</a>'
         if scoring_link else "Link pending publication of the current scoring rules."),
    ]
    banner = ""
    if not value["team_report_eligible"]:
        banner = (
            '<p style="margin:0 0 16px;padding:14px;border-left:4px solid #c47f15;'
            'background:#fff5e4;color:#704c16;"><strong>PERSONAL NOTICE</strong><br>'
            'A reliable overall measurement is unavailable. No team quality report is produced. '
            'The counts below describe only the scorable subset, not an overall quality score.</p>'
        )
    body = banner + html_table(("Summary", "Result"), rows, raw_cells={(0, 1), (3, 1)})
    exclusions = [unit for unit in value["units"] if not unit["scorable"]]
    if exclusions:
        references = ", ".join(_detail_link(unit, context, details_href, access_links=access_links) for unit in exclusions)
        body += (
            f'<p style="margin:12px 0;color:#704c16;{_FONT}">'
            f'{len(exclusions)} unit(s) excluded from every score count; evidence is not complete. '
            f'Human validation: {references}.</p>'
        )
    if value["failure_reasons"]:
        body += _paragraph("Private failure notice: " + ", ".join(value["failure_reasons"]))
    if "logging_failed" in warnings:
        body += _paragraph(warning_text(("logging_failed",))[0])
    return html_section("Summary", body)


def _detail_link(unit: dict, context: dict, details_href: str | None, *, short: bool = False, access_links=None) -> str:
    label = unit["unit_id"]["logical_version"] if short else _unit_name(unit, context)
    agent = unit["unit_id"]["agent"]
    href = access_links[agent]["html"] if access_links else (
        f"{details_href}#{agent}" if details_href else None
    )
    if href is None:
        return escape(label)
    return (
        f'<a href="{escape(href, quote=True)}" '
        f'style="color:#0067b8;text-decoration:underline;">{escape(label)}</a>'
    )


def _html_improvements(value: dict, context: dict, details_href: str | None, access_links=None) -> str:
    specifications = (
        ("Missed expected defects",
         lambda unit: unit["kind"] == "issue" and not unit["counts"]["correct_issues"],
         "independently evidenced issue(s) had no correct current detection",
         "Detect the evidenced root cause with a reasonable category and attributable current evidence."),
        ("Incorrect findings (Noise)",
         lambda unit: unit["counts"]["noise_cards"] > 0,
         "unit(s) contained confirmed core-incorrect findings",
         "Ground the diagnosis in actual endpoint behavior and current trace evidence. "
         "Wording, severity or proposed-fix disagreement alone is not Noise."),
        ("Repeated root causes (Duplicate)",
         lambda unit: unit["counts"]["duplicate_cards"] > 0,
         "unit(s) contained extra distinct correct cards for the same root",
         "Deduplicate the same proven root without suppressing different real defects. "
         "Page copies and same-ID updates are not duplicates."),
    )
    rows = []
    for title, matches, observation, needed in specifications:
        affected = [unit for unit in value["units"] if unit["scorable"] and matches(unit)]
        if affected:
            links = "<br>".join(_detail_link(unit, context, details_href, access_links=access_links) for unit in affected)
            rows.append((title, f"{len(affected)} {escape(observation)}.<br><br>{links}", needed))
    body = (
        html_table(("Product gap", "What happened", "Needed behavior"), rows,
                   raw_cells={(index, 1) for index in range(len(rows))})
        if rows else _paragraph("No confirmed Engine gap in the measured scope.")
    )
    return html_section("What needs improvement", body)


def _html_working(value: dict) -> str:
    counts = value["counts"]
    rows = []
    if counts["correct_issues"]:
        rows.append(("Expected-defect detection",
                     f'{counts["correct_issues"]} of {counts["expected_issues"]} scorable issues '
                     "were correctly detected using independent current evidence."))
    baselines = [unit for unit in value["units"] if unit["kind"] == "baseline" and unit["scorable"]]
    if baselines:
        without_noise = sum(not unit["counts"]["noise_cards"] for unit in baselines)
        rows.append(("Baseline finding quality",
                     f"{without_noise} of {len(baselines)} scorable baselines had no confirmed Noise. "
                     "This is not a claim of perfect Agent health."))
    body = html_table(("Capability", "Evidence"), rows) if rows else _paragraph(
        "No positive capability conclusion is supported by the available scorable scope."
    )
    return html_section("What is working", body)


def _html_agents(
    value: dict, context: dict, details_href: str | None, assignments: dict, agent_links: dict,
    *, attached_report: bool, access_links=None, access_expiry=None,
) -> str:
    rows = []
    for agent in dict.fromkeys(unit["unit_id"]["agent"] for unit in value["units"]):
        units = [unit for unit in value["units"] if unit["unit_id"]["agent"] == agent]
        issues = [unit for unit in units if unit["kind"] == "issue"]
        scored = [unit for unit in units if unit["scorable"]]
        kind = ""
        if context:
            source = context[_identity(units[0])].source_path
            kind = "Prompt" if source.endswith("definition.json") else "Hosted"
        name = escape(_agent_name(agent))
        if agent in agent_links:
            href = validate_foundry_link(agent_links[agent])
            name = f'<a href="{escape(href, quote=True)}" style="color:#0067b8;">{name}</a>'
        else:
            name += "<br><small>Foundry link unavailable</small>"
        if kind:
            name += f"<br><small>{kind}</small>"
        missed = sum(unit["scorable"] and not unit["counts"]["correct_issues"] for unit in issues)
        card_text = (
            f'{missed} missed; {sum(unit["counts"]["noise_cards"] for unit in scored)} Noise; '
            f'{sum(unit["counts"]["duplicate_cards"] for unit in scored)} Duplicate'
        )
        references = (
            f'<a href="{escape(details_href, quote=True)}#{agent}" style="color:#0067b8;">'
            "Review findings &amp; evidence</a>" if details_href else
            f"Attached report.md: {_agent_name(agent)}" if attached_report else
            "Detailed report link unavailable"
        )
        if access_links:
            references = (
                f'<a href="{escape(access_links[agent]["html"], quote=True)}" '
                'style="color:#0067b8;">View report</a><br>'
                f'<a href="{escape(access_links[agent]["markdown"], quote=True)}" '
                'style="color:#0067b8;">Download MD</a>'
            )
        rows.append((name, card_text, references, assignments.get(agent, "Assignment unavailable")))
    body = html_table(
        ("Agent", "Findings", "Human Validation", "Assigned To"), rows,
        raw_cells={(index, column) for index in range(len(rows)) for column in (0, 2)},
    )
    if access_links:
        body += _paragraph(
            f"Links expire {access_expiry} (up to 7 days). "
            "Anyone holding a link can read that file; forward carefully."
        )
    return html_section("Test Agents", body)


def render_email_html(
    result: QualityResult, *, allowed_units: Iterable[PlannedUnit],
    warnings: tuple[str, ...] = (), report_context: ReviewedReportContext | None = None,
    metadata: ReportMetadata | None = None, test_run: bool = False,
    details_href: str | None = None, delivery_id: str | None = None,
    scoring_link: VerifiedScoringLink | None = None,
    agent_links: dict[str, str] | None = None, attached_report: bool = False,
    report_access=None,
) -> str:
    """Compact email projection; private context is inserted by the email boundary."""
    if details_href not in {None, "report.html"}:
        raise ReportContextError("report_detail_link_invalid")
    if delivery_id is not None and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", delivery_id) is None:
        raise ReportContextError("report_delivery_identity_invalid")
    if scoring_link is not None and type(scoring_link) is not VerifiedScoringLink:
        raise ReportContextError("report_scoring_link_unverified")
    warning_text(warnings)
    value, context = _inputs(result, allowed_units, report_context, metadata)
    agent_links = {} if agent_links is None else agent_links
    if not isinstance(agent_links, dict) or not set(agent_links) <= {
        unit["unit_id"]["agent"] for unit in value["units"]
    }:
        raise ReportContextError("report_foundry_link_invalid")
    for link in agent_links.values():
        validate_foundry_link(link)
    access_links = None
    if report_access is not None:
        from .report_access import VerifiedReportAccess
        if type(report_access) is not VerifiedReportAccess:
            raise ReportContextError("report_access_unverified")
        access_links = report_access.for_agents(
            {unit["unit_id"]["agent"] for unit in value["units"]}, delivery_id,
        )
    parts = [
        _html_summary(value, context, details_href, scoring_link, warnings, access_links),
        _html_improvements(value, context, details_href, access_links),
        _html_working(value),
        _html_agents(value, context, details_href, report_context.assignments if report_context else {},
                     agent_links, attached_report=attached_report, access_links=access_links,
                     access_expiry=report_access.expires_at if report_access else None),
        "<!--private-context-->",
    ]
    return _html_page("".join(parts), metadata=metadata, test_run=test_run)


def render_json(
    result: QualityResult, *, allowed_units: Iterable[PlannedUnit],
) -> str:
    return json.dumps(
        public_projection(result, allowed_units=allowed_units),
        sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False,
    ) + "\n"
