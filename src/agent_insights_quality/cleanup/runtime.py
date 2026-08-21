from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CleanupResult:
    projects: tuple[str, ...]
    connections: tuple[str, ...]
    monitors: tuple[str, ...]
    artifacts: tuple[str, ...]
    dry_run: bool


class ProjectCleaner(Protocol):
    def cleanup_expired(self, *, now: date | None = None, dry_run: bool = True) -> list[str]: ...


class ConnectionCleaner(Protocol):
    def cleanup_owned_connections(self, owner_reference: str, *, dry_run: bool = True) -> list[str]: ...


class MonitorCleaner(Protocol):
    def cleanup_owned_monitors(self, owner_reference: str, *, dry_run: bool = True) -> list[str]: ...


class ArtifactCleaner(Protocol):
    def cleanup_expired(self, owner_reference: str, *, dry_run: bool = True) -> list[str]: ...


def cleanup_owned_resources(
    *,
    owner_reference: str,
    projects: ProjectCleaner,
    artifacts: ArtifactCleaner,
    connections: ConnectionCleaner | None = None,
    monitors: MonitorCleaner | None = None,
    dry_run: bool = True,
) -> CleanupResult:
    return CleanupResult(
        projects=tuple(projects.cleanup_expired(dry_run=dry_run)),
        connections=tuple(
            connections.cleanup_owned_connections(owner_reference, dry_run=dry_run)
            if connections
            else ()
        ),
        monitors=tuple(
            monitors.cleanup_owned_monitors(owner_reference, dry_run=dry_run) if monitors else ()
        ),
        artifacts=tuple(artifacts.cleanup_expired(owner_reference, dry_run=dry_run)),
        dry_run=dry_run,
    )
