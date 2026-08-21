from __future__ import annotations

import re
import subprocess
import json
from pathlib import Path
from typing import Any

from agent_insights_quality.contracts import ContractError, ROOT
from agent_insights_quality.privacy import require_privacy_safe


PUBLIC_FORBIDDEN_PATTERNS = {
    "literal corporate email address": re.compile(r"(?i)\b[A-Z0-9._%+-]+@microsoft\.com\b"),
    "Azure subscription resource ID": re.compile(
        r"(?i)/subscriptions/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}(?:/|$)"
    ),
    "private Azure DevOps endpoint": re.compile(
        r"(?i)(?<![A-Za-z0-9.-])"
        r"(?:(?:https?:)?//)?"
        r"(?:[A-Za-z0-9-]+\.)*(?:dev\.azure\.com|visualstudio\.com)"
        r"(?::[0-9]+)?"
        r"(?=$|[/?:#\s<>'\"\)\]\},;!]|[.,](?=$|[\s\)\]\},;!]))"
    ),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "bearer token": re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    "storage account key": re.compile(r"(?i)\bAccountKey=[A-Za-z0-9+/]{20,}={0,2}"),
    "assigned secret": re.compile(
        r"(?i)\b(?:client_secret|api_key|access_token|password)\s*[:=]\s*['\"][^'\"]{12,}['\"]"
    ),
    "high-entropy credential candidate": re.compile(
        r"(?<![A-Za-z0-9:])[A-Za-z0-9+/]{48,}={0,2}(?![A-Za-z0-9])"
    ),
}
_PRIVATE_RUNTIME_URL = re.compile(
    r"(?i)https?://[^\s<>'\"]*?(?:"
    r"ai\.azure\.com|portal\.azure\.com|"
    r"[a-z0-9.-]+\.(?:services\.ai\.azure\.com|openai\.azure\.com|azurewebsites\.net)|"
    r"(?:internal|private)(?:[.-][a-z0-9-]+)*\.[a-z0-9.-]+"
    r")"
)


def require_public_artifact_safe(value: Any, label: str) -> None:
    """Reject sensitive content and private runtime endpoints from public artifacts."""
    require_privacy_safe(value, label)
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    for pattern_label, pattern in PUBLIC_FORBIDDEN_PATTERNS.items():
        if pattern.search(text):
            raise ContractError(f"{label}: public artifact contains {pattern_label}")
    if _PRIVATE_RUNTIME_URL.search(text):
        raise ContractError(f"{label}: public artifact contains private runtime URL")


def repository_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ContractError(f"Unable to enumerate repository files: {result.stderr.strip()}")
    return [ROOT / line for line in result.stdout.splitlines() if line]


def validate_public_repository_content() -> None:
    violations = []
    scanner_path = Path(__file__).resolve()
    for path in repository_files():
        if not path.is_file() or path.resolve() == scanner_path:
            continue
        try:
            raw = path.read_bytes()
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            violations.append(f"{path.relative_to(ROOT)}: non-ASCII content")
            continue
        for label, pattern in PUBLIC_FORBIDDEN_PATTERNS.items():
            if pattern.search(text):
                violations.append(f"{path.relative_to(ROOT)}: {label}")
    if violations:
        raise ContractError(
            "Public repository safety validation failed:\n" + "\n".join(sorted(violations))
        )
