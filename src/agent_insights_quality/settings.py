"""Offline settings; loading configuration never checks a live capability."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from collections.abc import Mapping
from datetime import date
import json
from pathlib import Path
import re
from typing import Any

from .errors import QualityError
from .results import COVERAGE_POLICY


def production_runtime_root() -> Path:
    """The CLI must use this root, not a working directory or environment override."""
    return Path.home() / ".aiq-runtime" / "agent-insights-quality"


def _integer(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


@dataclass(frozen=True)
class RuntimeSettings:
    daily_lanes: int = 5
    daily_attempt_workers: int = 4
    daily_attempt_budget: int = 10
    daily_travel_session_lookahead: int = 0
    daily_evidence_grace_seconds: int = 30
    staging_workers: int = 8
    deployment_workers: int = 4
    assessment_workers: int = 4
    query_workers: int = 4
    attempts: int = 10
    readiness_attempts: int = 6
    hydration_seconds: int = 120
    poll_interval_seconds: int = 5
    poll_timeout_seconds: int = 600
    insights_poll_timeout_seconds: int = 1200
    daily_assessment_max_payload_bytes: int = 4_000_000
    retry_limit: int = 3
    retry_backoff_seconds: int = 2
    retry_max_backoff_seconds: int = 60
    log_max_bytes: int = 2 * 1024 * 1024
    log_backup_count: int = 3

    def __post_init__(self) -> None:
        bounds = {
            "daily_lanes": (1, 5),
            "daily_attempt_workers": (1, 4),
            "daily_attempt_budget": (1, 10),
            "daily_travel_session_lookahead": (0, 1),
            "daily_evidence_grace_seconds": (0, 120),
            "staging_workers": (1, 8),
            "deployment_workers": (1, 8),
            "assessment_workers": (1, 8),
            "query_workers": (1, 8),
            "attempts": (10, 10),
            "readiness_attempts": (6, 6),
            "hydration_seconds": (0, 600),
            "poll_interval_seconds": (1, 60),
            "poll_timeout_seconds": (1, 3600),
            "insights_poll_timeout_seconds": (1, 3600),
            "daily_assessment_max_payload_bytes": (1024, 8_000_000),
            "retry_limit": (0, 8),
            "retry_backoff_seconds": (1, 60),
            "retry_max_backoff_seconds": (1, 120),
            "log_max_bytes": (1024, 64 * 1024 * 1024),
            "log_backup_count": (1, 20),
        }
        if any(
            not _integer(getattr(self, name), *limits)
            for name, limits in bounds.items()
        ):
            raise QualityError("settings_invalid")
        if (
            self.poll_interval_seconds > self.poll_timeout_seconds
            or self.poll_interval_seconds > self.insights_poll_timeout_seconds
            or self.retry_backoff_seconds > self.retry_max_backoff_seconds
        ):
            raise QualityError("settings_invalid")

    @property
    def max_unscorable_units(self) -> int:
        return COVERAGE_POLICY.max_excluded_units

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class AssessmentSettings:
    deployment_name: str = "sol-assessment"
    model: str = "gpt-5.6-sol"
    model_version: str = "2026-07-09"
    credential: str = "azure_cli"

    def __post_init__(self) -> None:
        identifiers = (self.deployment_name, self.model)
        if any(
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value)
            for value in identifiers
        ):
            raise QualityError("assessment_settings_invalid")
        if (
            not isinstance(self.model_version, str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.model_version)
            or self.credential != "azure_cli"
        ):
            raise QualityError("assessment_settings_invalid")
        try:
            date.fromisoformat(self.model_version)
        except ValueError as error:
            raise QualityError("assessment_settings_invalid") from error

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AssessmentSettings:
        if not isinstance(value, Mapping) or set(value) != {item.name for item in fields(cls)}:
            raise QualityError("assessment_settings_invalid")
        return cls(**value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate settings field")
        result[key] = value
    return result


def _load(path: Path, allowed: set[str]) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            content = stream.read(16 * 1024 + 1)
        if len(content) > 16 * 1024:
            raise ValueError("Settings too large")
        value = json.loads(content, object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError("Unknown settings fields")
        return value
    except (OSError, ValueError, UnicodeError) as error:
        raise QualityError("settings_read_failed") from error


def load_settings(path: Path | None = None) -> RuntimeSettings:
    """An explicit path opts into a small JSON override; None uses code defaults."""
    if path is None:
        return RuntimeSettings()
    return RuntimeSettings(**_load(path, {item.name for item in fields(RuntimeSettings)}))


def load_assessment_settings(path: Path | None = None, *, require_complete: bool = False) -> AssessmentSettings:
    """Read private assessment.json only when explicitly configured."""
    if path is None:
        return AssessmentSettings()
    value = _load(path, {item.name for item in fields(AssessmentSettings)})
    return AssessmentSettings.from_dict(value) if require_complete else AssessmentSettings(**value)
