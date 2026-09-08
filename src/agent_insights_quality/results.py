"""One pure result model for scoring, coverage, and downstream renderers.

The upstream adapter judges diagnosis/category/independent current evidence and
reconciles card revisions. It also approves public aliases and summaries BEFORE
constructing these inputs. Shape checks below do not establish privacy: never
pass provider identifiers, raw evidence, or arbitrary model text as approved data.
No catalog, provider, trace-count, or per-card run-ID contract is assumed here.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
import re

from .scoring import SCORING_POLICY, ScoreCounts, ScoringPolicy, score_percentage


ISSUE_CATEGORIES = (
    "context_memory", "cost_tokens", "hallucinations", "latency",
    "output_quality", "reliability_errors", "safety_guardrails", "tool_call_failures",
)
CATEGORY_POLICY = "catalog-test-category-v1"


def _require_type(value: object, expected: type, name: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{name} must be {expected.__name__}")


def _require_tuple(value: tuple[object, ...], item_type: type, name: str) -> None:
    _require_type(value, tuple, name)
    for item in value:
        _require_type(item, item_type, f"{name} item")


def _validate_alias(value: str, name: str) -> None:
    _require_type(value, str, name)
    if len(value) > 64 or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value):
        raise ValueError(f"{name} must be a short lowercase hyphenated name")


def _validate_summary(value: str | None) -> None:
    if value is None:
        return
    _require_type(value, str, "summary")
    if not 1 <= len(value) <= 280 or value != value.strip() or not value.isprintable():
        raise ValueError("Approved summaries must be 1-280 printable, trimmed characters")


@dataclass(frozen=True)
class UnitId:
    """Canonical catalog identity, not a deployment/provider version identifier."""

    agent: str
    logical_version: str

    def __post_init__(self) -> None:
        _validate_alias(self.agent, "agent")
        _validate_alias(self.logical_version, "logical_version")

    def to_dict(self) -> dict[str, str]:
        return {"agent": self.agent, "logical_version": self.logical_version}


class CoreVerdict(str, Enum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNKNOWN = "unknown"


class Contribution(str, Enum):
    CURRENT = "current"
    HISTORICAL = "historical"


class DiagnosticVerdict(str, Enum):
    AGREES = "agrees"
    DISAGREES = "disagrees"
    UNKNOWN = "unknown"


class ExclusionReason(str, Enum):
    MISSING_RESULT = "missing_result"
    INCOMPLETE_EXECUTION = "incomplete_execution"
    INCOMPLETE_EVIDENCE = "incomplete_evidence"
    INCOMPLETE_ASSESSMENT = "incomplete_assessment"
    UNKNOWN_CORE = "unknown_core"


class FindingClassification(str, Enum):
    EXPECTED_DETECTION = "expected_detection"
    UNEXPECTED_REAL = "unexpected_real"
    NOISE = "noise"
    DUPLICATE = "duplicate"
    UNKNOWN = "unknown"
    HISTORICAL = "historical"


class DeliveryStatus(str, Enum):
    FULL = "Full"
    PARTIAL = "Partial"
    FAILED = "Failed"


class FailureReason(str, Enum):
    SYSTEMIC_FAILURE = "systemic_failure"
    INTEGRITY_FAILURE = "integrity_failure"
    TOO_MANY_EXCLUSIONS = "too_many_exclusions"
    NO_SCORABLE_ISSUES = "no_scorable_issues"


@dataclass(frozen=True)
class CoveragePolicy:
    version: str = field(default="whole-unit-max-two-exclusions-v1", init=False)
    max_excluded_units: int = field(default=2, init=False)
    minimum_scored_issues: int = field(default=1, init=False)


COVERAGE_POLICY = CoveragePolicy()


@dataclass(frozen=True)
class PlannedUnit:
    unit_id: UnitId
    expected_issue_alias: str | None = None
    category: str | None = None

    def __post_init__(self) -> None:
        _require_type(self.unit_id, UnitId, "unit_id")
        if self.expected_issue_alias is not None:
            _validate_alias(self.expected_issue_alias, "expected_issue_alias")
        if (self.expected_issue_alias is None) != (self.unit_id.logical_version == "v0"):
            raise ValueError("Only the v0 baseline may omit an expected issue alias")
        if self.category is not None:
            _require_type(self.category, str, "category")
            if not self.is_issue or self.category not in ISSUE_CATEGORIES:
                raise ValueError("Only issues may have a reviewed test category")

    @property
    def is_issue(self) -> bool:
        return self.expected_issue_alias is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "unit_id": self.unit_id.to_dict(),
            "expected_issue_alias": self.expected_issue_alias,
            **({"category": self.category} if self.category is not None else {}),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "PlannedUnit":
        if not isinstance(value, Mapping) or set(value) - {"category"} != {
            "unit_id", "expected_issue_alias",
        }:
            raise ValueError("Invalid serialized planned unit")
        planned = cls(
            UnitId(**value["unit_id"]), value["expected_issue_alias"], value.get("category"),
        )
        if planned.to_dict() != value:
            raise ValueError("Invalid serialized planned unit")
        return planned


@dataclass(frozen=True)
class CardVerdict:
    """One canonical card judgment in this unit's assessment scope.

    Correct cards require a root alias; matching the planned issue alias denotes
    the expected defect. Other correct roots are real unexpected findings.
    Historical cards are retained but never scored, even if their core is unknown.
    The adapter must reconcile conflicting same-ID revisions before aggregation.
    Summaries must already be approved public text; validation checks shape only.
    """

    card_alias: str
    core: CoreVerdict
    root_cause_alias: str | None = None
    contribution: Contribution = Contribution.CURRENT
    severity: DiagnosticVerdict = DiagnosticVerdict.UNKNOWN
    proposed_fix: DiagnosticVerdict = DiagnosticVerdict.UNKNOWN
    summary: str | None = None

    def __post_init__(self) -> None:
        _validate_alias(self.card_alias, "card_alias")
        _require_type(self.core, CoreVerdict, "core")
        _require_type(self.contribution, Contribution, "contribution")
        _require_type(self.severity, DiagnosticVerdict, "severity")
        _require_type(self.proposed_fix, DiagnosticVerdict, "proposed_fix")
        if self.root_cause_alias is not None:
            _validate_alias(self.root_cause_alias, "root_cause_alias")
        if self.core is CoreVerdict.CORRECT and self.root_cause_alias is None:
            raise ValueError("A correct card needs an independently established root alias")
        _validate_summary(self.summary)


@dataclass(frozen=True)
class UnitResult:
    """An assessed unit; explicit gaps exclude all its findings from scoring.

    An empty card tuple without gaps is a measured absence of findings. It must
    not be used as a success-shaped replacement for an incomplete assessment.
    Summaries must already be approved public text; validation checks shape only.
    """

    unit_id: UnitId
    cards: tuple[CardVerdict, ...] = ()
    exclusion_reasons: tuple[ExclusionReason, ...] = ()
    summary: str | None = None

    def __post_init__(self) -> None:
        _require_type(self.unit_id, UnitId, "unit_id")
        _require_tuple(self.cards, CardVerdict, "cards")
        _require_tuple(self.exclusion_reasons, ExclusionReason, "exclusion_reasons")
        if len(set(self.exclusion_reasons)) != len(self.exclusion_reasons):
            raise ValueError("Exclusion reasons must be distinct")
        _validate_summary(self.summary)


@dataclass(frozen=True)
class FindingResult:
    card: CardVerdict
    classification: FindingClassification
    scored: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "card_alias": self.card.card_alias,
            "core": self.card.core.value,
            "root_cause_alias": self.card.root_cause_alias,
            "contribution": self.card.contribution.value,
            "severity": self.card.severity.value,
            "proposed_fix": self.card.proposed_fix.value,
            "summary": self.card.summary,
            "classification": self.classification.value,
            "scored": self.scored,
        }


@dataclass(frozen=True)
class ScoredUnit:
    planned: PlannedUnit
    counts: ScoreCounts
    scorable: bool
    exclusion_reasons: tuple[ExclusionReason, ...]
    findings: tuple[FindingResult, ...]
    summary: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "unit_id": self.planned.unit_id.to_dict(),
            "kind": "issue" if self.planned.is_issue else "baseline",
            "expected_issue_alias": self.planned.expected_issue_alias,
            "counts": self.counts.to_dict(),
            "scorable": self.scorable,
            "exclusion_reasons": [reason.value for reason in self.exclusion_reasons],
            "findings": [finding.to_dict() for finding in self.findings],
            "summary": self.summary,
            **({"category": self.planned.category} if self.planned.category is not None else {}),
        }


@dataclass(frozen=True)
class Coverage:
    planned_issues: int
    scored_issues: int
    planned_baselines: int
    scored_baselines: int

    @property
    def excluded_units(self) -> int:
        return (
            self.planned_issues + self.planned_baselines
            - self.scored_issues - self.scored_baselines
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "planned_issues": self.planned_issues,
            "scored_issues": self.scored_issues,
            "planned_baselines": self.planned_baselines,
            "scored_baselines": self.scored_baselines,
            "excluded_units": self.excluded_units,
        }


@dataclass(frozen=True)
class CategoryResult:
    category: str
    score: float | None
    counts: ScoreCounts
    coverage: Coverage

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category, "score": self.score,
            "counts": self.counts.to_dict(), "coverage": self.coverage.to_dict(),
        }


@dataclass(frozen=True)
class BaselineResult:
    counts: ScoreCounts
    coverage: Coverage

    def to_dict(self) -> dict[str, object]:
        return {"counts": self.counts.to_dict(), "coverage": self.coverage.to_dict()}


@dataclass(frozen=True)
class CategoryBreakdown:
    """Test-unit slices, not card-label scores or shares of the global score."""

    categories: tuple[CategoryResult, ...]
    baseline: BaselineResult
    version: str = field(default=CATEGORY_POLICY, init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "categories": [category.to_dict() for category in self.categories],
            "baseline": self.baseline.to_dict(),
        }


@dataclass(frozen=True)
class QualityResult:
    """Aggregate once, then render to HTML/Markdown/ADX without rescoring."""

    units: tuple[ScoredUnit, ...]
    counts: ScoreCounts
    coverage: Coverage
    score: float | None
    status: DeliveryStatus
    team_report_eligible: bool
    failure_reasons: tuple[FailureReason, ...]
    scoring_policy: ScoringPolicy
    category_breakdown: CategoryBreakdown | None = None
    coverage_policy: CoveragePolicy = field(default=COVERAGE_POLICY, init=False)

    def __post_init__(self) -> None:
        _require_type(self.scoring_policy, ScoringPolicy, "scoring_policy")
        if self.category_breakdown is not None:
            _require_type(self.category_breakdown, CategoryBreakdown, "category_breakdown")

    @property
    def excluded_units(self) -> tuple[ScoredUnit, ...]:
        return tuple(unit for unit in self.units if not unit.scorable)

    def to_dict(self) -> dict[str, object]:
        """Explicit public field projection, not an arbitrary dataclass/model dump.

        Alias/summary approval is a precondition, not established by this method.
        """
        return {
            "scoring_policy": self.scoring_policy.to_dict(),
            "coverage_policy": {
                "version": self.coverage_policy.version,
                "max_excluded_units": self.coverage_policy.max_excluded_units,
                "minimum_scored_issues": self.coverage_policy.minimum_scored_issues,
            },
            "score": self.score,
            "status": self.status.value,
            "team_report_eligible": self.team_report_eligible,
            "failure_reasons": [reason.value for reason in self.failure_reasons],
            "counts": self.counts.to_dict(),
            "coverage": self.coverage.to_dict(),
            "units": [unit.to_dict() for unit in self.units],
            **({"category_breakdown": self.category_breakdown.to_dict()}
               if self.category_breakdown is not None else {}),
        }


def _counts(units: tuple[ScoredUnit, ...]) -> ScoreCounts:
    return ScoreCounts(
        correct_issues=sum(unit.counts.correct_issues for unit in units),
        expected_issues=sum(unit.counts.expected_issues for unit in units),
        noise_cards=sum(unit.counts.noise_cards for unit in units),
        duplicate_cards=sum(unit.counts.duplicate_cards for unit in units),
    )


def _coverage(units: tuple[ScoredUnit, ...]) -> Coverage:
    return Coverage(
        planned_issues=sum(unit.planned.is_issue for unit in units),
        scored_issues=sum(unit.scorable and unit.planned.is_issue for unit in units),
        planned_baselines=sum(not unit.planned.is_issue for unit in units),
        scored_baselines=sum(unit.scorable and not unit.planned.is_issue for unit in units),
    )


def _category_breakdown(
    units: tuple[ScoredUnit, ...], policy: ScoringPolicy, *, eligible: bool,
) -> CategoryBreakdown | None:
    if not any(unit.planned.category is not None for unit in units):
        return None
    categories = []
    for category in ISSUE_CATEGORIES:
        members = tuple(unit for unit in units if unit.planned.category == category)
        counts = _counts(members)
        categories.append(CategoryResult(
            category, score_percentage(counts, policy) if eligible else None,
            counts, _coverage(members),
        ))
    baselines = tuple(unit for unit in units if not unit.planned.is_issue)
    return CategoryBreakdown(
        tuple(categories), BaselineResult(_counts(baselines), _coverage(baselines)),
    )


def _score_unit(planned: PlannedUnit, actual: UnitResult | None) -> ScoredUnit:
    if actual is None:
        return ScoredUnit(
            planned, ScoreCounts(), False, (ExclusionReason.MISSING_RESULT,), (), None
        )

    # Collapse copies, not distinct cards. Conflicts have no trustworthy ordering.
    by_alias: dict[str, CardVerdict] = {}
    for card in actual.cards:
        previous = by_alias.get(card.card_alias)
        if previous is not None and previous != card:
            raise ValueError("Conflicting same-ID card verdicts require upstream reconciliation")
        by_alias[card.card_alias] = card
    cards = sorted(by_alias.values(), key=lambda card: card.card_alias)
    reasons = set(actual.exclusion_reasons)
    if any(
        card.contribution is Contribution.CURRENT and card.core is CoreVerdict.UNKNOWN
        for card in cards
    ):
        reasons.add(ExclusionReason.UNKNOWN_CORE)
    scorable = not reasons

    seen_roots: set[str] = set()
    findings: list[FindingResult] = []
    for card in cards:
        if card.contribution is Contribution.HISTORICAL:
            classification = FindingClassification.HISTORICAL
        elif card.core is CoreVerdict.INCORRECT:
            classification = FindingClassification.NOISE
        elif card.core is CoreVerdict.UNKNOWN:
            classification = FindingClassification.UNKNOWN
        else:
            assert card.root_cause_alias is not None  # Validated by CardVerdict.
            if card.root_cause_alias in seen_roots:
                classification = FindingClassification.DUPLICATE
            else:
                classification = (
                    FindingClassification.EXPECTED_DETECTION
                    if card.root_cause_alias == planned.expected_issue_alias
                    else FindingClassification.UNEXPECTED_REAL
                )
            seen_roots.add(card.root_cause_alias)
        findings.append(FindingResult(
            card, classification, scorable and card.contribution is Contribution.CURRENT
        ))
    classified = [
        finding.classification for finding in findings if finding.scored
    ]
    counts = ScoreCounts(
        correct_issues=classified.count(FindingClassification.EXPECTED_DETECTION),
        expected_issues=int(scorable and planned.is_issue),
        noise_cards=classified.count(FindingClassification.NOISE),
        duplicate_cards=classified.count(FindingClassification.DUPLICATE),
    )
    return ScoredUnit(
        planned, counts, scorable, tuple(sorted(reasons, key=lambda reason: reason.value)),
        tuple(findings), actual.summary,
    )


def aggregate_results(
    planned_units: Iterable[PlannedUnit],
    unit_results: Iterable[UnitResult],
    *,
    scoring_policy: ScoringPolicy = SCORING_POLICY,
    systemic_failure: bool = False,
    integrity_failure: bool = False,
) -> QualityResult:
    """Validate an explicit plan and derive score/coverage/delivery exactly once.

    Missing results become exclusions, never silent omissions. Duplicate planned
    identities, duplicate actual units, unplanned results, and unreconciled card
    revisions are invalid inputs, not measured quality failures.
    """
    _require_type(scoring_policy, ScoringPolicy, "scoring_policy")
    if type(systemic_failure) is not bool or type(integrity_failure) is not bool:
        raise TypeError("Failure flags must be booleans")
    plan = tuple(planned_units)
    if not plan:
        raise ValueError("At least one planned unit is required")
    planned_ids: set[UnitId] = set()
    expected_issues: set[str] = set()
    for unit in plan:
        _require_type(unit, PlannedUnit, "planned unit")
        if unit.unit_id in planned_ids:
            raise ValueError("Planned unit identities must be unique")
        planned_ids.add(unit.unit_id)
        if unit.expected_issue_alias is not None:
            if unit.expected_issue_alias in expected_issues:
                raise ValueError("Planned expected issue aliases must be unique")
            expected_issues.add(unit.expected_issue_alias)
    if any(unit.category is not None for unit in plan) and any(
        unit.is_issue and unit.category is None for unit in plan
    ):
        raise ValueError("A categorized plan requires a category for every issue")
    actual_by_id: dict[UnitId, UnitResult] = {}
    for actual in unit_results:
        _require_type(actual, UnitResult, "unit result")
        if actual.unit_id not in planned_ids:
            raise ValueError("Actual unit was not planned")
        if actual.unit_id in actual_by_id:
            raise ValueError("Actual unit identities must be unique")
        actual_by_id[actual.unit_id] = actual
    units = tuple(_score_unit(unit, actual_by_id.get(unit.unit_id)) for unit in plan)
    counts = _counts(units)
    coverage = _coverage(units)
    failures = []
    if systemic_failure:
        failures.append(FailureReason.SYSTEMIC_FAILURE)
    if integrity_failure:
        failures.append(FailureReason.INTEGRITY_FAILURE)
    if coverage.excluded_units > COVERAGE_POLICY.max_excluded_units:
        failures.append(FailureReason.TOO_MANY_EXCLUSIONS)
    if coverage.scored_issues < COVERAGE_POLICY.minimum_scored_issues:
        failures.append(FailureReason.NO_SCORABLE_ISSUES)
    status = (
        DeliveryStatus.FAILED if failures
        else DeliveryStatus.PARTIAL if coverage.excluded_units else DeliveryStatus.FULL
    )
    return QualityResult(
        units, counts, coverage, None if failures else score_percentage(counts, scoring_policy),
        status, not failures, tuple(failures), scoring_policy,
        _category_breakdown(units, scoring_policy, eligible=not failures),
    )


def rescore_result(
    result: QualityResult, scoring_policy: ScoringPolicy = SCORING_POLICY,
) -> QualityResult:
    """Create a policy comparison without reclassifying or mutating source results."""
    _require_type(result, QualityResult, "result")
    _require_type(scoring_policy, ScoringPolicy, "scoring_policy")
    eligible = (
        result.team_report_eligible
        and result.status is not DeliveryStatus.FAILED
        and not result.failure_reasons
    )
    return replace(
        result,
        score=score_percentage(result.counts, scoring_policy) if eligible else None,
        scoring_policy=scoring_policy,
        category_breakdown=_category_breakdown(result.units, scoring_policy, eligible=eligible),
    )
