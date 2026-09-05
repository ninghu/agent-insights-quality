"""A closed public vocabulary, not a free-text sanitizer.

``allowed_units`` is the caller's reviewed catalog plan, never provider/model
data. A syntactically harmless string is not evidence of public approval.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator

from .errors import QualityError
from .results import (
    Contribution,
    CoreVerdict,
    DeliveryStatus,
    DiagnosticVerdict,
    ExclusionReason,
    FailureReason,
    FindingClassification,
    PlannedUnit,
    QualityResult,
)
from .scoring import SCORING_POLICY


class PrivacyError(QualityError):
    def __init__(self) -> None:
        super().__init__("public_projection_invalid")


SUMMARIES = {
    "assessed": "Assessed using attributable endpoint and trace evidence.",
    "incomplete": "Essential execution, evidence, or assessment is incomplete.",
    "correct": "The core diagnosis has independent current evidence.",
    "incorrect": "The core diagnosis is contradicted by current evidence.",
    "unknown": "The available evidence does not establish the core diagnosis.",
    "historical": "Historical finding retained for context and not scored.",
}
WARNING_TEXT = {
    "work_item_unavailable": "Optional work-item context is unavailable.",
    "adx_delivery_failed": "Optional ADX delivery has not completed.",
    "github_publication_failed": "Optional GitHub publication has not completed.",
    "improvement_unavailable": "Optional improvement analysis is unavailable.",
    "logging_failed": "Operational logging reported a failure.",
}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object", "properties": properties,
        "required": list(properties), "additionalProperties": False,
    }


def _enum(enum: type) -> dict[str, Any]:
    return {"enum": [item.value for item in enum]}


_COUNT = {"type": "integer", "minimum": 0, "maximum": 1_000_000}
_COUNTS = _object({
    name: _COUNT for name in (
        "correct_issues", "expected_issues", "noise_cards", "duplicate_cards",
    )
})
_SUMMARY = {"enum": [None, *SUMMARIES.values()]}
_FINDING = _object({
    "card_alias": {"type": "string", "pattern": r"^card-[0-9]{4}$"},
    "core": _enum(CoreVerdict),
    "root_cause_alias": {"type": ["string", "null"]},
    "contribution": _enum(Contribution),
    "severity": _enum(DiagnosticVerdict),
    "proposed_fix": _enum(DiagnosticVerdict),
    "summary": _SUMMARY,
    "classification": _enum(FindingClassification),
    "scored": {"type": "boolean"},
})
_UNIT = _object({
    "unit_id": _object({
        "agent": {"type": "string"}, "logical_version": {"type": "string"},
    }),
    "kind": {"enum": ["baseline", "issue"]},
    "expected_issue_alias": {"type": ["string", "null"]},
    "counts": _COUNTS,
    "scorable": {"type": "boolean"},
    "exclusion_reasons": {
        "type": "array", "items": _enum(ExclusionReason), "uniqueItems": True,
    },
    "findings": {"type": "array", "items": _FINDING, "maxItems": 9999},
    "summary": _SUMMARY,
})
PUBLIC_RESULT_SCHEMA = _object({
    "scoring_policy": {"const": {
        "version": SCORING_POLICY.version,
        "formula": SCORING_POLICY.formula,
        "noise_weight": 1,
        "duplicate_weight": 0.25,
        "rounding": SCORING_POLICY.rounding,
    }},
    "coverage_policy": {"const": {
        "version": "whole-unit-max-two-exclusions-v1",
        "max_excluded_units": 2, "minimum_scored_issues": 1,
    }},
    "score": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
    "status": _enum(DeliveryStatus),
    "team_report_eligible": {"type": "boolean"},
    "failure_reasons": {
        "type": "array", "items": _enum(FailureReason), "uniqueItems": True,
    },
    "counts": _COUNTS,
    "coverage": _object({
        name: _COUNT for name in (
            "planned_issues", "scored_issues", "planned_baselines",
            "scored_baselines", "excluded_units",
        )
    }),
    "units": {"type": "array", "items": _UNIT, "minItems": 1},
})
_VALIDATOR = Draft202012Validator(PUBLIC_RESULT_SCHEMA)


def validate_public_projection(
    value: Mapping[str, Any], *, allowed_units: Iterable[PlannedUnit],
) -> dict[str, Any]:
    """Reject unknown fields and unapproved values; return an independent copy.

    This validates the publication boundary, not a second scoring implementation.
    Counts and the delivery decision must originate in ``aggregate_results``.
    """
    import math
    import re

    plan = tuple(allowed_units)
    if (
        not plan or any(not isinstance(unit, PlannedUnit) for unit in plan)
        or len({unit.unit_id for unit in plan}) != len(plan)
        or not isinstance(value, Mapping)
        or not _VALIDATOR.is_valid(value)
    ):
        raise PrivacyError()
    score = value["score"]
    if score is not None and (
        type(score) not in (int, float) or not math.isfinite(score)
    ):
        raise PrivacyError()
    count_objects = [value["counts"], value["coverage"]]
    count_objects.extend(unit["counts"] for unit in value["units"])
    if any(type(number) is not int for counts in count_objects for number in counts.values()):
        raise PrivacyError()
    by_id = {
        (unit.unit_id.agent, unit.unit_id.logical_version): unit for unit in plan
    }
    seen = set()
    for unit in value["units"]:
        identity = (unit["unit_id"]["agent"], unit["unit_id"]["logical_version"])
        if identity not in by_id or identity in seen:
            raise PrivacyError()
        seen.add(identity)
        planned = by_id[identity]
        if (
            unit["expected_issue_alias"] != planned.expected_issue_alias
            or unit["kind"] != ("issue" if planned.is_issue else "baseline")
        ):
            raise PrivacyError()
        aliases = set()
        for finding in unit["findings"]:
            alias = finding["card_alias"]
            root = finding["root_cause_alias"]
            if (
                alias in aliases
                or root is not None and root != planned.expected_issue_alias
                and re.fullmatch(r"root-[0-9]{4}", root) is None
            ):
                raise PrivacyError()
            aliases.add(alias)
    if seen != set(by_id):
        raise PrivacyError()
    return deepcopy(dict(value))


def public_projection(
    result: QualityResult, *, allowed_units: Iterable[PlannedUnit],
) -> dict[str, Any]:
    """Project a single already-aggregated result; never echo raw card titles."""
    if not isinstance(result, QualityResult):
        raise PrivacyError()
    return validate_public_projection(result.to_dict(), allowed_units=allowed_units)


def warning_text(codes: tuple[str, ...]) -> tuple[str, ...]:
    if (
        not isinstance(codes, tuple)
        or any(not isinstance(code, str) or code not in WARNING_TEXT for code in codes)
    ):
        raise PrivacyError()
    return tuple(WARNING_TEXT[code] for code in dict.fromkeys(codes))
