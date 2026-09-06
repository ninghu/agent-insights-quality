"""Read-only renderers of the one aggregated result. No traffic or sink writes."""

from __future__ import annotations

from collections.abc import Iterable
from html import escape
import json
import re

from .privacy import public_projection, warning_text
from .report_context import ReportContextError, ReportMetadata, ReviewedReportContext
from .results import PlannedUnit, QualityResult, UnitId


def _identity(unit: dict) -> UnitId:
    return UnitId(**unit["unit_id"])


def _unit_name(unit: dict, context: dict) -> str:
    identity = _identity(unit)
    name = f"{identity.agent} / {identity.logical_version}"
    return f"{name} - {context[identity].title}" if context else name


def _rows(value: dict, context: dict) -> list[tuple[str, str, str, str]]:
    rows = []
    for unit in value["units"]:
        name = _unit_name(unit, context)
        counts = unit["counts"]
        if not unit["scorable"]:
            outcome = "Unscored: " + ", ".join(unit["exclusion_reasons"])
        elif unit["kind"] == "baseline":
            outcome = "Baseline measured (no healthy bonus)"
        else:
            outcome = "Detected" if counts["correct_issues"] else "Missed"
        gaps = []
        if unit["scorable"]:
            if unit["kind"] == "issue" and not counts["correct_issues"]:
                gaps.append("Expected defect not correctly detected")
            if counts["noise_cards"]:
                gaps.append(f"{counts['noise_cards']} Noise")
            if counts["duplicate_cards"]:
                gaps.append(f"{counts['duplicate_cards']} Duplicate")
        other = []
        unexpected = sum(
            finding["classification"] == "unexpected_real" for finding in unit["findings"]
        )
        if unexpected:
            other.append(f"{unexpected} unexpected real finding(s)")
        unscored = [
            finding for finding in unit["findings"]
            if not finding["scored"] and finding["contribution"] == "current"
        ]
        for classification in sorted({finding["classification"] for finding in unscored}):
            count = sum(finding["classification"] == classification for finding in unscored)
            other.append(f"{count} unscored {classification}")
        rows.append((name, outcome, "; ".join(gaps) or "None confirmed",
                     "; ".join(other) or "None reported"))
    return rows


def _follow_up(unit: dict) -> tuple[tuple[str, str], ...]:
    notes = []
    if not unit["scorable"]:
        notes.append((
            "Measurement follow-up",
            "This whole unit is excluded: " + ", ".join(unit["exclusion_reasons"])
            + ". Resolve the execution, evidence or assessment gap in the retained private "
            "checkpoint before drawing a quality conclusion. This is not a confirmed Engine miss.",
        ))
    elif unit["kind"] == "issue" and not unit["counts"]["correct_issues"]:
        notes.append((
            "Engine follow-up",
            "The reviewed defect was independently evidenced, but no current card correctly "
            "detected it. Check diagnosis and current evidence linkage for the expected symptom "
            "below using the retained pre-Insights evidence. The healthy behavior is an Agent "
            "reference, not a claim that the Engine itself should implement the Agent fix.",
        ))
    roots = {}
    for finding in unit["findings"]:
        classification = finding["classification"]
        root = finding["root_cause_alias"]
        alias = finding["card_alias"]
        scope = "scored" if finding["scored"] else "unscored"
        if classification == "historical":
            action = "Historical context only; no current score contribution."
        elif classification == "expected_detection":
            action = "Correct detection of the reviewed expected defect."
            roots[root] = alias
        elif classification == "unexpected_real":
            action = (
                "Inspect the independently supported Agent problem outside the expected defect. "
                "It is not Noise and earns no expected-issue credit."
            )
            roots[root] = alias
        elif classification == "noise":
            action = (
                "Check the core diagnosis, category and linkage against retained current evidence; "
                "the core claim is confirmed incorrect. Severity/fix disagreement alone is not Noise."
            )
        elif classification == "duplicate":
            action = (
                f"Same independently supported root as {roots[root]}. Check same-root "
                "deduplication across distinct cards without suppressing different real defects."
            )
        else:
            action = (
                "The core diagnosis is unconfirmed. Resolve it from retained current evidence, "
                "not catalog wording or the card's own claim."
            )
        reference = f" Root: {root}." if root is not None else ""
        notes.append((f"{alias}: {classification} ({scope})", action + reference))
    return tuple(notes)


def _details(value: dict, context: dict) -> list[tuple[str, tuple[tuple[str, str], ...]]]:
    sections = []
    for unit in value["units"]:
        notes = []
        if context:
            reviewed = context[_identity(unit)]
            notes.extend((
                ("Reviewed expected symptom" if unit["kind"] == "issue" else "Reviewed baseline",
                 reviewed.expected_symptom),
                ("Healthy Agent behavior to compare", reviewed.healthy_behavior),
                ("Reproduction reference", f"{reviewed.traffic_path}; source: {reviewed.source_path}"),
            ))
        notes.extend(_follow_up(unit))
        if notes:
            sections.append((_unit_name(unit, context), tuple(notes)))
    return sections


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


_CONTEXT_GUIDANCE = (
    "Reviewed catalog context describes intended defects, not proof that they occurred. "
    "Reproduction references identify the version-local traffic and complete source at the "
    "reported commit. Each traffic file defines ten attempts; preserve the setup/probe turn "
    "order and separate attempt conversations when reproducing through the deployed endpoint. "
    "These references do not authorize new traffic or resampling. Retained private assessment "
    "artifacts contain the actual reasons, citations and endpoint/trace evidence."
)


def _markdown_text(text: str) -> str:
    # Escape markup, not content. Approval is established before rendering.
    text = escape(text, quote=False)
    for character in ("\\", "`", "*", "[", "]", "|"):
        text = text.replace(character, "\\" + character)
    return re.sub(r"(?<!\w)_|_(?!\w)", r"\\_", text)


def _summary(value: dict) -> tuple[str, ...]:
    counts, coverage = value["counts"], value["coverage"]
    score = "Unmeasured (no quality score)" if value["score"] is None else f"{value['score']:.1f}"
    return (
        f"Quality score: {score}",
        f"C={counts['correct_issues']}; E_scored={counts['expected_issues']}; "
        f"N_scored={counts['noise_cards']}; D_scored={counts['duplicate_cards']}. "
        "Noise weight 1; Duplicate weight 0.25.",
        f"Issue coverage: {coverage['scored_issues']}/{coverage['planned_issues']} planned; "
        f"baseline coverage: {coverage['scored_baselines']}/{coverage['planned_baselines']} planned; "
        f"excluded whole units: {coverage['excluded_units']}.",
        "Score = 100*C/(E_scored+N_scored+0.25*D_scored). No overall quality threshold.",
        "Engine gaps below are confirmed only for scorable units. Unexpected real findings "
        "are Agent problems, not Noise. Execution, evidence and assessment exclusions are "
        "framework/infrastructure or unresolved measurement problems, not proven Engine misses.",
    )


def render_markdown(
    result: QualityResult, *, allowed_units: Iterable[PlannedUnit],
    warnings: tuple[str, ...] = (),
    report_context: ReviewedReportContext | None = None,
    metadata: ReportMetadata | None = None,
) -> str:
    value, context = _inputs(result, allowed_units, report_context, metadata)
    lines = [
        "# Agent Insights quality", "",
        *[line + "\n" for line in _metadata_lines(metadata) + _summary(value)],
    ]
    if value["failure_reasons"]:
        lines += ["Private failure notice: " + ", ".join(value["failure_reasons"]), ""]
    lines += [
        "| Unit | Outcome / exclusion | Confirmed Engine gaps | Other / unscored findings |",
        "| --- | --- | --- | --- |",
        *["| " + " | ".join(_markdown_text(cell) for cell in row) + " |"
          for row in _rows(value, context)],
    ]
    if context:
        lines += ["", "## Reviewed contracts and follow-up", "", _CONTEXT_GUIDANCE]
    for title, notes in _details(value, context):
        lines += ["", "### " + _markdown_text(title)]
        for label, text in notes:
            lines += ["", f"**{_markdown_text(label)}:** {_markdown_text(text)}"]
    lines += ["", *warning_text(warnings)]
    return "\n".join(lines).rstrip() + "\n"


def render_html(
    result: QualityResult, *, allowed_units: Iterable[PlannedUnit],
    warnings: tuple[str, ...] = (),
    report_context: ReviewedReportContext | None = None,
    metadata: ReportMetadata | None = None,
) -> str:
    value, context = _inputs(result, allowed_units, report_context, metadata)
    parts = [_html_summary(value, context)]
    rows = _rows(value, context)
    linked_rows = [
        (f'<span id="{_unit_anchor(unit)}">{escape(row[0])}</span>', *row[1:])
        for unit, row in zip(value["units"], rows, strict=True)
    ]
    parts.append(html_section("Unit results", html_table(
        ("Unit", "Outcome / exclusion", "Confirmed Engine gaps", "Other / unscored findings"),
        linked_rows, raw_cells={(index, 0) for index in range(len(rows))},
    )))
    if context:
        parts.append(html_section("Reviewed contracts and follow-up", _paragraph(_CONTEXT_GUIDANCE)))
    for title, notes in _details(value, context):
        parts.append(html_section(title, "".join(
            f"<p><strong>{escape(label)}:</strong> {escape(text)}</p>" for label, text in notes
        )))
    parts.append(_html_methodology(value, metadata, warnings))
    return _html_page("".join(parts), metadata=metadata)


_FONT = "font-family:Segoe UI,Arial,sans-serif;font-size:14px;line-height:21px;"
_REASONS = {
    "missing_result": "Result not available",
    "incomplete_execution": "Execution could not be completed",
    "incomplete_evidence": "Insufficient attributable evidence",
    "incomplete_assessment": "Assessment could not be completed",
    "unknown_core": "Finding's core diagnosis remains unresolved",
}


def _paragraph(text: str) -> str:
    return f'<p style="margin:8px 0;color:#475569;{_FONT}">{escape(text)}</p>'


def html_table(
    headers: tuple[str, ...], rows: list[tuple[str, ...]], *,
    raw_cells: set[tuple[int, int]] | None = None,
) -> str:
    """Only explicitly constructed markup cells bypass escaping."""
    raw_cells = raw_cells or set()
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
        f'<thead><tr bgcolor="#e8eef7">{header}</tr></thead><tbody>{"".join(body)}</tbody></table>'
    )


def html_section(title: str, body: str, *, anchor: str | None = None) -> str:
    identity = f' id="{escape(anchor, quote=True)}"' if anchor else ""
    return (
        f'<tr><td style="padding:22px 28px 4px;{_FONT}">'
        f'<h2{identity} style="margin:0 0 12px;color:#12304a;font-size:20px;line-height:27px;">'
        f'{escape(title)}</h2>{body}</td></tr>'
    )


def _html_page(body: str, *, metadata: ReportMetadata | None, test_run: bool = False) -> str:
    date_line = f"Daily report &middot; {escape(metadata.report_date)}" if metadata else "Daily report"
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


def _unit_anchor(unit: dict) -> str:
    identity = _identity(unit)
    return f"unit-{identity.agent}-{identity.logical_version}"


def _agent_name(name: str) -> str:
    return name.removesuffix("-agent").replace("-", " ").title()


def _html_summary(value: dict, context: dict) -> str:
    counts, coverage = value["counts"], value["coverage"]
    score = (
        '<strong style="font-size:28px;line-height:36px;color:#12304a;">'
        f'{value["score"]:.1f}</strong><span style="color:#64748b;"> / 100</span>'
        if value["score"] is not None else "Unmeasured (no quality score)"
    )
    rows = [
        ("Quality score", score),
        ("Issue coverage", f'{coverage["scored_issues"]}/{coverage["planned_issues"]} planned; '
         f'{counts["correct_issues"]} detected, '
         f'{counts["expected_issues"] - counts["correct_issues"]} missed, '
         f'{coverage["planned_issues"] - coverage["scored_issues"]} unscored'),
        ("Baseline coverage", f'{coverage["scored_baselines"]}/{coverage["planned_baselines"]} planned'
         " (no healthy bonus)"),
        ("Finding quality", f'{counts["noise_cards"]} Noise cards (weight 1); '
         f'{counts["duplicate_cards"]} Duplicate cards (weight 0.25)'),
        ("Measurement exclusions", f'excluded whole units: {coverage["excluded_units"]}'),
    ]
    banner = ""
    if not value["team_report_eligible"]:
        banner = (
            '<p style="margin:0 0 16px;padding:14px;border-left:4px solid #c47f15;'
            'background:#fff5e4;color:#704c16;"><strong>PERSONAL NOTICE</strong><br>'
            'A reliable overall measurement is unavailable. No team quality report is produced. '
            'The counts below describe only the scorable subset, not an overall quality score.</p>'
        )
    body = banner + html_table(("Summary", "Result"), rows, raw_cells={(0, 1)})
    body += _paragraph("Score change: not compared. No comparable prior measurement is attached; "
                       "rotating cases or different exclusions must not imply improvement.")
    exclusions = []
    for unit in value["units"]:
        if not unit["scorable"]:
            reasons = "; ".join(
                f"{_REASONS[reason]} ({reason})" for reason in unit["exclusion_reasons"]
            )
            exclusions.append((_unit_name(unit, context), reasons))
    if exclusions:
        body += _paragraph(
            "Excluded units are measurement follow-ups, not a confirmed Engine miss. "
            "Their detections, Noise and Duplicate counts are excluded together."
        )
        body += html_table(("Excluded unit", "Why it could not be scored"), exclusions)
    if value["failure_reasons"]:
        body += _paragraph("Private failure notice: " + ", ".join(value["failure_reasons"]))
    return html_section("Summary", body)


def _detail_link(unit: dict, context: dict, details_href: str | None, *, short: bool = False) -> str:
    label = unit["unit_id"]["logical_version"] if short else _unit_name(unit, context)
    if details_href is None:
        return escape(label)
    return (
        f'<a href="{escape(details_href, quote=True)}#{_unit_anchor(unit)}" '
        f'style="color:#0067b8;text-decoration:underline;">{escape(label)}</a>'
    )


def _html_improvements(value: dict, context: dict, details_href: str | None) -> str:
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
            links = "<br>".join(_detail_link(unit, context, details_href) for unit in affected)
            rows.append((title, f"{len(affected)} {escape(observation)}.<br><br>{links}", needed))
    body = (
        html_table(("Product gap", "What happened", "Needed behavior"), rows,
                   raw_cells={(index, 1) for index in range(len(rows))})
        if rows else _paragraph("No confirmed Engine gap in the scorable scope. "
                                "This does not establish that excluded units are healthy.")
    )
    body += _paragraph(
        "These are confirmed Engine gaps, not a checklist of Agent fixes. "
        "Unit references identify the retained assessments; reviewed catalog descriptions "
        "alone are not proof of a defect."
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
    value: dict, context: dict, details_href: str | None, metadata: ReportMetadata | None,
) -> str:
    rows = []
    for agent in dict.fromkeys(unit["unit_id"]["agent"] for unit in value["units"]):
        units = [unit for unit in value["units"] if unit["unit_id"]["agent"] == agent]
        issues = [unit for unit in units if unit["kind"] == "issue"]
        baselines = [unit for unit in units if unit["kind"] == "baseline"]
        scored = [unit for unit in units if unit["scorable"]]
        kind = ""
        if context:
            source = context[_identity(units[0])].source_path
            kind = "Prompt" if source.endswith("definition.json") else "Hosted"
        name = escape(_agent_name(agent)) + (f"<br><small>{kind}</small>" if kind else "")
        issue_text = (
            f'{sum(unit["counts"]["correct_issues"] for unit in issues)} detected / '
            f'{sum(unit["scorable"] for unit in issues)} scored / {len(issues)} planned'
        )
        baseline_text = f'{sum(unit["scorable"] for unit in baselines)}/{len(baselines)} scored'
        card_text = (
            f'{sum(unit["counts"]["noise_cards"] for unit in scored)} Noise / '
            f'{sum(unit["counts"]["duplicate_cards"] for unit in scored)} Duplicate'
        )
        references = ", ".join(
            _detail_link(unit, {}, details_href, short=True) for unit in units
        )
        rows.append((name, issue_text, baseline_text, card_text, references))
    location = _paragraph("Test region: " + metadata.region_display) if metadata else ""
    return html_section("Test Agents", location + html_table(
        ("Agent", "Issues", "Baseline", "Findings", "Version references"), rows,
        raw_cells={(index, column) for index in range(len(rows)) for column in (0, 4)},
    ))


def _html_other(value: dict, context: dict, details_href: str | None) -> str:
    rows = []
    for unit in value["units"]:
        for finding in unit["findings"]:
            kind = finding["classification"]
            if kind == "historical" or finding["scored"] and kind != "unexpected_real":
                continue
            note = (
                "Independently supported Agent problem outside the expected defect; "
                "not Noise and no additional expected-issue credit."
                if kind == "unexpected_real" else
                "Retained for follow-up only; this finding does not contribute to the score."
            )
            rows.append((_detail_link(unit, context, details_href),
                         f'{finding["card_alias"]}: {kind}'
                         + (" (unscored)" if not finding["scored"] else ""), note))
    if not rows:
        return ""
    return html_section("Other findings and measurement follow-up", html_table(
        ("Unit", "Finding", "Meaning"), rows,
        raw_cells={(index, 0) for index in range(len(rows))},
    ))


def _html_methodology(value: dict, metadata: ReportMetadata | None, warnings: tuple[str, ...]) -> str:
    summary = _summary(value)
    body = "".join(_paragraph(line) for line in summary[1:2] + summary[3:])
    body += "".join(_paragraph(line) for line in _metadata_lines(metadata))
    body += "".join(_paragraph(line) for line in warning_text(warnings))
    return html_section("How Scoring Works", body, anchor="how-scoring-works")


def render_email_html(
    result: QualityResult, *, allowed_units: Iterable[PlannedUnit],
    warnings: tuple[str, ...] = (), report_context: ReviewedReportContext | None = None,
    metadata: ReportMetadata | None = None, test_run: bool = False,
    details_href: str | None = None, delivery_id: str | None = None,
) -> str:
    """Compact email projection; private context is inserted by the email boundary."""
    if details_href not in {None, "report.html"}:
        raise ReportContextError("report_detail_link_invalid")
    if delivery_id is not None and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", delivery_id) is None:
        raise ReportContextError("report_delivery_identity_invalid")
    value, context = _inputs(result, allowed_units, report_context, metadata)
    parts = [
        _html_summary(value, context),
        _html_improvements(value, context, details_href),
        _html_working(value),
        _html_agents(value, context, details_href, metadata),
        _html_other(value, context, details_href),
        "<!--private-context-->",
        _html_methodology(value, metadata, warnings),
    ]
    if delivery_id:
        parts.append(html_section("Run reference", _paragraph(delivery_id)))
    return _html_page("".join(parts), metadata=metadata, test_run=test_run)


def render_json(
    result: QualityResult, *, allowed_units: Iterable[PlannedUnit],
) -> str:
    return json.dumps(
        public_projection(result, allowed_units=allowed_units),
        sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False,
    ) + "\n"
