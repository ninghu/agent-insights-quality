from __future__ import annotations

import ast
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from agent_insights_quality.util import ROOT, ContractError, content_hash, read_yaml
from agent_insights_quality.validation_manifest import _validation_contract_file_hash

_VERIFIER_CONTRACT_VERSION = "1.0.0"
_VERIFIER_SCHEMA_PATHS = (
    "schemas/test-agent-validation-authority-result.schema.json",
    "schemas/test-agent-validation-evidence.schema.json",
    "schemas/test-agent-validation-rules.schema.json",
)
_VERIFIER_IMPLEMENTATION_SYMBOLS = {
    "src/agent_insights_quality/automation_policy.py": (
        "TRACE_ASSERTION_DEADLINE_SECONDS",
        "TRACE_ASSERTION_POLL_SECONDS",
    ),
    "src/agent_insights_quality/live.py": (
        "_TRACE_ID",
        "_RESPONSE_REFERENCE",
        "_PERMANENT_LOGS_QUERY_ERROR_CODES",
        "_TELEMETRY_IDENTITY_STABILIZATION_SECONDS",
        "TelemetryCorrelationError",
        "TelemetryQueryError",
        "LiveRuntime._logs_client",
        "LiveRuntime._query_resource",
        "LiveRuntime._query_logs_result",
        "LiveRuntime.wait_for_telemetry",
        "LiveRuntime.telemetry_identity_passes",
        "LiveRuntime.trace_assertion_evidence_for_requests",
        "LiveRuntime._trace_rows",
        "_telemetry_boolean",
        "_logs_query_status_class",
        "_permanent_logs_query_failure",
        "_is_invoke_agent_span",
        "_canonical_output_messages_state_from_rows",
        "_canonical_output_messages_state_from_correlated_rows",
        "_semantic_assertion_results_from_correlated_rows",
        "_telemetry_output_response",
        "_canonical_output_messages_expectation_passes",
        "_trace_contract_ready",
        "_trace_behavior_summary",
        "_correlated_request_rows",
        "_valid_span_graph",
        "_rows_for_response_anchor",
        "_request_correlation_impossible",
        "_trace_rows_signature",
        "_trace_assertion_stability_signature",
        "_json_trace_value",
        "_kql_string_literal",
        "_nested_value",
        "_result_class",
        "_tool_rows",
        "_terminal_text",
        "_request_text",
        "_scope_values",
        "_duration_seconds",
        "_span_interval",
        "_normalize_trace_assertions",
        "_trace_assertion_names",
        "_trace_assertion_result",
        "_usable_response",
        "_response_text",
        "_semantic_assertion_names",
        "_semantic_assertion_result",
        "_normalize_fixture",
        "_discovered_operation_ids",
        "_complete_operation_ids",
        "_matched_reference_count",
        "_telemetry_references",
        "_telemetry_string_set",
        "_operation_correlation_impossible",
        "_validate_response_references",
        "_validate_operation_references",
        "TelemetryOnlyRuntime",
    ),
    "src/agent_insights_quality/validation_live.py": (
        "_POST_RESPONSE_TELEMETRY_ERRORS",
        "PostResponseTelemetryError",
        "FoundryScenarioAttemptRunner.__init__",
        "FoundryScenarioAttemptRunner.verify",
        "FoundryScenarioAttemptRunner.verify_attempts",
        "FoundryScenarioVerifier.__init__",
        "FoundryScenarioVerifier.verify",
        "FoundryScenarioVerifier.verify_attempts",
        "_attempt_observation",
    ),
    "src/agent_insights_quality/validation_runtime.py": (
        "verify_validation_shard",
        "_verify_invoked_authority",
    ),
    "src/agent_insights_quality/validation_rules.py": (
        "validation_matrix",
        "validate_validation_rules",
        "_validate_scenario",
        "_validate_step",
        "_validate_public_safe_parameters",
        "_contains_forbidden_selector",
        "_nonempty_string",
    ),
    "src/agent_insights_quality/validation_manifest.py": (
        "_validation_contract_file_hash",
    ),
    "src/agent_insights_quality/validation_verifier.py": (
        "current_verifier_digest",
        "authority_verification_outcome",
        "_first_authority_evidence_error",
        "_python_symbol_digest",
        "_ast_node_name",
    ),
}
_VERIFIER_IMPLEMENTATION_FILES = (
    "src/agent_insights_quality/validation_authority_results.py",
    "src/agent_insights_quality/validation_evidence.py",
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
            },
            "schemas": {
                path: _validation_contract_file_hash(repository_root / path)
                for path in _VERIFIER_SCHEMA_PATHS
            },
            "implementation_files": {
                path: _validation_contract_file_hash(repository_root / path)
                for path in _VERIFIER_IMPLEMENTATION_FILES
            },
            "implementation_symbols": {
                path: _python_symbol_digest(
                    repository_root / path,
                    symbols,
                )
                for path, symbols in _VERIFIER_IMPLEMENTATION_SYMBOLS.items()
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


def _python_symbol_digest(path: Path, symbols: tuple[str, ...]) -> str:
    content = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace(
        "\r", "\n"
    )
    tree = ast.parse(content, filename=str(path))
    lines = content.splitlines()
    sources: dict[str, str] = {}
    for symbol in symbols:
        node: ast.AST = tree
        for part in symbol.split("."):
            body = getattr(node, "body", ())
            matches = [item for item in body if _ast_node_name(item) == part]
            if len(matches) != 1:
                raise ContractError(
                    f"Verifier implementation symbol {symbol} is not unique in {path}"
                )
            node = matches[0]
        start = min(
            [node.lineno, *(item.lineno for item in getattr(node, "decorator_list", ()))]
        )
        if node.end_lineno is None:
            raise ContractError(f"Verifier implementation symbol {symbol} has no end")
        sources[symbol] = "\n".join(lines[start - 1 : node.end_lineno]) + "\n"
    return content_hash(sources)


def _ast_node_name(node: ast.AST) -> str | None:
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [
            target.id
            for target in targets
            if isinstance(target, ast.Name)
        ]
        return names[0] if len(names) == 1 else None
    return None
