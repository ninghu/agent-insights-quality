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
        f"{value['status']} - Quality score: {score}",
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
    parts = ["<!doctype html><html><body><h1>Agent Insights quality</h1>"]
    parts += [f"<p>{escape(line)}</p>" for line in _metadata_lines(metadata) + _summary(value)]
    if value["failure_reasons"]:
        parts.append("<p>Private failure notice: "
                     + escape(", ".join(value["failure_reasons"])) + "</p>")
    parts.append(
        "<table><thead><tr><th>Unit</th><th>Outcome / exclusion</th>"
        "<th>Confirmed Engine gaps</th><th>Other / unscored findings</th>"
        "</tr></thead><tbody>"
    )
    parts += [
        "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>"
        for row in _rows(value, context)
    ]
    parts.append("</tbody></table>")
    if context:
        parts += ["<h2>Reviewed contracts and follow-up</h2>", f"<p>{escape(_CONTEXT_GUIDANCE)}</p>"]
    for title, notes in _details(value, context):
        parts.append(f"<h3>{escape(title)}</h3>")
        parts += [f"<p><strong>{escape(label)}:</strong> {escape(text)}</p>" for label, text in notes]
    parts += [f"<p>{escape(line)}</p>" for line in warning_text(warnings)]
    parts.append("</body></html>")
    return "".join(parts)


def render_json(
    result: QualityResult, *, allowed_units: Iterable[PlannedUnit],
) -> str:
    return json.dumps(
        public_projection(result, allowed_units=allowed_units),
        sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False,
    ) + "\n"
