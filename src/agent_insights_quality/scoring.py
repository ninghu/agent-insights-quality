"""Pure numeric scoring. Counts must come from whole, scorable version units."""

from collections.abc import Mapping
from dataclasses import dataclass, field


_LEGACY_VERSION = "unique-issues-noise-1-duplicate-025-v1"
_VERSION = "unique-issues-noise-1-duplicate-05-miss-025-v2"


@dataclass(frozen=True)
class ScoringPolicy:
    """Select only reviewed versions; weights and formulas are never configurable."""

    version: str = _VERSION
    formula: str = field(init=False)
    noise_weight: int = field(default=1, init=False)
    duplicate_weight: float = field(init=False)
    miss_weight: float | None = field(init=False)
    rounding: str = field(default="half-up-one-decimal", init=False)

    def __post_init__(self) -> None:
        if type(self.version) is not str:
            raise TypeError("Scoring policy version must be a string")
        if self.version == _LEGACY_VERSION:
            formula = "100*C/(E_scored+N_scored+0.25*D_scored)"
            duplicate_weight, miss_weight = 0.25, None
        elif self.version == _VERSION:
            formula = "100*C/(C+N_scored+0.5*D_scored+0.25*(E_scored-C))"
            duplicate_weight, miss_weight = 0.5, 0.25
        else:
            raise ValueError("Unknown scoring policy version")
        object.__setattr__(self, "formula", formula)
        object.__setattr__(self, "duplicate_weight", duplicate_weight)
        object.__setattr__(self, "miss_weight", miss_weight)

    def to_dict(self) -> dict[str, str | int | float]:
        value = {
            "version": self.version,
            "formula": self.formula,
            "noise_weight": self.noise_weight,
            "duplicate_weight": self.duplicate_weight,
        }
        if self.miss_weight is not None:
            value["miss_weight"] = self.miss_weight
        value["rounding"] = self.rounding
        return value

    @classmethod
    def from_dict(cls, serialized: Mapping[str, object]) -> "ScoringPolicy":
        """Restore a complete closed policy, including the original v1 shape."""
        if not isinstance(serialized, Mapping):
            raise TypeError("Serialized scoring policy must be a mapping")
        policy = cls(version=serialized.get("version"))
        expected = policy.to_dict()
        if set(serialized) != set(expected) or any(
            type(serialized[name]) not in ((int, float) if type(item) in (int, float) else (str,))
            or serialized[name] != item
            for name, item in expected.items()
        ):
            raise ValueError("Serialized scoring policy does not match its reviewed version")
        return policy


SCORING_POLICY = ScoringPolicy()
LEGACY_SCORING_POLICY = ScoringPolicy(version=_LEGACY_VERSION)


@dataclass(frozen=True)
class ScoreCounts:
    correct_issues: int = 0
    expected_issues: int = 0
    noise_cards: int = 0
    duplicate_cards: int = 0

    def __post_init__(self) -> None:
        values = (
            self.correct_issues, self.expected_issues,
            self.noise_cards, self.duplicate_cards,
        )
        if any(type(value) is not int for value in values):
            raise TypeError("Score counts must be integers, not booleans or floats")
        if any(value < 0 for value in values):
            raise ValueError("Score counts must be nonnegative")
        if self.correct_issues > self.expected_issues:
            raise ValueError("Correct issues cannot exceed scored expected issues")

    def to_dict(self) -> dict[str, int]:
        return {
            "correct_issues": self.correct_issues,
            "expected_issues": self.expected_issues,
            "noise_cards": self.noise_cards,
            "duplicate_cards": self.duplicate_cards,
        }


def score_percentage(
    counts: ScoreCounts, policy: ScoringPolicy = SCORING_POLICY,
) -> float | None:
    """Return a measured score, or None when no expected issue is scorable.

    Integer arithmetic implements exact half-up rounding, independent of floating
    point ties or decimal context. A fully measured miss is 0.0, not None.
    """
    if not isinstance(counts, ScoreCounts):
        raise TypeError("counts must be ScoreCounts")
    if not isinstance(policy, ScoringPolicy):
        raise TypeError("policy must be ScoringPolicy")
    if counts.expected_issues == 0:
        return None
    if policy.version == _LEGACY_VERSION:
        denominator = (
            4 * counts.expected_issues + 4 * counts.noise_cards + counts.duplicate_cards
        )
    else:
        denominator = (
            4 * counts.correct_issues + 4 * counts.noise_cards + 2 * counts.duplicate_cards
            + counts.expected_issues - counts.correct_issues
        )
    tenths, remainder = divmod(4000 * counts.correct_issues, denominator)
    return (tenths + (2 * remainder >= denominator)) / 10
