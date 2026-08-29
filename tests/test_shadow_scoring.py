from __future__ import annotations

from agent_insights_quality.shadow_scoring import (
    calculate_shadow_quality_score,
    select_shadow_primary,
)
from agent_insights_quality.util import ROOT, read_json

_NATIVE_FIELDS = (
    "title",
    "description",
    "category",
    "severity",
    "proposed_fix",
    "linked_traces",
)


def _reference(index: int) -> str:
    return f"sha256:{index:064x}"


def _card(
    index: int,
    *,
    finding_type: str = "MATCHED",
    diagnosis_correct: bool = True,
    failed_fields: tuple[str, ...] = (),
) -> dict:
    fields = {
        "root_cause": diagnosis_correct,
        **{
            field: field not in failed_fields
            for field in _NATIVE_FIELDS
        },
    }
    return {
        "reference": _reference(index),
        "finding_type": finding_type,
        "fields": fields,
    }


def _issue(index: int, cards: list[dict]) -> dict:
    return {
        "issue_id": f"issue-{index:03d}",
        "runtime_evidence_complete": True,
        "assessment": {"card_evaluations": cards},
    }


def _baseline(*evaluations: str) -> dict:
    return {
        "runtime_evidence_complete": True,
        "assessment": {
            "card_evaluations": [
                {"evaluation": evaluation} for evaluation in evaluations
            ]
        },
    }


def test_all_missing_has_zero_quality_and_na_precision() -> None:
    result = calculate_shadow_quality_score(
        [],
        [_issue(1, []), _issue(2, [])],
        incomplete=False,
    )

    assert result["counts"] == {
        "expected_issues": 2,
        "detected_issues": 0,
        "correct_diagnosis_primaries": 0,
        "generated_issue_cards": 0,
        "baseline_noise_cards": 0,
    }
    assert result["components"] == {
        "coverage": 0.0,
        "diagnosis_recall": 0.0,
        "selected_card_quality": 0.0,
        "useful_coverage": 0.0,
        "precision": None,
    }
    assert result["score"] == 0.0
    assert result["gate_failures"] == [
        "score",
        "coverage",
        "diagnosis_recall",
        "precision",
    ]


def test_one_miss_at_coverage_gate_keeps_full_detected_quality() -> None:
    issues = [
        _issue(index, [_card(index)])
        for index in range(1, 20)
    ]
    issues.append(_issue(20, []))

    result = calculate_shadow_quality_score([], issues, incomplete=False)

    assert result["components"] == {
        "coverage": 95.0,
        "diagnosis_recall": 100.0,
        "selected_card_quality": 100.0,
        "useful_coverage": 95.0,
        "precision": 100.0,
    }
    assert result["score"] == 96.0
    assert result["gate_failures"] == []


def test_wrong_diagnosis_gates_selected_card_quality_to_zero() -> None:
    issue = _issue(
        1,
        [_card(1, finding_type="PARTIAL", diagnosis_correct=False)],
    )

    primary = select_shadow_primary(issue)
    result = calculate_shadow_quality_score([], [issue], incomplete=False)

    assert primary == {
        "reference": _reference(1),
        "finding_type": "PARTIAL",
        "diagnosis_correct": False,
        "quality": 0.0,
    }
    assert result["components"] == {
        "coverage": 100.0,
        "diagnosis_recall": 0.0,
        "selected_card_quality": 0.0,
        "useful_coverage": 0.0,
        "precision": 100.0,
    }
    assert result["score"] == 20.0
    assert result["gate_failures"] == ["score", "diagnosis_recall"]


def test_card_spray_reduces_precision_and_uses_unrounded_gate_value() -> None:
    issues = [
        _issue(index, [_card(index)])
        for index in range(1, 7)
    ]
    issues.append(
        _issue(
            7,
            [
                _card(7, finding_type="PARTIAL", failed_fields=("severity",)),
                *[
                    _card(100 + index, finding_type="DUPLICATE")
                    for index in range(5)
                ],
            ],
        )
    )

    result = calculate_shadow_quality_score([], issues, incomplete=False)

    assert result["counts"]["generated_issue_cards"] == 12
    assert result["components"]["precision"] == 58.3
    assert result["score"] == 90.0
    assert result["gate_failures"] == ["score", "precision"]


def test_baseline_noise_penalizes_precision_but_agent_finding_is_neutral() -> None:
    result = calculate_shadow_quality_score(
        [_baseline("noise", "valid_agent_finding")],
        [_issue(1, [_card(1)])],
        incomplete=False,
    )

    assert result["counts"]["baseline_noise_cards"] == 1
    assert result["components"]["precision"] == 50.0
    assert result["score"] == 90.0
    assert result["gate_failures"] == ["precision", "baseline_noise"]


def test_mismatched_primary_is_capped_at_forty() -> None:
    issue = _issue(1, [_card(1, finding_type="MISMATCHED")])

    primary = select_shadow_primary(issue)
    result = calculate_shadow_quality_score([], [issue], incomplete=False)

    assert primary is not None
    assert primary["quality"] == 40.0
    assert result["components"]["selected_card_quality"] == 40.0
    assert result["score"] == 52.0


def test_primary_tie_break_uses_ascending_stable_reference() -> None:
    issue = _issue(
        1,
        [
            _card(2, finding_type="PARTIAL", failed_fields=("title",)),
            _card(0, finding_type="NOISE"),
            _card(1, finding_type="PARTIAL", failed_fields=("title",)),
            _card(3, finding_type="INCOMPLETE"),
        ],
    )

    primary = select_shadow_primary(issue)

    assert primary is not None
    assert primary["reference"] == _reference(1)


def test_incomplete_shadow_metrics_and_primaries_are_untrusted() -> None:
    issue = _issue(1, [_card(1)])
    result = calculate_shadow_quality_score(
        [_baseline("noise")],
        [issue],
        incomplete=True,
    )

    assert result["counts"] == {
        "expected_issues": 1,
        "detected_issues": 1,
        "correct_diagnosis_primaries": 1,
        "generated_issue_cards": 1,
        "baseline_noise_cards": 1,
    }
    assert all(value is None for value in result["components"].values())
    assert result["score"] is None
    assert result["gate_failures"] is None


def test_compatible_daily_reports_have_reproducible_shadow_backtests() -> None:
    expectations = {
        "2026/08/26": {
            "counts": {
                "expected_issues": 25,
                "detected_issues": 17,
                "correct_diagnosis_primaries": 11,
                "generated_issue_cards": 29,
                "baseline_noise_cards": 7,
            },
            "components": {
                "coverage": 68.0,
                "diagnosis_recall": 64.7,
                "selected_card_quality": 55.6,
                "useful_coverage": 37.8,
                "precision": 47.2,
            },
            "score": 39.7,
        },
        "2026/08/28": {
            "counts": {
                "expected_issues": 25,
                "detected_issues": 14,
                "correct_diagnosis_primaries": 7,
                "generated_issue_cards": 23,
                "baseline_noise_cards": 1,
            },
            "components": {
                "coverage": 56.0,
                "diagnosis_recall": 50.0,
                "selected_card_quality": 47.5,
                "useful_coverage": 26.6,
                "precision": 58.3,
            },
            "score": 32.9,
        },
    }
    for relative, expected in expectations.items():
        report = read_json(ROOT / "reports" / "daily" / relative / "report.json")

        result = calculate_shadow_quality_score(
            report["baseline"],
            report["issues"],
            incomplete=report["summary"]["incomplete"],
        )

        assert {
            "counts": result["counts"],
            "components": result["components"],
            "score": result["score"],
        } == expected
