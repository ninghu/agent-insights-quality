from __future__ import annotations

import re
from pathlib import Path

from agent_insights_quality.util import ContractError

_ALLOWED = (
    re.compile(r"^reports/latest\.(json|md)$"),
    re.compile(r"^reports/trend\.json$"),
    re.compile(r"^reports/insight-engine-improvement\.(json|md)$"),
    re.compile(r"^reports/daily/[0-9]{4}/[0-9]{2}/[0-9]{2}/(report\.(json|md)|email-receipt\.json)$"),
    re.compile(
        r"^reports/daily/[0-9]{4}/[0-9]{2}/[0-9]{2}/"
        r"insight-engine-improvement\.(json|md)$"
    ),
    re.compile(
        r"^reports/daily/[0-9]{4}/[0-9]{2}/[0-9]{2}/agents/"
        r"(weather-agent|healthcare-agent|finance-agent|travel-agent|support-ticket-agent)\.md$"
    ),
)


def validate_generated_paths(paths: list[str]) -> None:
    if not paths:
        raise ContractError("Generated change contains no paths")
    invalid = [
        path.replace("\\", "/")
        for path in paths
        if not any(pattern.fullmatch(path.replace("\\", "/")) for pattern in _ALLOWED)
    ]
    if invalid:
        raise ContractError("Generated change contains protected paths: " + ", ".join(invalid))


def relative_files(root: Path) -> list[str]:
    return [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    ]
