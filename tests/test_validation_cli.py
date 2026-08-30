from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_insights_quality import cli
from agent_insights_quality.util import ContractError


def test_prepare_validation_cli_writes_only_private_candidate_manifest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    private = tmp_path / ".aiq-runtime" / "agent-insights-quality"
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", str(private))
    output = private / "test-agent-validation" / "candidate.json"
    args = cli.build_parser().parse_args(
        [
            "prepare-test-agent-validation",
            "--pr-number",
            "999",
            "--candidate-head-sha",
            "a" * 40,
            "--candidate-tree-sha",
            "b" * 40,
            "--workflow-run-id",
            "synthetic-run",
            "--output",
            str(output),
        ]
    )
    result = json.loads(cli._dispatch(args) or "{}")
    assert result["authority_count"] == 41
    assert output.is_file()
    assert json.loads(output.read_text(encoding="utf-8"))["kind"] == (
        "test-agent-validation-candidate"
    )

    escaped = cli.build_parser().parse_args(
        [
            "prepare-test-agent-validation",
            "--pr-number",
            "999",
            "--candidate-head-sha",
            "a" * 40,
            "--candidate-tree-sha",
            "b" * 40,
            "--workflow-run-id",
            "synthetic-run",
            "--output",
            str(tmp_path / "public.json"),
        ]
    )
    with pytest.raises(ContractError, match="private runtime root"):
        cli._dispatch(escaped)


def test_validation_cli_exposes_contract_lifecycle_receipt_and_reconciler_commands() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(
        ["validate-test-agent-evidence", "--evidence", "evidence.json"]
    ).command == "validate-test-agent-evidence"
    assert parser.parse_args(
        ["validate-test-agent-lifecycle", "--lifecycle", "lifecycle.json"]
    ).command == "validate-test-agent-lifecycle"
    assert parser.parse_args(
        ["validate-test-agent-receipt", "--receipt", "receipt.json"]
    ).command == "validate-test-agent-receipt"
    assert parser.parse_args(
        [
            "issue-test-agent-validation-receipt",
            "--receipt",
            "receipt.json",
            "--storage-account",
            "syntheticstorage",
        ]
    ).command == "issue-test-agent-validation-receipt"
    assert parser.parse_args(
        [
            "reconcile-test-agent-validation",
            "--storage-account",
            "syntheticstorage",
            "--ownership-nonce",
            "nonce-0001",
            "--holder-workflow-reference",
            "workflow",
            "--holder-app-reference",
            "app",
            "--holder-run-reference",
            "run",
        ]
    ).command == "reconcile-test-agent-validation"
    assert parser.parse_args(
        [
            "run-test-agent-validation",
            "--candidate",
            "candidate.json",
            "--storage-account",
            "syntheticstorage",
            "--expected-azure-client-id",
            "client-id",
            "--automation-principal-id",
            "principal-id",
            "--receipt-output",
            "receipt.json",
        ]
    ).command == "run-test-agent-validation"
    assert parser.parse_args(
        [
            "verify-test-agent-validation-credential",
            "--expected-client-id",
            "client-id",
        ]
    ).command == "verify-test-agent-validation-credential"
