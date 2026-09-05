"""Pure numeric scoring. Counts must come from whole, scorable version units."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScoringPolicy:
    version: str = field(default="unique-issues-noise-1-duplicate-025-v1", init=False)
    formula: str = field(default="100*C/(E_scored+N_scored+0.25*D_scored)", init=False)
    noise_weight: int = field(default=1, init=False)
    duplicate_weight: float = field(default=0.25, init=False)
    rounding: str = field(default="half-up-one-decimal", init=False)


SCORING_POLICY = ScoringPolicy()


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


def score_percentage(counts: ScoreCounts) -> float | None:
    """Return a measured score, or None when no expected issue is scorable.

    Integer arithmetic implements exact half-up rounding, independent of floating
    point ties or decimal context. A fully measured miss is 0.0, not None.
    """
    if not isinstance(counts, ScoreCounts):
        raise TypeError("counts must be ScoreCounts")
    if counts.expected_issues == 0:
        return None
    denominator = (
        4 * counts.expected_issues + 4 * counts.noise_cards + counts.duplicate_cards
    )
    tenths, remainder = divmod(4000 * counts.correct_issues, denominator)
    return (tenths + (2 * remainder >= denominator)) / 10
