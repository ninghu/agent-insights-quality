from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from agent_insights_quality.contracts import ContractError, ROOT, load_data


def scan_text(path: Path, text: str, patterns: Iterable[str]) -> list[str]:
    findings = []
    for pattern in patterns:
        if re.search(pattern, text):
            findings.append(pattern)
    return findings


def validate_no_direct_trace_injection() -> None:
    policy = load_data(ROOT / "config" / "security-policy.yaml")
    extensions = set(policy["source_extensions"])
    violations = []
    scanner_path = Path(__file__).resolve()
    for root_name in policy["scan_roots"]:
        scan_root = ROOT / root_name
        if not scan_root.exists():
            continue
        for path in scan_root.rglob("*"):
            if not path.is_file() or path.suffix not in extensions or path.resolve() == scanner_path:
                continue
            text = path.read_text(encoding="utf-8")
            findings = scan_text(path, text, policy["forbidden_patterns"])
            if findings:
                violations.append(f"{path.relative_to(ROOT)}: {', '.join(findings)}")
    if violations:
        raise ContractError(
            "Direct Application Insights writes or trace injection are forbidden:\n"
            + "\n".join(violations)
        )
