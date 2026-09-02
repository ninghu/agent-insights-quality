from __future__ import annotations

import hashlib
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from agent_insights_quality.automation_policy import (
    TRACE_ASSERTION_DEADLINE_SECONDS,
    TRACE_ASSERTION_POLL_SECONDS,
)
from agent_insights_quality.util import ROOT, content_hash, read_yaml

_VERIFIER_CONTRACT_VERSION = "1.0.0"
_VERIFIER_SCHEMA_PATHS = (
    "schemas/test-agent-validation-authority-result.schema.json",
    "schemas/test-agent-validation-evidence.schema.json",
    "schemas/test-agent-validation-invocation-receipt.schema.json",
    "schemas/test-agent-validation-rules.schema.json",
)
_VERIFIER_IMPLEMENTATION_PATHS = (
    "src/agent_insights_quality/live.py",
    "src/agent_insights_quality/validation_authority_results.py",
    "src/agent_insights_quality/validation_evidence.py",
    "src/agent_insights_quality/validation_invocations.py",
    "src/agent_insights_quality/validation_live.py",
    "src/agent_insights_quality/validation_rules.py",
    "src/agent_insights_quality/validation_runtime.py",
    "src/agent_insights_quality/validation_verifier.py",
)


@cache
def current_verifier_digest(root: Path = ROOT) -> str:
    repository_root = root.resolve()
    policy = read_yaml(repository_root / "config" / "test-agent-validation.yaml")
    verification = policy["verification"]
    return content_hash(
        {
            "contract_version": _VERIFIER_CONTRACT_VERSION,
            "policy": {
                "trace_hydration": policy["trace_hydration"],
                "response_bound_batch_scope": verification[
                    "response_bound_batch_scope"
                ],
                "trace_assertion_deadline_seconds": (
                    TRACE_ASSERTION_DEADLINE_SECONDS
                ),
                "trace_assertion_poll_seconds": TRACE_ASSERTION_POLL_SECONDS,
            },
            "schemas": {
                path: _normalized_file_hash(repository_root / path)
                for path in _VERIFIER_SCHEMA_PATHS
            },
            "implementation": {
                path: _normalized_file_hash(repository_root / path)
                for path in _VERIFIER_IMPLEMENTATION_PATHS
            },
        }
    )


def authority_verification_outcome(
    evidence: Mapping[str, Any],
) -> tuple[str, str | None, str | None]:
    if evidence["evidence_complete"] is True:
        return ("PASS" if evidence["pass"] is True else "FAIL"), None, None
    return "INCOMPLETE", "authority_assertion", _first_authority_evidence_error(
        evidence
    )


def _first_authority_evidence_error(
    authority_evidence: Mapping[str, Any],
) -> str:
    for scenario in authority_evidence["scenarios"]:
        for attempt in [
            *scenario["issue_attempts"],
            *scenario["v0_attempts"],
        ]:
            if attempt["complete"] is not True:
                return str(attempt["error_code"] or "incomplete_assertion_evidence")
    return "incomplete_assertion_evidence"


def _normalized_file_hash(path: Path) -> str:
    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return "sha256:" + hashlib.sha256(content).hexdigest()
