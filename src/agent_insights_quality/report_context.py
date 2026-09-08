"""Reviewed catalog context for rendering, separate from measurement evidence.

The caller supplies a trusted repository root (the same public source authority
used for the run), never a model-selected path. Only this loader can construct a
context from catalog fields; arbitrary strings or restored JSON are not an
approval boundary. Loading does not read traffic bodies, prompts or Agent source.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
import re
from typing import Any

from .catalogs import load_catalog
from .errors import QualityError
from .results import PlannedUnit, UnitId


class ReportContextError(QualityError):
    """A code-owned error without rejected catalog text or filesystem paths."""


@dataclass(frozen=True)
class ReportMetadata:
    """Actual caller-resolved run metadata. No region or source fallback."""

    report_date: str
    region_display: str
    source_revision: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.report_date, str)
            or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", self.report_date) is None
            or not isinstance(self.region_display, str)
            or self.region_display not in {"swedencentral", "SwedenCentral", "Sweden Central"}
            or not isinstance(self.source_revision, str)
            or re.fullmatch(r"[0-9a-f]{40}", self.source_revision) is None
        ):
            raise ReportContextError("report_metadata_invalid")
        try:
            date.fromisoformat(self.report_date)
        except ValueError as error:
            raise ReportContextError("report_metadata_invalid") from error

    def to_private_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class _ReviewedUnit:
    planned: PlannedUnit
    title: str
    expected_symptom: str
    healthy_behavior: str
    traffic_path: str
    source_path: str


def _plan(allowed_units: Iterable[PlannedUnit]) -> tuple[PlannedUnit, ...]:
    plan = tuple(allowed_units)
    if (
        not plan or any(not isinstance(unit, PlannedUnit) for unit in plan)
        or len({unit.unit_id for unit in plan}) != len(plan)
    ):
        raise ReportContextError("report_context_plan_invalid")
    return plan


def _catalog_text(value: Any) -> str:
    # Shape only. Public approval comes from the caller's reviewed catalog root,
    # not these checks or any attempt to sanitize model/provider prose.
    if (
        not isinstance(value, str) or not 1 <= len(value) <= 2000
        or not value.isprintable() or value != value.strip()
    ):
        raise ReportContextError("report_catalog_field_invalid")
    return value


@dataclass(frozen=True, init=False)
class ReviewedReportContext:
    _root: Path
    _units: tuple[_ReviewedUnit, ...]
    _assignments: tuple[tuple[str, str], ...]

    def __init__(self, root: Path, *, allowed_units: Iterable[PlannedUnit]) -> None:
        plan = _plan(allowed_units)
        catalog = load_catalog(root)
        assignments = []
        agents = catalog._documents[0].get("agents")
        if (
            not isinstance(agents, list) or any(not isinstance(agent, dict) for agent in agents)
            or len(agents) != len(catalog.agents)
            or {agent.get("name") for agent in agents} != set(catalog.agents)
        ):
            raise ReportContextError("report_catalog_owner_invalid")
        for agent in agents:
            owner = _catalog_text(agent.get("owner"))
            if len(owner) > 100 or any(char in owner for char in ";,\r\n<>"):
                raise ReportContextError("report_catalog_owner_invalid")
            assignments.append((agent["name"], owner))
        by_id = {target.unit_id: target for target in catalog.targets}
        units = []
        for planned in plan:
            target = by_id.get(planned.unit_id)
            if target is None or planned.expected_issue_alias != (
                None if target.is_baseline else target.unit_id.logical_version
            ):
                raise ReportContextError("report_context_identity_mismatch")
            version = target.version_root.relative_to(catalog.root).as_posix()
            if target.is_baseline:
                title = "Healthy baseline"
                symptom = "No injected issue; compare actual behavior with the reviewed per-step expectations."
                healthy = "Honor the baseline response, conversation and instrumentation contracts."
            else:
                title = _catalog_text(target.expectation.get("title"))
                symptom = _catalog_text(target.expectation.get("root_cause"))
                healthy = _catalog_text(target.expectation.get("expected_fix"))
            units.append(_ReviewedUnit(
                planned, title, symptom, healthy, f"{version}/traffic.json",
                f"{version}/definition.json" if target.is_prompt else f"{version}/source/",
            ))
        object.__setattr__(self, "_root", catalog.root)
        object.__setattr__(self, "_units", tuple(units))
        object.__setattr__(self, "_assignments", tuple(assignments))

    @property
    def assignments(self) -> dict[str, str]:
        """Presentation assignments, not a change to the frozen measured contract."""
        return dict(self._assignments)

    def for_plan(self, allowed_units: Iterable[PlannedUnit]) -> dict[UnitId, _ReviewedUnit]:
        plan = _plan(allowed_units)
        if set(plan) != {unit.planned for unit in self._units}:
            raise ReportContextError("report_context_identity_mismatch")
        return {unit.planned.unit_id: unit for unit in self._units}

    def to_private_dict(self) -> dict[str, Any]:
        """Snapshot diagnostics only; no unchecked from-dict approval path."""
        return {
            "catalog_root": str(self._root),
            "units": [{**asdict(unit), "planned": unit.planned.to_dict()} for unit in self._units],
        }


def load_report_context(
    root: Path, *, allowed_units: Iterable[PlannedUnit],
) -> ReviewedReportContext:
    return ReviewedReportContext(root, allowed_units=allowed_units)
