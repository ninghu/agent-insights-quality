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
    import_email_receipt,
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
    request_path = finalize_readiness_failure(
        load_data(ROOT / "config" / "runtime-readiness.yaml"),
        load_data(ROOT / "config" / "reporting.yaml"),
        "2026-08-21",
        output_root=tmp_path,
        generated_at="2026-08-21T08:00:00Z",
    )
    target = request_path.parent
    assert {path.name for path in target.iterdir()} == {
        "readiness-failure.json",
        "readiness-failure.md",
        "failure-email.html",
        "email-send-request.json",
    }

    report = json.loads((target / "readiness-failure.json").read_text(encoding="ascii"))
    request = json.loads(request_path.read_text(encoding="ascii"))
    validate_instance(report, SCHEMAS / "readiness-failure.schema.json", "failure")
    validate_instance(request, SCHEMAS / "email-send-request.schema.json", "request")
    assert report["status"] == "INCONCLUSIVE"
    assert report["prohibited_actions"] == list(READINESS_FAILURE_PROHIBITED_ACTIONS)
    assert report["performed_actions"] == [
        "render_failure_report",
        "render_failure_email",
        "create_email_send_request",
    ]
    assert report["email_handoff_reference"] == "email-send-request.json"
    assert request["state"] == "unsent"
    assert request["attempt_count"] == 0
    assert request["recipient"] == {
        "mode": "authenticated_user",
        "address": None,
        "source": "connected_microsoft_mailbox",
    }
    assert request["content_digest"].startswith("sha256:")
    assert request["transport_strategy"]["stop_after_first_confirmed_success"] is True
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
        tmp_path / "daily" / "2026" / "08" / "21" / "email-send-request.json"
    ).is_file()


def _sent_receipt(request: dict) -> dict:
    reference = "sha256:" + ("a" * 64)
    return {
        "schema_version": "1.0.0",
        "request_hash": request["request_hash"],
        "content_digest": request["content_digest"],
        "state": "sent",
        "completed_at": "2026-08-21T08:01:00Z",
        "successful_transport": "connected_copilot_mail",
        "attempts": [
            {
                "transport": "connected_copilot_mail",
                "state": "sent",
                "content_digest": request["content_digest"],
                "host_id": None,
                "authorization_confirmed": True,
                "mailbox_match_verified": True,
                "sent_items_verified": True,
                "provider_reference": reference,
                "error": None,
            }
        ],
        "provider_reference": reference,
        "error": None,
    }


def test_readiness_email_requires_validated_transport_receipt(tmp_path: Path) -> None:
    request_path = finalize_readiness_failure(
        load_data(ROOT / "config" / "runtime-readiness.yaml"),
        load_data(ROOT / "config" / "reporting.yaml"),
        "2026-08-21",
        output_root=tmp_path,
        generated_at="2026-08-21T08:00:00Z",
    )
    request = json.loads(request_path.read_text(encoding="ascii"))
    receipt = _sent_receipt(request)

    imported = import_email_receipt(request, receipt)

    assert imported["state"] == "sent"
    forged = deepcopy(receipt)
    forged["provider_reference"] = "sha256:" + ("b" * 64)
    with pytest.raises(ContractError, match="matching opaque confirmation"):
        import_email_receipt(request, forged)


def test_changed_readiness_content_cannot_reuse_email_request(tmp_path: Path) -> None:
    readiness = load_data(ROOT / "config" / "runtime-readiness.yaml")
    reporting = load_data(ROOT / "config" / "reporting.yaml")
    request_path = finalize_readiness_failure(
        readiness,
        reporting,
        "2026-08-21",
        output_root=tmp_path,
        generated_at="2026-08-21T08:00:00Z",
    )
    delivered_bundle = {
        path.name: path.read_bytes() for path in request_path.parent.iterdir()
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
        path.name: path.read_bytes() for path in request_path.parent.iterdir()
    } == delivered_bundle


def test_legacy_record_email_result_cli_is_disabled(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request_path = finalize_readiness_failure(
        load_data(ROOT / "config" / "runtime-readiness.yaml"),
        load_data(ROOT / "config" / "reporting.yaml"),
        "2026-08-21",
        output_root=tmp_path,
        generated_at="2026-08-21T08:00:00Z",
    )
    assert (
        main(
            [
                "record-email-result",
                "--handoff",
                str(request_path),
                "--status",
                "sent",
                "--receipt-reference",
                "sha256:" + ("b" * 64),
            ]
        )
        == 1
    )
    assert "Legacy record-email-result is disabled" in capsys.readouterr().err


@pytest.mark.parametrize(
    "filenames",
    [
        {"email-handoff.json"},
        {"email-send-request.json"},
        {"email-send-request.json", "email-receipt.json"},
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
        {
            "plan.json",
            "plan.md",
            "report.json",
            "report.md",
            "readiness-failure.json",
            "readiness-failure.md",
            "failure-email.html",
            "email-send-request.json",
            "email-receipt.json",
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
    with pytest.raises(ContractError, match="artifact set|reviewed legacy"):
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

    for path in day.iterdir():
        path.unlink()
    for filename in {
        "readiness-failure.json",
        "readiness-failure.md",
        "failure-email.html",
        "email-send-request.json",
        "email-receipt.json",
    }:
        (day / filename).write_text("{}\n", encoding="ascii")
    contracts.validate_report_layout()


def test_complete_readiness_bundle_is_schema_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = tmp_path / "reports"
    reporting = load_data(ROOT / "config" / "reporting.yaml")
    request_path = finalize_readiness_failure(
        load_data(ROOT / "config" / "runtime-readiness.yaml"),
        reporting,
        "2026-08-21",
        output_root=reports,
        generated_at="2026-08-21T08:00:00Z",
    )
    request = json.loads(request_path.read_text(encoding="ascii"))
    receipt = _sent_receipt(request)
    (request_path.parent / "email-receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n",
        encoding="ascii",
    )
    failure_path = request_path.with_name("readiness-failure.json")
    failure = json.loads(failure_path.read_text(encoding="ascii"))
    failure["status"] = "AT BAR"
    failure_path.write_text(json.dumps(failure, indent=2) + "\n", encoding="ascii")

    monkeypatch.setattr(contracts, "ROOT", tmp_path)
    with pytest.raises(ContractError, match="'INCONCLUSIVE' was expected"):
        contracts.validate_report_artifacts([], {}, reporting)
