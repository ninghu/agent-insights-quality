"""Pure planning from reviewed targets, Git changes and caller-owned last-test records."""

from __future__ import annotations

import copy
import json
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from .catalogs import Catalog
from .contracts import Target


@dataclass(frozen=True)
class LastTest:
    source_revision: str
    status: Literal["PASS", "FAIL", "INCOMPLETE"]
    tested_at: str

    def __post_init__(self) -> None:
        if not self.source_revision or not self.tested_at:
            raise ValueError("Last-test records require their actual source and date")
        if self.status not in {"PASS", "FAIL", "INCOMPLETE"}:
            raise ValueError("Unknown last-test status")


@dataclass(frozen=True)
class Selection:
    target: Target
    action: Literal["traffic", "reassess"]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SourceChanges:
    paths: tuple[Path, ...]
    evaluation_only: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if not set(self.evaluation_only) <= set(self.paths):
            raise ValueError("Evaluation-only paths must be part of the source comparison")


def select_daily(
    catalog: Catalog, day: date, *, test_run: bool = False,
) -> tuple[Target, ...]:
    """Rotate four issues per lane by business day, not locale-dependent week numbers.

    Weekend private plans use a separate deterministic calendar-day position. This
    selects work only; the caller must retain the actual requested execution date.
    """
    if day.weekday() >= 5 and not test_run:
        raise ValueError("Official Daily planning requires a weekday")
    elapsed = (day - date(1970, 1, 5)).days
    weeks, remainder = divmod(elapsed, 7)
    position = weeks * 5 + remainder
    if day.weekday() >= 5:
        position = elapsed
    selected = []
    for name in catalog.agents:
        lane = catalog.for_agent(name)
        baseline = next(item for item in lane if item.is_baseline)
        issues = sorted(
            (item for item in lane if not item.is_baseline),
            key=lambda item: item.unit_id.logical_version,
        )
        if len(issues) < 4:
            raise ValueError("Daily lanes require at least four distinct issues")
        start = position * 4 % len(issues)
        selected.extend([baseline, *(issues[(start + i) % len(issues)] for i in range(4))])
    return tuple(selected)


def deployment_inputs(target: Target) -> tuple[Path, ...]:
    """Directory roots include added/deleted files; these are paths, not source hashes."""
    providers = target.baseline_root.parents[2] / "src" / "agent_insights_quality" / "providers"
    generators = (providers / "artifacts.py",)
    if target.is_prompt:
        return (*generators, target.version_root / "definition.json")
    hosted = (
        *generators, providers / "hosted.py",
        target.version_root / "source", target.baseline_root / "requirements.txt",
    )
    if target.agent_type == "hosted_custom_container":
        return (
            *hosted, providers / "acr.py", providers / "container_environment.py",
            target.baseline_root / "Dockerfile",
        )
    return (*hosted, target.baseline_root / "host.yaml")


def traffic_inputs(target: Target) -> tuple[Path, ...]:
    root = target.baseline_root.parents[2]
    return (
        target.version_root / "traffic.json",
        root / "src" / "agent_insights_quality" / "traffic.py",
    )


def evaluation_inputs(target: Target) -> tuple[Path, ...]:
    root = target.baseline_root.parents[2]
    return (
        root / "catalogs" / "AGENT_CATALOG.yaml",
        root / "catalogs" / "ISSUE_CATALOG.yaml",
        target.version_root / "implementation.yaml",
        root / "schemas" / "traffic.schema.json",
        *((root / "schemas" / "prompt-traffic.schema.json",) if target.is_prompt else ()),
        root / "src" / "agent_insights_quality" / "assessment.py",
        root / "src" / "agent_insights_quality" / "staging_policy.py",
        root / "src" / "agent_insights_quality" / "assessment_partition.py",
        root / "src" / "agent_insights_quality" / "providers" / "sol.py",
        root / "src" / "agent_insights_quality" / "telemetry.py",
        root / "src" / "agent_insights_quality" / "prompts" / "staging.md",
        root / "src" / "agent_insights_quality" / "prompts" / "daily.md",
    )


def _git_revisions(root: Path, base_revision: str, revision: str) -> tuple[str, str]:
    revisions = []
    for value in (base_revision, revision):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--end-of-options", f"{value}^{{commit}}"],
            cwd=root, capture_output=True, text=True, check=True,
        )
        revisions.append(result.stdout.strip())
    return revisions[0], revisions[1]


def _git_paths(root: Path, revisions: tuple[str, str]) -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--no-renames", "--name-only", "-z",
         *revisions, "--"],
        cwd=root, capture_output=True, check=True,
    )
    return tuple(Path(value) for value in result.stdout.decode("utf-8").split("\0") if value)


def git_changed_paths(
    root: Path, base_revision: str, revision: str = "HEAD",
) -> tuple[Path, ...]:
    """Compare ordinary Git trees, including both sides of renames and deleted files."""
    return _git_paths(root, _git_revisions(root, base_revision, revision))


def _git_document(root: Path, revision: str, path: Path) -> dict | None:
    name = path.as_posix()
    exists = subprocess.run(
        ["git", "ls-tree", "--name-only", "-z", revision, "--", name],
        cwd=root, capture_output=True, check=True,
    )
    if not exists.stdout:
        return None
    result = subprocess.run(
        ["git", "show", f"{revision}:{name}"],
        cwd=root, capture_output=True, check=True,
    )
    document = json.loads(result.stdout)
    if not isinstance(document, dict):
        raise ValueError("Traffic comparison requires a JSON object")
    return document


def _traffic_execution(document: dict) -> dict:
    execution = copy.deepcopy(document)
    execution.pop("coverage", None)
    for request in execution["requests"]:
        request.pop("expected", None)
    return execution


def git_source_changes(
    root: Path, base_revision: str, revision: str = "HEAD",
) -> SourceChanges:
    """Recognize expectation-only traffic edits by comparing ordinary Git contents."""
    revisions = _git_revisions(root, base_revision, revision)
    paths = _git_paths(root, revisions)
    evaluation_only = []
    for path in paths:
        if path.parts[0] != "agents" or path.name != "traffic.json":
            continue
        before, after = (_git_document(root, commit, path) for commit in revisions)
        if (
            before is not None and after is not None
            and before.get("contract_version") == after.get("contract_version") == "2.0"
            and _traffic_execution(before) == _traffic_execution(after)
        ):
            evaluation_only.append(path)
    return SourceChanges(paths, tuple(evaluation_only))


def _changes(root: Path, paths: Iterable[str | Path]) -> tuple[Path, ...]:
    result = []
    for path in paths:
        path = Path(path)
        resolved = (root / path).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("Changed path escapes the repository")
        result.append(resolved)
    return tuple(result)


def _touches(changes: tuple[Path, ...], dependencies: tuple[Path, ...]) -> bool:
    return any(
        path == dependency or path.is_relative_to(dependency)
        for path in changes for dependency in dependencies
    )


def select_staging(
    catalog: Catalog,
    *,
    last_tests: Mapping[str, LastTest] | None = None,
    changed_paths: Iterable[str | Path] | SourceChanges = (),
    changes_by_revision: Mapping[str, Iterable[str | Path] | SourceChanges] | None = None,
    full: bool = False,
    evaluation_paths: Iterable[str | Path] = (),
) -> tuple[Selection, ...]:
    """No state or provider queries; unchanged PASS and FAIL records remain untouched.

    Supply changed_paths for a common comparison base, or changes_by_revision for
    each record's own source revision. Missing per-revision comparisons are errors,
    not evidence of unchanged work. INCOMPLETE selections let the runner resume
    matching checkpoints rather than repeat completed traffic.
    """
    last_tests = {} if last_tests is None else last_tests
    unknown = set(last_tests) - {target.key for target in catalog.targets}
    if unknown:
        raise ValueError("Last-test record names an unknown target")

    def normalized(
        changes: Iterable[str | Path] | SourceChanges,
    ) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
        if isinstance(changes, SourceChanges):
            return (
                _changes(catalog.root, changes.paths),
                _changes(catalog.root, changes.evaluation_only),
            )
        return _changes(catalog.root, changes), ()

    common, common_evaluation = normalized(changed_paths)
    revisions = (
        {key: normalized(paths) for key, paths in changes_by_revision.items()}
        if changes_by_revision is not None else None
    )
    extra_evaluation = _changes(catalog.root, evaluation_paths)
    selected = []
    for target in catalog.targets:
        record = last_tests.get(target.key)
        reasons = []
        if full:
            reasons.append("full")
        if record is None:
            reasons.append("missing")
        elif record.status == "INCOMPLETE":
            reasons.append("incomplete")
        changes = common
        evaluation_only = common_evaluation
        execution_changes = tuple(path for path in common if path not in common_evaluation)
        if record is not None and revisions is not None and not full:
            if record.source_revision not in revisions:
                raise ValueError("Missing comparison for last-test source revision")
            paths, expectation_paths = revisions[record.source_revision]
            changes += paths
            evaluation_only += expectation_paths
            execution_changes += tuple(path for path in paths if path not in expectation_paths)
        if _touches(changes, deployment_inputs(target)):
            reasons.append("deployment_changed")
        if _touches(execution_changes, traffic_inputs(target)):
            reasons.append("traffic_changed")
        evaluation_changed = _touches(
            changes, evaluation_inputs(target) + extra_evaluation,
        ) or _touches(evaluation_only, traffic_inputs(target))
        if reasons:
            selected.append(Selection(target, "traffic", tuple(reasons)))
        elif evaluation_changed:
            selected.append(Selection(target, "reassess", ("evaluation_changed",)))
    return tuple(selected)
