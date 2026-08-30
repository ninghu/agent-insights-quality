from __future__ import annotations

from agent_insights_quality.util import ROOT


def _workflow(name: str) -> str:
    return (
        ROOT / ".github" / "workflows" / name
    ).read_text(encoding="utf-8")


def test_candidate_workflow_is_bounded_nonauthorizing_shadow_preparation() -> None:
    text = _workflow("test-agent-validation.yml")
    folded = text.casefold()
    assert "workflow_dispatch" in text
    assert "test-agent-validation-account" in text
    assert "cancel-in-progress: false" in text
    assert "id-token: write" in text
    assert "test-agent-validation-shadow" in text
    assert "prepare-test-agent-validation" in text
    assert "generate-test-agent-validation-rules --check" in text
    assert "non-authorizing" in folded
    assert "required check" in folded
    for forbidden in (
        "run-daily",
        "run-full",
        "publish-adx",
        "agent_insight_monitor",
        "create-promotion-receipt",
    ):
        assert forbidden not in folded


def test_receipt_workflow_uses_protected_default_branch_issuer() -> None:
    text = _workflow("test-agent-validation-receipt.yml")
    assert "environment: test-agent-validation-receipt" in text
    assert "ref: main" in text
    assert "issue-test-agent-validation-receipt" in text
    assert "id-token: write" in text
    assert "Receipt path escapes" in text
    assert "accepts merge receipts only" in text


def test_reconciler_is_cleanup_only_on_fifteen_minute_cadence() -> None:
    text = _workflow("test-agent-validation-reconciler.yml")
    assert 'cron: "*/15 * * * *"' in text
    assert "cleanup-only:" in text
    assert "reconcile-test-agent-validation" in text
    assert "ref: main" in text
    for forbidden in (
        "prepare-test-agent-validation",
        "run-daily",
        "run-full",
        "issue-test-agent-validation-receipt",
    ):
        assert forbidden not in text
