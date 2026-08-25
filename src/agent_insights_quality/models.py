from __future__ import annotations

from dataclasses import dataclass, field


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


@dataclass(frozen=True)
class InsightRunEvidence:
    run_reference: str
    window_start: str
    window_end: str
    status: str
    insights: tuple[InsightEvidence, ...]


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
    trace_contract_verified: bool = False


@dataclass
class AgentResult:
    agent_name: str
    baseline: VersionResult
    issues: list[VersionResult]
