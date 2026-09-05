"""Read-only renderers of the one aggregated result. No traffic or sink writes."""

from __future__ import annotations

from collections.abc import Iterable
from html import escape
import json

from .privacy import public_projection, warning_text
from .results import PlannedUnit, QualityResult


def _rows(value: dict) -> list[tuple[str, str, str, str]]:
    rows = []
    for unit in value["units"]:
        identity = unit["unit_id"]
        name = f"{identity['agent']} / {identity['logical_version']}"
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
) -> str:
    value = public_projection(result, allowed_units=allowed_units)
    lines = ["# Agent Insights quality", "", *[line + "\n" for line in _summary(value)]]
    if value["failure_reasons"]:
        lines += ["Private failure notice: " + ", ".join(value["failure_reasons"]), ""]
    lines += [
        "| Unit | Outcome / exclusion | Confirmed Engine gaps | Other / unscored findings |",
        "| --- | --- | --- | --- |",
        *["| " + " | ".join(row) + " |" for row in _rows(value)],
    ]
    lines += ["", *warning_text(warnings)]
    return "\n".join(lines).rstrip() + "\n"


def render_html(
    result: QualityResult, *, allowed_units: Iterable[PlannedUnit],
    warnings: tuple[str, ...] = (),
) -> str:
    value = public_projection(result, allowed_units=allowed_units)
    parts = ["<!doctype html><html><body><h1>Agent Insights quality</h1>"]
    parts += [f"<p>{escape(line)}</p>" for line in _summary(value)]
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
        for row in _rows(value)
    ]
    parts.append("</tbody></table>")
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
