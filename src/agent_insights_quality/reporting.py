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
    text = escape(" ".join(text.splitlines()), quote=False)
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
        f"Issue coverage: {coverage['scored_issues']}/{coverage['planned_issues']} expected; "
        f"baseline coverage: {coverage['scored_baselines']}/{coverage['planned_baselines']} expected; "
        f"excluded whole units: {coverage['excluded_units']}.",
        "Score = 100*C/(E_scored+N_scored+0.25*D_scored). No overall quality threshold.",
        "Engine gaps below are confirmed only for scorable units. Unexpected real findings "
        "need human triage, not automatic Agent fixes or rescoring. Execution, evidence and assessment exclusions are "
        "framework/infrastructure or unresolved measurement problems, not proven Engine misses.",
    )


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
) -> str:
    """Explicit private boundary; never used by public_artifacts or ADX."""
    if type(review_context) is not RetainedReviewContext:
        raise ReportContextError("report_review_context_invalid")
    return _render_markdown(
        result, allowed_units=allowed_units, warnings=warnings,
        report_context=report_context, metadata=metadata, delivery_id=delivery_id,
        private=review_context.for_result(result),
    )


def _extra_action() -> str:
    return (
        "Human confirmation needed, not a confirmed Agent fix task. Compare the exact user request, "
        "delivered output and cited pre-Insights spans with the card's core claim. Establish whether "
        "there is an unresolved contract violation, ambiguous wording, or already-correct behavior. "
        "For a confirmed violation, request an Agent fix and a regression case. If the recommended "
        "behavior already occurs, record 'Already handled / no Agent change requested' with evidence; "
        "do not change working behavior just to remove a finding. If the core claim is unsupported, "
        "request review of the finding/assessment rather than silently relabeling it. Engine behavior: "
        "distinguish defects from successful recovery. unexpected_real alone is not a fix instruction; "
        "as recorded, it is not Noise and earns no expected-issue credit. No frozen result is changed here."
    )


def _evidence_lines(excerpts: tuple[dict, ...]) -> list[str]:
    lines = []
    for item in excerpts[:2]:
        lines += ["", "**Cited endpoint:** " + _markdown_text(
            f"attempt {item['attempt']}, step {item['step_id']}, {item['endpoint_ref']}")]
        for label, key in (("Actual request input", "request"), ("Delivered endpoint output", "response_output")):
            suffix = " [Excerpt truncated; full content remains in the retained artifact.]" if item[key + "_truncated"] else ""
            lines += ["", f"**{label}:** " + _markdown_text(item[key] + suffix)]
    if len(excerpts) > 2:
        lines += ["", f"Showing two of {len(excerpts)} cited endpoints; all references remain in the assessment."]
    return lines


def _render_markdown(
    result, *, allowed_units, warnings, report_context, metadata, delivery_id, private=None,
) -> str:
    value, context = _inputs(result, allowed_units, report_context, metadata)
    if delivery_id is not None and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", delivery_id) is None:
        raise ReportContextError("report_delivery_identity_invalid")
    lines = [
        "# Agent Insights quality", "",
        *([f"Run identity: {delivery_id}", ""] if delivery_id else []),
        *(["Private human-validation report. Retained judgments are quoted, not reassessed.", ""]
          if private is not None else []),
        *[line + "\n" for line in _metadata_lines(metadata) + _summary(value)],
    ]
    if value["failure_reasons"]:
        lines += ["Private failure notice: " + ", ".join(value["failure_reasons"]), ""]
    if context:
        lines += ["", _CONTEXT_GUIDANCE]
    if private is not None:
        lines += [
            "", "For each reviewed case, record confirmed, disputed, or insufficient evidence, "
            "with the supporting references and follow-up owner. Resolve evidence by Agent, version, "
            "response/turn and span branch; an operation ID alone is not an invocation identity. "
            "Distinguish the external user's task and delivered answer from internal model prompts "
            "and intermediate outputs. Review results do not silently replace this frozen measurement.",
        ]
    agents = dict.fromkeys(unit["unit_id"]["agent"] for unit in value["units"])
    for agent in agents:
        units = [unit for unit in value["units"] if unit["unit_id"]["agent"] == agent]
        missed = [u for u in units if u["scorable"] and u["kind"] == "issue"
                  and not u["counts"]["correct_issues"]]
        noise = sum(u["counts"]["noise_cards"] for u in units)
        duplicates = sum(u["counts"]["duplicate_cards"] for u in units)
        lines += [
            "", f'<a id="{agent}"></a>', f"## {_agent_name(agent)}", "",
            f"Confirmed Engine gaps: {len(missed)} missed defects; {noise} Noise; {duplicates} Duplicates.",
        ]
        if report_context:
            lines += ["", "**Assigned To:** " + _markdown_text(report_context.assignments[agent])]
        actionable = [
            unit for unit in units if unit in missed or not unit["scorable"]
            or any(f["classification"] in {"noise", "duplicate", "unexpected_real", "unknown"}
                   for f in unit["findings"])
        ]
        if not actionable:
            lines += ["", "No confirmed gap or unresolved finding in the measured scope; no human action requested."]
        healthy = [u["unit_id"]["logical_version"] for u in units if u not in actionable]
        if healthy:
            lines += ["", "Other measured versions (no detailed follow-up): " + ", ".join(healthy) + "."]
        if private is not None:
            versions = []
            for unit in units:
                retained = private.get(_identity(unit), {})
                deployment = retained.get("deployment")
                if deployment:
                    versions.append(
                        f"{unit['unit_id']['logical_version']} = {deployment['agent_name']} / "
                        f"{deployment['provider_version']} (deployment source {deployment['source_revision']}; "
                        f"traffic source {retained['traffic_source_revision']}, tested {retained['tested_at']})"
                    )
            if versions:
                lines += ["", "**Retained version references:**", *[
                    "\n" + _markdown_text(version) for version in versions
                ]]
        for unit in actionable:
            identity = _identity(unit)
            detail = private.get(identity, {}) if private is not None else {}
            lines += ["", f'<a id="{_unit_anchor(unit)}"></a>',
                      "### " + _markdown_text(_unit_name(unit, context))]
            if not unit["scorable"]:
                lines += ["", "**Measurement exclusion:** " + ", ".join(unit["exclusion_reasons"])
                          + ". All this unit's counts are excluded; not a confirmed Engine miss.",
                          "", "Human validation: inspect the retained execution/evidence/assessment "
                          "checkpoint for the stated gap before drawing a quality conclusion."]
            if unit in missed:
                lines += ["", "**Missed defect:** The reviewed defect was independently evidenced, "
                          "but no current card correctly detected it.",
                          "", "Human validation: compare the observations below with the reviewed "
                          "expected symptom; inspect the retained current card set for a matching "
                          "root diagnosis and evidence linkage. Expected Engine behavior: detect "
                          "that evidenced defect with a reasonable category and attributable current evidence."]
            if context:
                reviewed = context[identity]
                lines += [
                    "", "**Reviewed expected symptom (not proof):** " + _markdown_text(reviewed.expected_symptom),
                    "", "**Healthy Agent reference:** " + _markdown_text(reviewed.healthy_behavior),
                    "", "**Source / reproduction reference:** " + _markdown_text(
                        f"{reviewed.traffic_path}; source: {reviewed.source_path}"),
                ]
            if detail.get("artifact"):
                lines += ["", "**Retained private assessment:** " + _markdown_text(detail["artifact"]),
                          "", "Path is relative to the private Daily runtime directory. Contains "
                          "private_detail.input.cards, private_detail.input.attempts, "
                          "private_detail.input.visible_snapshot and private_detail.resolved judgments. "
                          "Citation row/endpoint references are local to this artifact.",
                          "", "**Measured provenance:** " + _markdown_text(
                              f"traffic {detail['traffic_run_id']}; tested {detail['tested_at']}; "
                              f"assessed {detail['assessed_at']}; traffic source "
                              f"{detail['traffic_source_revision']}; judgment source {detail['source_revision']}.")]
                if detail.get("deployment"):
                    deployment = detail["deployment"]
                    lines += ["", "**Retained deployment:** " + _markdown_text(
                        f"{deployment['agent_name']} / version {deployment['provider_version']} "
                        f"(source {deployment['source_revision']})")]
                if unit in missed:
                    observations = detail["observations"]
                    lines += ["", f"Saved sufficient observations: {len(observations)}/10. "
                              "Representative citations below; all ten judgments remain in the artifact."]
                    for observed in observations[:2]:
                        lines += ["", "**Saved observation:** " + _markdown_text(observed["reason"]),
                                  "", "**Verify evidence:** " + _markdown_text("; ".join(observed["citations"]))]
                        lines += _evidence_lines(observed["endpoint_evidence"])
                if detail["limitations"]:
                    lines += ["", "**Saved uncertainty:** " + ", ".join(detail["limitations"])]
                for name, snapshot in detail["evidence_scope"].items():
                    lines += ["", "**Retained evidence scope:** " + _markdown_text(
                        f"{name}: observed {snapshot.get('observed_at', 'not recorded')}; "
                        f"query complete={snapshot.get('query_complete', 'not recorded')}; "
                        f"gaps={', '.join(snapshot.get('gaps', [])) or 'none recorded'}. "
                        "A complete query is not proof of complete telemetry."
                    )]
            else:
                lines += ["", "**Evidence detail unavailable here:** " + _markdown_text(
                    detail.get("unavailable", "Consult the retained private assessment for actual claims, "
                               "judgment reasons and citations. Catalog context is not proof."))]
            roots = {
                finding["root_cause_alias"]: finding["card_alias"]
                for finding in reversed(unit["findings"])
                if finding["classification"] in {"expected_detection", "unexpected_real"}
            }
            for finding in unit["findings"]:
                classification = finding["classification"]
                if classification in {"expected_detection", "historical"}:
                    continue
                alias = finding["card_alias"]
                card = detail.get("cards", {}).get(alias, {})
                scope = "scored" if finding["scored"] else "unscored"
                lines += ["", f"#### {alias}: {classification} ({scope})"]
                for label, field in (("Actual card", "title"), ("Claim", "claim"), ("Saved judgment", "reason")):
                    if card.get(field):
                        lines += ["", f"**{label}:** " + _markdown_text(card[field])]
                if card.get("provider_card_id"):
                    lines += ["", "**Retained card identity:** " + _markdown_text(card["provider_card_id"])]
                if card.get("citations"):
                    lines += ["", "**Verify evidence:** " + _markdown_text("; ".join(card["citations"]))]
                    lines += _evidence_lines(card["endpoint_evidence"])
                elif private is not None:
                    lines += ["", "This retained card has no available citation detail here; consult its assessment "
                              "artifact rather than treating the card's claim as proof."]
                if classification == "unexpected_real":
                    action = _extra_action()
                elif classification == "noise":
                    action = (
                        "Compare the actual card claim with the saved reason and cited endpoint/spans. "
                        "The core claim was judged incorrect; identify the contradicted assertion, not "
                        "merely a severity or proposed-fix disagreement. If the cited evidence actually "
                        "supports the correctly scoped claim, dispute the assessment rather than requesting "
                        "an Engine fix. Expected Engine behavior: suppress or correct unsupported diagnoses "
                        "and distinguish internal-only observations from delivered-response defects."
                    )
                elif classification == "duplicate":
                    action = (
                        f"Same independently supported root as {roots.get(finding['root_cause_alias'], 'the first correct card')}. "
                        "Compare both distinct card identities and causal claims against the cited evidence. "
                        "Expected Engine behavior: consolidate the same proven root without suppressing "
                        "different defects; page copies and same-ID updates are not duplicates."
                    )
                else:
                    action = (
                        "Resolve the core diagnosis from retained current evidence before requesting a fix. "
                        "Expected Engine behavior: disclose uncertainty rather than assert an unsupported defect."
                    )
                lines += ["", "**Human action:** " + _markdown_text(action)]
    if warnings:
        lines += ["", "## Measurement notes", "", *warning_text(warnings)]
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
    def inline(line):
        # Escape all source markup; only our generated bold labels are interpreted.
        line = re.sub(r"\\([\\`*\[\]|_])", r"\1", line)
        return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escape(unescape(line)))

    parts = []
    for line in markdown.splitlines():
        anchor = re.fullmatch(r'<a id="([a-z][a-z0-9-]*)"></a>', line)
        heading = re.fullmatch(r"(#{1,4}) (.*)", line)
        if anchor:
            parts.append(f'<a id="{anchor[1]}"></a>')
        elif heading:
            level = len(heading[1])
            parts.append(f"<h{level}>{inline(heading[2])}</h{level}>")
        elif line.strip():
            parts.append(f"<p>{inline(line)}</p>")
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


def _unit_anchor(unit: dict) -> str:
    identity = _identity(unit)
    return f"unit-{identity.agent}-{identity.logical_version}"


def _agent_name(name: str) -> str:
    return name.removesuffix("-agent").replace("-", " ").title()


def _html_summary(value: dict, context: dict, details_href, scoring_link, warnings: tuple[str, ...]) -> str:
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
        references = ", ".join(_detail_link(unit, context, details_href) for unit in exclusions)
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


def _detail_link(unit: dict, context: dict, details_href: str | None, *, short: bool = False) -> str:
    label = unit["unit_id"]["logical_version"] if short else _unit_name(unit, context)
    if details_href is None:
        return escape(label)
    return (
        f'<a href="{escape(details_href, quote=True)}#{unit["unit_id"]["agent"]}" '
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
    *, attached_report: bool,
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
        rows.append((name, card_text, references, assignments.get(agent, "Assignment unavailable")))
    return html_section("Test Agents", html_table(
        ("Agent", "Findings", "Human Validation", "Assigned To"), rows,
        raw_cells={(index, column) for index in range(len(rows)) for column in (0, 2)},
    ))


def render_email_html(
    result: QualityResult, *, allowed_units: Iterable[PlannedUnit],
    warnings: tuple[str, ...] = (), report_context: ReviewedReportContext | None = None,
    metadata: ReportMetadata | None = None, test_run: bool = False,
    details_href: str | None = None, delivery_id: str | None = None,
    scoring_link: VerifiedScoringLink | None = None,
    agent_links: dict[str, str] | None = None, attached_report: bool = False,
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
    parts = [
        _html_summary(value, context, details_href, scoring_link, warnings),
        _html_improvements(value, context, details_href),
        _html_working(value),
        _html_agents(value, context, details_href, report_context.assignments if report_context else {},
                     agent_links, attached_report=attached_report),
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
