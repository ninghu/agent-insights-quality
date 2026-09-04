from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_insights_quality.models import (
    InsightEvidence,
    InsightRunCheckpoint,
    InvocationEvidence,
    RequestCompletionEvidence,
    TraceAssertionEvidence,
    VersionResult,
)
from agent_insights_quality.runtime_state import (
    TrafficLedger,
    VersionCheckpointStore,
    profile_run_lock,
)
from agent_insights_quality.util import ContractError


def test_traffic_ledger_preserves_clean_horizon(tmp_path: Path) -> None:
    ledger = TrafficLedger("daily", tmp_path)
    started = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
    ledger.mark_started(
        "weather-agent",
        now=started,
        uncertain_seconds=600,
    )
    ledger.mark_started(
        "weather-agent",
        now=started + timedelta(seconds=10),
        uncertain_seconds=300,
    )
    assert ledger.clean_after(
        "weather-agent",
        lookback_seconds=360,
        margin_seconds=30,
    ) == started + timedelta(seconds=990)
    completed = started + timedelta(seconds=10)
    ledger.mark_completed("weather-agent", now=completed)
    assert ledger.clean_after(
        "weather-agent",
        lookback_seconds=360,
        margin_seconds=30,
    ) == completed + timedelta(seconds=390)


def test_version_checkpoint_round_trips_private_stages(tmp_path: Path) -> None:
    store = VersionCheckpointStore(
        tmp_path / "stages",
        "sha256:" + "d" * 64,
    )
    args = (
        "weather-agent",
        "issue-001",
        "18",
        "sha256:" + "a" * 64,
    )
    invocation = InvocationEvidence(
        operation_ids=(),
        response_references=("private-response-reference",),
        started_at="2026-08-27T18:00:00+00:00",
        completed_at="2026-08-27T18:00:10+00:00",
        request_count=1,
        allow_window_correlation=False,
        response_count=1,
        usable_response_count=1,
        trace_assertion_count=1,
        trace_assertions_passed=1,
        request_summaries=(
            RequestCompletionEvidence(
                request_index=0,
                response_count=1,
                usable_response=True,
                semantic_assertion_count=0,
                semantic_assertions_passed=0,
                assertion_results=(),
                activation_gate=True,
                direct_terminal_response_count=0,
                function_call_count=0,
                trace_assertion_count=1,
                trace_assertions_passed=1,
                trace_assertion_results=(
                    TraceAssertionEvidence("one_tool_call", True),
                ),
            ),
        ),
    )
    insight = InsightEvidence(
        reference="sha256:" + "b" * 64,
        agent_version="18",
        title="Synthetic finding",
        description="Synthetic description.",
        category="output_quality",
        severity="medium",
        proposed_fix="Apply the synthetic fix.",
        linked_operation_ids=("c" * 32,),
        trace_count=1,
        updated_at="2026-08-27T18:01:00+00:00",
    )
    result = VersionResult(
        logical_version="issue-001",
        foundry_version="18",
        status="observed",
        operation_ids=["c" * 32],
        insight_references=[insight.reference],
        window_start="2026-08-27T18:00:00+00:00",
        window_end="2026-08-27T18:02:00+00:00",
        observed_insight=insight,
        observed_insights=[insight],
        endpoint_request_count=1,
        endpoint_response_count=1,
        endpoint_usable_response_count=1,
        trace_assertion_count=1,
        trace_assertions_passed=1,
        trace_contract_verified=True,
        endpoint_request_summaries=list(invocation.request_summaries),
    )
    store.save_invocation(*args, invocation)
    store.save_operation_ids(*args, ("c" * 32,))
    store.save_trace_verified(*args)
    store.mark_insight_start_pending(*args)
    assert store.insight_start_pending(*args) is True
    assert store.insight_start_outcome(*args)["status"] == "pending"
    assert store.has_unresolved_insight_state() is True
    store.clear_insight_start_pending(*args)
    assert store.insight_start_pending(*args) is False
    store.record_insight_start_outcome(*args, status="explicit_no_run")
    store.mark_insight_start_pending(*args)
    checkpoint = InsightRunCheckpoint(
        "private-run-id",
        {"private-card-id": ("2026-08-27T18:01:00+00:00", 1)},
    )
    store.save_insight_run(*args, checkpoint)
    assert store.insight_start_pending(*args) is False
    assert store.insight_start_outcome(*args)["status"] == "started"
    assert store.has_unresolved_insight_state() is False
    store.save_result(*args, result)
    assert store.invocation(*args) == invocation
    assert store.operation_ids(*args) == ("c" * 32,)
    assert store.trace_verified(*args) is True
    assert store.insight_run(*args) == checkpoint
    assert store.result(*args) == result
    store.save_rejected_result(*args, result, drain_pending=True)
    assert store.insight_drain_pending(*args) is True
    assert store.has_unresolved_insight_state() is True
    store.clear_insight_drain_pending(*args)
    assert store.insight_drain_pending(*args) is False
    assert store.has_unresolved_insight_state() is False
    assert store.claim_agent_recovery("weather-agent", 2) is True
    assert store.claim_agent_recovery("weather-agent", 2) is True
    assert store.claim_agent_recovery("weather-agent", 2) is False
    resumed_store = VersionCheckpointStore(
        tmp_path / "stages",
        "sha256:" + "d" * 64,
    )
    assert resumed_store.claim_agent_recovery("weather-agent", 2) is False
    different_contract = VersionCheckpointStore(
        tmp_path / "stages",
        "sha256:" + "e" * 64,
    )
    with pytest.raises(ContractError, match="current contract"):
        different_contract.result(*args)


def test_unknown_insight_start_requires_stable_no_run_before_one_retry(
    tmp_path: Path,
) -> None:
    store = VersionCheckpointStore(
        tmp_path / "stages",
        "sha256:" + "d" * 64,
    )
    args = (
        "finance-agent",
        "issue-019",
        "19",
        "sha256:" + "a" * 64,
    )
    store.save_operation_ids(*args, ("c" * 32,))
    store.mark_insight_start_pending(*args)
    store.record_insight_start_outcome(*args, status="unknown")
    store.save_result(
        *args,
        VersionResult(
            logical_version="issue-019",
            foundry_version="19",
            status="inconclusive",
            error_code="insight_run_start_unresolved",
        ),
    )
    store.record_insight_start_outcome(*args, status="explicit_no_run")
    store.prepare_insight_start_retry(*args)
    store.mark_insight_start_pending(*args)

    assert store.insight_start_outcome(*args)["status"] == "pending"
    assert store.insight_start_outcome(*args)["retry_count"] == 1
    assert list((tmp_path / "stages" / "insight-start-history").rglob("*.json"))


def test_profile_run_lock_rejects_overlap(tmp_path: Path) -> None:
    with profile_run_lock("daily", "aiq-20260827", tmp_path):
        with pytest.raises(ContractError, match="active qualification"):
            with profile_run_lock("daily", "aiq-20260827-r01", tmp_path):
                pass
    with profile_run_lock("daily", "aiq-20260827-r01", tmp_path):
        pass


def test_recoverable_version_checkpoint_is_archived_before_fresh_attempt(
    tmp_path: Path,
) -> None:
    store = VersionCheckpointStore(
        tmp_path / "stages",
        "sha256:" + "d" * 64,
    )
    args = (
        "finance-agent",
        "v0",
        "17",
        "sha256:" + "a" * 64,
    )
    result = VersionResult(
        logical_version="v0",
        foundry_version="17",
        status="inconclusive",
        error_code="baseline_evidence_incomplete",
    )
    store.save_rejected_result(*args, result, drain_pending=False)

    preserved = store.preserve_version_attempt(*args)
    assert store.result(*args) == result

    digest = store.archive_version_for_recovery(*args)

    assert digest == preserved
    assert digest.startswith("sha256:")
    assert store.result(*args) is None
    archives = list((tmp_path / "stages" / "recovery-history").rglob("*.json"))
    assert len(archives) == 1
