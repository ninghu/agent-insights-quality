from __future__ import annotations

from typing import Any

SHADOW_SCORE_FORMULA = "coverage_quality_precision_v2"
SHADOW_SCORE_AUTOMATION_AUTHORITY = False
SHADOW_SCORE_REPORT_PROFILES = ("staging",)
SHADOW_USEFUL_COVERAGE_WEIGHT = 0.80
SHADOW_PRECISION_WEIGHT = 0.20
SHADOW_MISMATCHED_QUALITY_CAP = 40.0
SHADOW_CALIBRATION_COMPLETE_RUNS = 3
SHADOW_FIELD_WEIGHTS = {
    "title": 0.05,
    "description": 0.25,
    "category": 0.05,
    "severity": 0.15,
    "proposed_fix": 0.25,
    "linked_traces": 0.25,
}
SHADOW_GATE_THRESHOLDS = {
    "score": 90.0,
    "coverage": 95.0,
    "diagnosis_recall": 95.0,
    "precision": 80.0,
    "baseline_noise_cards": 0,
}

_PRIMARY_FINDING_TYPES = {"MATCHED", "PARTIAL", "MISMATCHED"}


def _display_score(value: float) -> float:
    return round(value, 1)


def _native_card_quality(fields: dict[str, Any]) -> float:
    return 100.0 * sum(
        weight
        for field, weight in SHADOW_FIELD_WEIGHTS.items()
        if fields.get(field) is True
    )


def select_shadow_primary(issue: dict[str, Any]) -> dict[str, Any] | None:
    if issue.get("runtime_evidence_complete") is not True:
        return None
    candidates: list[tuple[float, str, str, bool]] = []
    cards = issue.get("assessment", {}).get("card_evaluations", [])
    for card in cards if isinstance(cards, list) else []:
        if not isinstance(card, dict):
            continue
        finding_type = card.get("finding_type")
        reference = card.get("reference")
        fields = card.get("fields")
        if (
            finding_type not in _PRIMARY_FINDING_TYPES
            or not isinstance(reference, str)
            or not isinstance(fields, dict)
        ):
            continue
        diagnosis_correct = fields.get("root_cause") is True
        quality = _native_card_quality(fields) if diagnosis_correct else 0.0
        if finding_type == "MISMATCHED":
            quality = min(quality, SHADOW_MISMATCHED_QUALITY_CAP)
        candidates.append(
            (quality, reference, str(finding_type), diagnosis_correct)
        )
    if not candidates:
        return None
    quality, reference, finding_type, diagnosis_correct = min(
        candidates,
        key=lambda value: (-value[0], value[1]),
    )
    return {
        "reference": reference,
        "finding_type": finding_type,
        "diagnosis_correct": diagnosis_correct,
        "quality": _display_score(quality),
    }


def calculate_shadow_quality_score(
    baseline: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    *,
    incomplete: bool,
) -> dict[str, Any]:
    expected_issues = len(issues)
    generated_issue_cards = sum(
        len(cards)
        for item in issues
        if isinstance(
            cards := item.get("assessment", {}).get("card_evaluations", []),
            list,
        )
    )
    baseline_noise_cards = sum(
        card.get("evaluation") == "noise"
        for item in baseline
        if item.get("runtime_evidence_complete") is True
        for card in item.get("assessment", {}).get("card_evaluations", [])
        if isinstance(card, dict)
    )
    primaries = [
        primary
        for item in issues
        if (primary := select_shadow_primary(item)) is not None
    ]
    detected_issues = len(primaries)
    correct_diagnosis_primaries = sum(
        primary["diagnosis_correct"] is True for primary in primaries
    )
    counts = {
        "expected_issues": expected_issues,
        "detected_issues": detected_issues,
        "correct_diagnosis_primaries": correct_diagnosis_primaries,
        "generated_issue_cards": generated_issue_cards,
        "baseline_noise_cards": baseline_noise_cards,
    }
    if incomplete:
        return {
            "formula": SHADOW_SCORE_FORMULA,
            "automation_authority": SHADOW_SCORE_AUTOMATION_AUTHORITY,
            "counts": counts,
            "components": {
                "coverage": None,
                "diagnosis_recall": None,
                "selected_card_quality": None,
                "useful_coverage": None,
                "precision": None,
            },
            "score": None,
            "gate_failures": None,
        }

    coverage = (
        100.0 * detected_issues / expected_issues
        if expected_issues
        else 0.0
    )
    diagnosis_recall = (
        100.0 * correct_diagnosis_primaries / detected_issues
        if detected_issues
        else 0.0
    )
    selected_card_quality = (
        sum(primary["quality"] for primary in primaries) / detected_issues
        if detected_issues
        else 0.0
    )
    useful_coverage = (
        sum(primary["quality"] for primary in primaries) / expected_issues
        if expected_issues
        else 0.0
    )
    precision_denominator = generated_issue_cards + baseline_noise_cards
    precision = (
        100.0 * detected_issues / precision_denominator
        if precision_denominator
        else None
    )
    score = (
        SHADOW_USEFUL_COVERAGE_WEIGHT * useful_coverage
        + SHADOW_PRECISION_WEIGHT * (precision or 0.0)
    )
    gate_failures = []
    if score < SHADOW_GATE_THRESHOLDS["score"]:
        gate_failures.append("score")
    if coverage < SHADOW_GATE_THRESHOLDS["coverage"]:
        gate_failures.append("coverage")
    if diagnosis_recall < SHADOW_GATE_THRESHOLDS["diagnosis_recall"]:
        gate_failures.append("diagnosis_recall")
    if (
        precision is None
        or precision < SHADOW_GATE_THRESHOLDS["precision"]
    ):
        gate_failures.append("precision")
    if baseline_noise_cards != SHADOW_GATE_THRESHOLDS[
        "baseline_noise_cards"
    ]:
        gate_failures.append("baseline_noise")
    return {
        "formula": SHADOW_SCORE_FORMULA,
        "automation_authority": SHADOW_SCORE_AUTOMATION_AUTHORITY,
        "counts": counts,
        "components": {
            "coverage": _display_score(coverage),
            "diagnosis_recall": _display_score(diagnosis_recall),
            "selected_card_quality": _display_score(selected_card_quality),
            "useful_coverage": _display_score(useful_coverage),
            "precision": (
                _display_score(precision) if precision is not None else None
            ),
        },
        "score": _display_score(score),
        "gate_failures": gate_failures,
    }
