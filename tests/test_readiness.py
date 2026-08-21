from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from agent_insights_quality.cli import main
import agent_insights_quality.contracts as contracts
from agent_insights_quality.contracts import ContractError, ROOT, SCHEMAS, load_data, validate_instance
from agent_insights_quality.readiness import (
    MANDATORY_RUNTIME_COMPONENTS,
    READINESS_FAILURE_PROHIBITED_ACTIONS,
    require_daily_runtime,
    validate_runtime_readiness,
)
from agent_insights_quality.reporting import (
    finalize_readiness_failure,
    record_email_delivery,
    validate_email_handoff,
    validate_stored_bundle_content,
)


def test_runtime_readiness_keeps_daily_disabled_until_every_component_is_ready() -> None:
    readiness = load_data(ROOT / "config" / "runtime-readiness.yaml")
    validate_runtime_readiness(readiness)
    assert readiness["daily_workflow_enabled"] is False
    assert set(readiness["mandatory_components"]) == MANDATORY_RUNTIME_COMPONENTS
    assert readiness["mandatory_components"]["scenario_catalog"] is True
    assert readiness["mandatory_components"]["healthy_agents"] is False
    assert readiness["mandatory_components"]["deterministic_scoring"] is True
    assert readiness["mandatory_components"]["copilot_judging"] is True
    assert readiness["mandatory_components"]["quality_memory"] is True
    assert readiness["mandatory_components"]["ado_synchronization"] is True
    assert readiness["mandatory_components"]["reporting_and_email"] is True
    assert readiness["mandatory_components"]["live_qualification"] is False
    assert sum(readiness["mandatory_components"].values()) == 6


def test_daily_runtime_fails_closed_as_inconclusive(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check-runtime-readiness"]) == 1
    output = capsys.readouterr()
    assert "INCONCLUSIVE" in output.err
    assert "Run the readiness-failure finalizer before stopping" in output.err


def test_readiness_cannot_enable_daily_workflow_early() -> None:
    readiness = deepcopy(load_data(ROOT / "config" / "runtime-readiness.yaml"))
    readiness["daily_workflow_enabled"] = True
    with pytest.raises(ContractError, match="aggregate readiness"):
        validate_runtime_readiness(readiness)
    with pytest.raises(ContractError, match="INCONCLUSIVE"):
        require_daily_runtime(load_data(ROOT / "config" / "runtime-readiness.yaml"))


def test_readiness_failure_finalizer_requires_email_without_operational_actions(
    tmp_path: Path,
) -> None:
    memory_before = (ROOT / "state" / "quality-memory.json").read_bytes()
    handoff_path = finalize_readiness_failure(
        load_data(ROOT / "config" / "runtime-readiness.yaml"),
        load_data(ROOT / "config" / "reporting.yaml"),
        "2026-08-21",
        output_root=tmp_path,
        generated_at="2026-08-21T08:00:00Z",
    )
    target = handoff_path.parent
    assert {path.name for path in target.iterdir()} == {
        "readiness-failure.json",
        "readiness-failure.md",
        "failure-email.html",
        "email-handoff.json",
    }

    report = json.loads((target / "readiness-failure.json").read_text(encoding="ascii"))
    handoff = json.loads(handoff_path.read_text(encoding="ascii"))
    validate_instance(report, SCHEMAS / "readiness-failure.schema.json", "failure")
    validate_instance(handoff, SCHEMAS / "email-handoff.schema.json", "handoff")
    assert report["status"] == "INCONCLUSIVE"
    assert report["prohibited_actions"] == list(READINESS_FAILURE_PROHIBITED_ACTIONS)
    assert report["performed_actions"] == [
        "render_failure_report",
        "render_failure_email",
        "create_email_handoff",
    ]
    assert handoff["required"] is True
    assert handoff["message_count"] == 1
    assert handoff["reporting_mode"] == "test"
    assert handoff["recipient_variable"] == "AIQ_TEST_REPORT_RECIPIENT"
    assert handoff["content_digest"].startswith("sha256:")
    assert handoff["sender_context"] == "authenticated_user_mailbox"
    assert handoff["delivery"] == {
        "status": "pending",
        "receipt_reference": None,
        "error_code": None,
    }
    rendered = (
        (target / "readiness-failure.md").read_text(encoding="ascii")
        + (target / "failure-email.html").read_text(encoding="ascii")
    ).lower()
    assert "email was sent" not in rendered
    assert "email was delivered" not in rendered
    assert (ROOT / "state" / "quality-memory.json").read_bytes() == memory_before


def test_run_daily_finalizes_before_returning_nonzero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            [
                "run-daily",
                "--report-date",
                "2026-08-21",
                "--output-root",
                str(tmp_path),
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert "INCONCLUSIVE" in output.err
    assert "Email-required handoff" in output.err
    assert (
        tmp_path / "daily" / "2026" / "08" / "21" / "email-handoff.json"
    ).is_file()


def test_email_delivery_result_is_recorded_once(tmp_path: Path) -> None:
    handoff_path = finalize_readiness_failure(
        load_data(ROOT / "config" / "runtime-readiness.yaml"),
        load_data(ROOT / "config" / "reporting.yaml"),
        "2026-08-21",
        output_root=tmp_path,
        generated_at="2026-08-21T08:00:00Z",
    )
    receipt = "sha256:" + ("a" * 64)
    record_email_delivery(handoff_path, status="sent", receipt_reference=receipt)
    handoff = json.loads(handoff_path.read_text(encoding="ascii"))
    assert handoff["delivery"] == {
        "status": "sent",
        "receipt_reference": receipt,
        "error_code": None,
    }
    with pytest.raises(ContractError, match="already recorded"):
        record_email_delivery(handoff_path, status="sent", receipt_reference=receipt)

    finalize_readiness_failure(
        load_data(ROOT / "config" / "runtime-readiness.yaml"),
        load_data(ROOT / "config" / "reporting.yaml"),
        "2026-08-21",
        output_root=tmp_path,
        generated_at="2026-08-21T09:00:00Z",
    )
    preserved = json.loads(handoff_path.read_text(encoding="ascii"))
    assert preserved["delivery"]["status"] == "sent"
    assert preserved["delivery"]["receipt_reference"] == receipt


def test_sent_receipt_cannot_be_reused_for_changed_readiness_content(tmp_path: Path) -> None:
    readiness = load_data(ROOT / "config" / "runtime-readiness.yaml")
    reporting = load_data(ROOT / "config" / "reporting.yaml")
    handoff_path = finalize_readiness_failure(
        readiness,
        reporting,
        "2026-08-21",
        output_root=tmp_path,
        generated_at="2026-08-21T08:00:00Z",
    )
    record_email_delivery(
        handoff_path,
        status="sent",
        receipt_reference="sha256:" + ("c" * 64),
    )
    delivered_bundle = {
        path.name: path.read_bytes() for path in handoff_path.parent.iterdir()
    }
    changed_readiness = deepcopy(readiness)
    changed_readiness["mandatory_components"]["infrastructure"] = True
    with pytest.raises(ContractError, match="content does not match"):
        finalize_readiness_failure(
            changed_readiness,
            reporting,
            "2026-08-21",
            output_root=tmp_path,
            generated_at="2026-08-21T09:00:00Z",
        )
    assert {
        path.name: path.read_bytes() for path in handoff_path.parent.iterdir()
    } == delivered_bundle


def test_content_digest_binds_email_subject(tmp_path: Path) -> None:
    handoff_path = finalize_readiness_failure(
        load_data(ROOT / "config" / "runtime-readiness.yaml"),
        load_data(ROOT / "config" / "reporting.yaml"),
        "2026-08-21",
        output_root=tmp_path,
        generated_at="2026-08-21T08:00:00Z",
    )
    handoff = json.loads(handoff_path.read_text(encoding="ascii"))
    handoff["subject"] = (
        "[Agent Insights Quality] INCONCLUSIVE - 2026-08-21 - alternate signal"
    )
    with pytest.raises(ContractError, match="content digest"):
        validate_stored_bundle_content(handoff_path, handoff)


def test_historical_test_handoff_survives_production_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_reporting = load_data(ROOT / "config" / "reporting.yaml")
    test_handoff_path = finalize_readiness_failure(
        load_data(ROOT / "config" / "runtime-readiness.yaml"),
        test_reporting,
        "2026-08-21",
        output_root=tmp_path / "reports",
        generated_at="2026-08-21T08:00:00Z",
    )
    production_reporting = deepcopy(test_reporting)
    production_reporting["mode"] = "production"
    production_reporting["recipient_variable"] = "AIQ_PRODUCTION_REPORT_RECIPIENT"

    test_handoff = json.loads(test_handoff_path.read_text(encoding="ascii"))
    validate_email_handoff(test_handoff, "historical handoff", production_reporting)
    monkeypatch.setattr(contracts, "ROOT", tmp_path)
    contracts.validate_report_artifacts([], {}, production_reporting)

    production_handoff_path = finalize_readiness_failure(
        load_data(ROOT / "config" / "runtime-readiness.yaml"),
        production_reporting,
        "2026-08-22",
        output_root=tmp_path / "reports",
        generated_at="2026-08-22T08:00:00Z",
    )
    production_handoff = json.loads(production_handoff_path.read_text(encoding="ascii"))
    validate_email_handoff(
        production_handoff,
        "new production handoff",
        production_reporting,
        require_current_selection=True,
    )
    assert production_handoff["reporting_mode"] == "production"
    assert production_handoff["recipient_variable"] == "AIQ_PRODUCTION_REPORT_RECIPIENT"


def test_handoff_recipient_must_match_its_recorded_mode(tmp_path: Path) -> None:
    reporting = load_data(ROOT / "config" / "reporting.yaml")
    handoff_path = finalize_readiness_failure(
        load_data(ROOT / "config" / "runtime-readiness.yaml"),
        reporting,
        "2026-08-21",
        output_root=tmp_path,
        generated_at="2026-08-21T08:00:00Z",
    )
    handoff = json.loads(handoff_path.read_text(encoding="ascii"))
    handoff["recipient_variable"] = "AIQ_PRODUCTION_REPORT_RECIPIENT"
    with pytest.raises(ContractError, match="recorded reporting mode"):
        validate_email_handoff(handoff, "handoff", reporting)


def test_failed_email_delivery_requires_sanitized_error_code(tmp_path: Path) -> None:
    handoff_path = finalize_readiness_failure(
        load_data(ROOT / "config" / "runtime-readiness.yaml"),
        load_data(ROOT / "config" / "reporting.yaml"),
        "2026-08-21",
        output_root=tmp_path,
        generated_at="2026-08-21T08:00:00Z",
    )
    with pytest.raises(ContractError, match="requires only an error code"):
        record_email_delivery(handoff_path, status="failed")

    record_email_delivery(handoff_path, status="failed", error_code="mail_unavailable")
    handoff = json.loads(handoff_path.read_text(encoding="ascii"))
    assert handoff["delivery"] == {
        "status": "failed",
        "receipt_reference": None,
        "error_code": "mail_unavailable",
    }


def test_record_email_result_cli_updates_pending_handoff(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handoff_path = finalize_readiness_failure(
        load_data(ROOT / "config" / "runtime-readiness.yaml"),
        load_data(ROOT / "config" / "reporting.yaml"),
        "2026-08-21",
        output_root=tmp_path,
        generated_at="2026-08-21T08:00:00Z",
    )
    receipt = "sha256:" + ("b" * 64)
    assert (
        main(
            [
                "record-email-result",
                "--handoff",
                str(handoff_path),
                "--status",
                "sent",
                "--receipt-reference",
                receipt,
            ]
        )
        == 0
    )
    assert "Email delivery result recorded" in capsys.readouterr().out
    handoff = json.loads(handoff_path.read_text(encoding="ascii"))
    assert handoff["delivery"]["receipt_reference"] == receipt


@pytest.mark.parametrize(
    "filenames",
    [
        {"email-handoff.json"},
        {"readiness-failure.json", "readiness-failure.md", "failure-email.html"},
        {"failure-email.html"},
        {"plan.json", "plan.md", "report.json", "report.md", "email-handoff.json"},
        {
            "plan.json",
            "plan.md",
            "report.json",
            "report.md",
            "readiness-failure.json",
            "readiness-failure.md",
            "failure-email.html",
            "email-handoff.json",
        },
    ],
)
def test_report_layout_rejects_partial_or_mixed_readiness_bundles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filenames: set[str],
) -> None:
    day = tmp_path / "reports" / "daily" / "2026" / "08" / "21"
    day.mkdir(parents=True)
    for filename in filenames:
        (day / filename).write_text("{}\n", encoding="ascii")
    monkeypatch.setattr(contracts, "ROOT", tmp_path)
    with pytest.raises(ContractError, match="artifact set|exactly its four artifacts"):
        contracts.validate_report_layout()


def test_report_layout_accepts_only_complete_readiness_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = tmp_path / "reports" / "daily" / "2026" / "08" / "21"
    day.mkdir(parents=True)
    for filename in {
        "readiness-failure.json",
        "readiness-failure.md",
        "failure-email.html",
        "email-handoff.json",
    }:
        (day / filename).write_text("{}\n", encoding="ascii")
    monkeypatch.setattr(contracts, "ROOT", tmp_path)
    contracts.validate_report_layout()


def test_complete_readiness_bundle_is_schema_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = tmp_path / "reports"
    reporting = load_data(ROOT / "config" / "reporting.yaml")
    handoff_path = finalize_readiness_failure(
        load_data(ROOT / "config" / "runtime-readiness.yaml"),
        reporting,
        "2026-08-21",
        output_root=reports,
        generated_at="2026-08-21T08:00:00Z",
    )
    failure_path = handoff_path.with_name("readiness-failure.json")
    failure = json.loads(failure_path.read_text(encoding="ascii"))
    failure["status"] = "AT BAR"
    failure_path.write_text(json.dumps(failure, indent=2) + "\n", encoding="ascii")

    monkeypatch.setattr(contracts, "ROOT", tmp_path)
    with pytest.raises(ContractError, match="'INCONCLUSIVE' was expected"):
        contracts.validate_report_artifacts([], {}, reporting)
