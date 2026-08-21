from __future__ import annotations

from pathlib import Path

import pytest

from copy import deepcopy

from agent_insights_quality.contracts import (
    ContractError,
    ROOT,
    load_data,
    validate_security_policy,
)
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
        "https://" + "example." + "visualstudio." + "com/Project/_workitems/edit/1",
        "/subscriptions/" + "01234567-89ab-cdef-0123-456789abcdef" + "/resourceGroups/private",
    ],
)
def test_public_safety_patterns_reject_private_identifiers(unsafe: str) -> None:
    assert any(pattern.search(unsafe) for pattern in PUBLIC_FORBIDDEN_PATTERNS.values())


@pytest.mark.parametrize(
    "unsafe",
    [
        "http://" + "dev." + "azure.com/org/project",
        "//" + "dev." + "azure.com/org/project",
        "http://" + "example." + "visualstudio." + "com/Project",
        "//" + "example." + "visualstudio." + "com/Project",
        "dev." + "azure.com/org/project",
    ],
)
def test_public_safety_rejects_ado_hosts_independent_of_scheme(unsafe: str) -> None:
    pattern = PUBLIC_FORBIDDEN_PATTERNS["private Azure DevOps endpoint"]
    assert pattern.search(unsafe)


@pytest.mark.parametrize(
    "safe",
    [
        "https://dev." + "azure.com.example.test/docs",
        "https://not" + "visualstudio." + "com/docs",
        "The phrase dev azure com is not a URL.",
        "https://example.test/path/dev." + "azure.com-guide",
    ],
)
def test_public_safety_ado_host_pattern_avoids_near_matches(safe: str) -> None:
    pattern = PUBLIC_FORBIDDEN_PATTERNS["private Azure DevOps endpoint"]
    assert pattern.search(safe) is None


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


@pytest.mark.parametrize(
    ("field", "removed"),
    [
        ("scan_roots", "src"),
        ("scan_roots", "agents"),
        ("source_extensions", ".py"),
        ("source_extensions", ".yaml"),
    ],
)
def test_security_policy_rejects_reduced_scan_coverage(field: str, removed: str) -> None:
    policy = deepcopy(load_data(ROOT / "config" / "security-policy.yaml"))
    policy[field].remove(removed)
    with pytest.raises(ContractError, match="mandatory"):
        validate_security_policy(policy)
