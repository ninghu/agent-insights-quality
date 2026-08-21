from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from agent_insights_quality.contracts import ContractError


_PATTERNS = {
    "email address": re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    "US social security number": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone number": re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)"),
    "IBAN": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    "routing or account number": re.compile(
        r"(?i)\b(?:routing|account|acct)\s*(?:number|no\.?|#)?\s*[:=]\s*\d{6,17}\b"
    ),
    "assigned secret": re.compile(
        r"(?i)\b(?:password|secret|token|client[_ -]?secret|api[_ -]?key|access[_ -]?token|"
        r"refresh[_ -]?token)\s*[:=]\s*['\"]?\S{8,}"
    ),
    "SAS signature": re.compile(r"(?i)(?:[?&]|\b)sig=[A-Za-z0-9%+/=_-]{16,}"),
    "bearer token": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}\b"),
    "JWT": re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    ),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "storage account key": re.compile(r"(?i)\bAccountKey=[A-Za-z0-9+/]{20,}={0,2}"),
}
_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_URL = re.compile(r"https?://[^\s<>'\"]+")
_OPAQUE_IDENTIFIER_KEYS = {
    "bundle_id",
    "trace_id",
    "trace_ids",
    "span_ids",
    "output_hash",
    "package_hash",
    "prompt_hash",
    "bundle_hash",
    "signature",
    "evidence_fingerprint",
    "artifact_reference",
    "report_reference",
    "project_reference",
    "version_digest",
    "agent_version_digest",
    "healthy_digest",
    "faulted_digest",
}


def _luhn_valid(candidate: str) -> bool:
    digits = [int(value) for value in re.sub(r"\D", "", candidate)]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def sensitive_findings(value: Any) -> list[str]:
    findings: set[str] = set()

    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if str(key) in _OPAQUE_IDENTIFIER_KEYS:
                    continue
                walk(child)
        elif isinstance(item, Iterable) and not isinstance(item, (str, bytes)):
            for child in item:
                walk(child)
        elif isinstance(item, str):
            for label, pattern in _PATTERNS.items():
                if pattern.search(item):
                    findings.add(label)
            if any(_luhn_valid(match.group(0)) for match in _CARD_CANDIDATE.finditer(item)):
                findings.add("payment card number")

    walk(value)
    return sorted(findings)


def require_privacy_safe(value: Any, label: str) -> None:
    findings = sensitive_findings(value)
    if findings:
        raise ContractError(
            f"{label} contains secret or PII content: {', '.join(findings)}"
        )


def sanitize_sensitive_text(value: str) -> str:
    result = value
    for label, pattern in _PATTERNS.items():
        replacement = (
            "[REDACTED_EMAIL]"
            if label == "email address"
            else "[REDACTED_TOKEN]"
        )
        result = pattern.sub(replacement, result)
    result = _CARD_CANDIDATE.sub(
        lambda match: (
            "[REDACTED_FINANCIAL]"
            if _luhn_valid(match.group(0))
            else match.group(0)
        ),
        result,
    )
    return _URL.sub("[REDACTED_URL]", result)
