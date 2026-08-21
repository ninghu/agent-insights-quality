from __future__ import annotations

from pathlib import Path

import pytest

from agent_insights_quality.contracts import ROOT, load_data
from agent_insights_quality.public_safety import PUBLIC_FORBIDDEN_PATTERNS
from agent_insights_quality.security import scan_text


def test_endpoint_query_code_is_allowed() -> None:
    patterns = load_data(ROOT / "config" / "security-policy.yaml")["forbidden_patterns"]
    safe = "result = query_application_insights_read_only(trace_id)"
    assert scan_text(Path("safe.py"), safe, patterns) == []


@pytest.mark.parametrize(
    "unsafe",
    [
        "client = LogsIngestionClient(endpoint, credential)",
        "client.upload_logs(rule_id, stream_name, logs)",
        "application_insights.inject(trace)",
        "trace_ingestion(payload)",
        'requests.post("https://example.monitor.azure.com/v1/logs", json=traces)',
        'urlopen("https://example.monitor.azure.com/v1/logs", data=payload)',
        'curl -X POST "https://example.monitor.azure.com/v1/logs"',
    ],
)
def test_direct_trace_injection_patterns_are_rejected(unsafe: str) -> None:
    patterns = load_data(ROOT / "config" / "security-policy.yaml")["forbidden_patterns"]
    assert scan_text(Path("unsafe.py"), unsafe, patterns)


@pytest.mark.parametrize(
    "unsafe",
    [
        "owner@" + "microsoft.com",
        "https://" + "example." + "visualstudio.com/Project/_workitems/edit/1",
        "/subscriptions/" + "01234567-89ab-cdef-0123-456789abcdef" + "/resourceGroups/private",
    ],
)
def test_public_safety_patterns_reject_private_identifiers(unsafe: str) -> None:
    assert any(pattern.search(unsafe) for pattern in PUBLIC_FORBIDDEN_PATTERNS.values())


@pytest.mark.parametrize(
    "unsafe",
    [
        "-----BEGIN " + "PRIVATE KEY-----",
        "ghp_" + ("a" * 36),
        "Authorization: " + "Bearer " + ("a" * 32),
        "AccountKey=" + ("A" * 40),
        "client_" + "secret='" + ("s" * 24) + "'",
    ],
)
def test_public_safety_patterns_reject_credentials(unsafe: str) -> None:
    assert any(pattern.search(unsafe) for pattern in PUBLIC_FORBIDDEN_PATTERNS.values())
