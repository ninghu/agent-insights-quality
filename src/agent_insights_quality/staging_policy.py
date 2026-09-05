"""Reviewed staging aggregation policy, independent of Daily readiness and score."""

from dataclasses import asdict, dataclass
import re


@dataclass(frozen=True)
class StagingPolicy:
    version: str
    minimum_required: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.version, str)
            or re.fullmatch(r"[a-z][a-z0-9-]{0,79}", self.version) is None
            or type(self.minimum_required) is not int
            or not 1 <= self.minimum_required <= 10
        ):
            raise ValueError("Invalid reviewed staging policy")

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


STAGING_POLICY = StagingPolicy("staging-observations-v2", minimum_required=8)


@dataclass(frozen=True)
class StagingPolicyMigration:
    """Caller-supplied authorization for one reviewed source/policy transition."""

    source_revision: str
    destination_policy: StagingPolicy

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_revision, str)
            or re.fullmatch(r"[0-9a-f]{40}", self.source_revision) is None
            or not isinstance(self.destination_policy, StagingPolicy)
        ):
            raise ValueError("Invalid staging policy migration")

    def to_dict(self) -> dict:
        return asdict(self)
