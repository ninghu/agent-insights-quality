from __future__ import annotations

from collections import Counter
from typing import Any


def working_capabilities(report: dict[str, Any]) -> list[tuple[str, str]]:
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


def improvement_rows(report: dict[str, Any]) -> list[tuple[str, str, str]]:
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
    missing = [
        item
        for item in issues
        if not any(
            card.get("finding_type") in {"MATCHED", "PARTIAL", "MISMATCHED"}
            for card in item.get("assessment", {}).get("card_evaluations", [])
        )
    ]
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
