from __future__ import annotations

import re
import subprocess
from pathlib import PurePosixPath
from typing import Iterable

from agent_insights_quality.contracts import ContractError, ROOT, load_data


def normalize_repo_path(path: str) -> str:
    if "\\" in path:
        raise ContractError(f"Unsafe repository path: {path}")
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts or path.startswith("./"):
        raise ContractError(f"Unsafe repository path: {path}")
    return candidate.as_posix()


def path_is_allowed(path: str, allowed_patterns: Iterable[str]) -> bool:
    normalized = normalize_repo_path(path)
    for pattern in allowed_patterns:
        escaped = re.escape(pattern)
        expression = escaped.replace(r"\*\*", "\0").replace(r"\*", "[^/]*")
        expression = expression.replace(r"\?", "[^/]").replace("\0", ".*")
        if re.fullmatch(expression, normalized):
            return True
    return False


def validate_generated_paths(paths: Iterable[str]) -> None:
    policy = load_data(ROOT / "config" / "automation-policy.yaml")
    allowed = policy["allowed_paths"]
    invalid = sorted({normalize_repo_path(path) for path in paths if not path_is_allowed(path, allowed)})
    if invalid:
        raise ContractError(
            "Generated automation attempted to modify protected paths:\n"
            + "\n".join(f"- {path}" for path in invalid)
        )


def changed_paths(base_ref: str) -> list[str]:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-status",
            "--find-renames",
            "--diff-filter=ACMRTD",
            f"{base_ref}...HEAD",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ContractError(f"Unable to compare generated changes with {base_ref}: {result.stderr.strip()}")
    paths = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if not fields:
            continue
        status = fields[0]
        if status.startswith(("R", "C")) and len(fields) == 3:
            paths.extend(fields[1:])
        elif len(fields) == 2:
            paths.append(fields[1])
        else:
            raise ContractError(f"Unable to parse git diff entry: {line}")
    return paths
