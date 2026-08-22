from __future__ import annotations

from pathlib import Path

_REVIEWED_SUFFIXES = frozenset({".json", ".py", ".txt", ".yaml", ".yml"})
_REVIEWED_FILENAMES = frozenset({".dockerignore", "Dockerfile"})
_IGNORED_DIRECTORIES = frozenset(
    {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "build", "dist"}
)


def reviewed_source_files(root: Path) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not any(
            part in _IGNORED_DIRECTORIES
            for part in path.relative_to(root).parts
        )
        and (
            path.name in _REVIEWED_FILENAMES
            or (
                not path.name.startswith(".")
                and path.suffix in _REVIEWED_SUFFIXES
            )
        )
    ]


__all__ = ["reviewed_source_files"]
