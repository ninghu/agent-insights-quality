from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SemanticAssertionEvidence:
    assertion: str
    passed: bool
    evidence_sufficient: bool


@dataclass(frozen=True)
class TraceAssertionEvidence:
    assertion: str
    passed: bool
    evidence_sufficient: bool = True


@dataclass(frozen=True)
class RequestCompletionEvidence:
    request_index: int
    response_count: int
    usable_response: bool
    semantic_assertion_count: int
    semantic_assertions_passed: int
    assertion_results: tuple[SemanticAssertionEvidence, ...]
    activation_gate: bool
    direct_terminal_response_count: int
    function_call_count: int
    trace_assertion_count: int = 0
    trace_assertions_passed: int = 0
    trace_assertion_results: tuple[TraceAssertionEvidence, ...] = ()
    error_code: str | None = None


@dataclass(frozen=True)
class InvocationEvidence:
    operation_ids: tuple[str, ...]
    response_references: tuple[str, ...]
    started_at: str
    completed_at: str
    request_count: int
    allow_window_correlation: bool
    response_count: int = 0
    usable_response_count: int = 0
    semantic_assertion_count: int = 0
    semantic_assertions_passed: int = 0
    trace_assertion_count: int = 0
    trace_assertions_passed: int = 0
    request_summaries: tuple[RequestCompletionEvidence, ...] = ()


@dataclass(frozen=True)
class InsightEvidence:
    reference: str
    agent_version: str
    title: str
    description: str
    category: str
    severity: str
    proposed_fix: str
    linked_operation_ids: tuple[str, ...]
    trace_count: int
    updated_at: str


def linked_operations_match_scope(
    linked_operation_ids: Collection[str],
    operation_ids: Collection[str],
) -> bool:
    linked_values = list(linked_operation_ids)
    linked = set(linked_values)
    return (
        bool(linked)
        and len(linked_values) == len(linked)
        and linked.issubset(operation_ids)
    )


@dataclass(frozen=True)
class InsightRunEvidence:
    run_reference: str
    window_start: str
    window_end: str
    status: str
    insights: tuple[InsightEvidence, ...]


@dataclass(frozen=True)
class InsightRunCheckpoint:
    run_id: str
    before_revisions: dict[str, tuple[str, int]]


@dataclass
class VersionResult:
    logical_version: str
    foundry_version: str
    status: str
    operation_ids: list[str] = field(default_factory=list)
    insight_references: list[str] = field(default_factory=list)
    window_start: str | None = None
    window_end: str | None = None
    error_code: str | None = None
    observed_insight: InsightEvidence | None = None
    observed_insights: list[InsightEvidence] = field(default_factory=list)
    endpoint_request_count: int = 0
    endpoint_response_count: int = 0
    endpoint_usable_response_count: int = 0
    semantic_assertion_count: int = 0
    semantic_assertions_passed: int = 0
    trace_assertion_count: int = 0
    trace_assertions_passed: int = 0
    trace_contract_verified: bool = False
    trace_behavior_summary: dict[str, object] = field(default_factory=dict)
    issue_trace_gap_acceptance: dict[str, object] | None = None
    endpoint_request_summaries: list[RequestCompletionEvidence] = field(
        default_factory=list
    )


@dataclass
class AgentResult:
    agent_name: str
    baseline: VersionResult
    issues: list[VersionResult]


def request_completion_payload(
    value: RequestCompletionEvidence,
) -> dict[str, object]:
    return {
        "request_index": value.request_index,
        "response_count": value.response_count,
        "usable_response": value.usable_response,
        "semantic_assertion_count": value.semantic_assertion_count,
        "semantic_assertions_passed": value.semantic_assertions_passed,
        "assertion_results": [
            {
                "assertion": assertion.assertion,
                "passed": assertion.passed,
                "evidence_sufficient": assertion.evidence_sufficient,
            }
            for assertion in value.assertion_results
        ],
        "trace_assertion_count": value.trace_assertion_count,
        "trace_assertions_passed": value.trace_assertions_passed,
        "trace_assertion_results": [
            {
                "assertion": assertion.assertion,
                "passed": assertion.passed,
                "evidence_sufficient": assertion.evidence_sufficient,
            }
            for assertion in value.trace_assertion_results
        ],
        "activation_gate": value.activation_gate,
        "direct_terminal_response_count": value.direct_terminal_response_count,
        "function_call_count": value.function_call_count,
        "error_code": value.error_code,
    }
