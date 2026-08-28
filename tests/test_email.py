from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from agent_insights_quality.email import (
    create_request,
    import_receipt,
    resolve_recipient,
    validate_published_receipt,
    write_private_report_preview,
)
from agent_insights_quality.util import ContractError

_DASHBOARD_LINK = "https://aka.ms/agent-insights/quality"
_PROJECT_LINK = "https://ai.azure.com/synthetic-project"
_ADX_PUBLICATION = {"status": "published", "error_code": None}


def test_email_requires_reviewed_domain_and_one_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime_root = tmp_path / ".aiq-runtime" / "test-runtime"
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", str(runtime_root))
    assert resolve_recipient() == "agentinsightsteam@microsoft.com"
    with pytest.raises(ContractError, match="private test email recipient"):
        resolve_recipient(test_run=True)
    override = runtime_root / "config" / "email-recipient.json"
    override.parent.mkdir(parents=True)
    override.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "purpose": "daily_test",
                "recipient": "synthetic-user@microsoft.com",
            }
        ),
        encoding="utf-8",
    )
    assert resolve_recipient() == "agentinsightsteam@microsoft.com"
    assert resolve_recipient(test_run=True) == "synthetic-user@microsoft.com"
    override.unlink()
    report = {
        "status": "PASS",
        "profile": "daily",
        "run_id": "aiq-20260824",
        "report_date": "2026-08-24",
        "score_comparison": {
            "report_date": "2026-08-23",
            "quality_score": 94.1,
            "delta": 5.9,
        },
        "summary": {
            "issues_correct": 25,
            "issues_expected": 25,
            "baseline_passed": 5,
            "issues_partial": 0,
            "noise_cards": 0,
            "unverified_cards": 0,
            "observed_cards": 25,
            "field_quality_score": 100,
            "clean_card_precision": 100,
            "quality_score": 100,
            "quality_threshold": 90,
            "quality_score_formula": "field_weighted_v1",
            "incomplete_reasons": [],
            "incomplete": False,
        },
        "issues": [],
        "baseline": [
            {
                "agent": agent,
                "insight_count": 0,
                "assessment": {"verdict": "clean"},
            }
            for agent in (
                "weather-agent",
                "healthcare-agent",
                "finance-agent",
                "travel-agent",
                "support-ticket-agent",
            )
        ],
    }
    with pytest.raises(ContractError, match="microsoft.com"):
        create_request(report, "test@example.com")
    with pytest.raises(ContractError, match="exactly one"):
        create_request(
            report,
            "external@example.com, synthetic@microsoft.com",
        )
    with pytest.raises(ContractError, match="exactly one"):
        create_request(report, "Example User <synthetic@microsoft.com>")
    with pytest.raises(ContractError, match="exactly one"):
        create_request(report, "user@m\u0131crosoft.com")
    request = create_request(
        report,
        "synthetic@microsoft.com",
        project_link=_PROJECT_LINK,
        dashboard_link=_DASHBOARD_LINK,
        adx_publication=_ADX_PUBLICATION,
    )
    assert request["channel"] == "copilot_email"
    assert request["send_once"] is True
    assert request["retry_ambiguous"] is False
    assert request["delivery_mode"] == "official"
    assert "Recommended human validation" not in request["html"]
    assert ">Test agent</th>" in request["html"]
    assert ">Owner</th>" in request["html"]
    assert ">Tested issues</th>" in request["html"]
    assert ">Report</th>" in request["html"]
    assert ">Ownership</th>" not in request["html"]
    assert "Quality Score: 100/100" in request["html"]
    assert "(+5.9) &middot; PASS</span>" in request["html"]
    assert "How Scoring Works" in request["html"]
    assert request["html"].index("PASS</span>") < request["html"].index(
        "How Scoring Works"
    )
    assert 'style="color:#dbeafe;text-decoration:underline;"' in request["html"]
    assert "(<a" not in request["html"]
    assert "Works</a>)" not in request["html"]
    assert "docs/QUALITY_BAR.md#quality-score" in request["html"]
    assert "How to read results" in request["html"]
    assert "docs/INSIGHT_RESULTS.md" in request["html"]
    assert "Open quality trend dashboard" in request["html"]
    assert _DASHBOARD_LINK in request["html"]
    assert request["html"].index("Test Agents</h2>") < request["html"].index(
        "Foundry Project:"
    )
    assert request["html"].index("Foundry Project:") < request["html"].index(
        ">Test agent</th>"
    )
    assert _PROJECT_LINK in request["html"]
    assert "Incorrect related Insights" in request["html"]
    assert "Noise/duplicate Insights" in request["html"]
    assert "Incorrect/noisy insights" not in request["html"]
    assert request["html"].index("Incorrect related Insights") < request["html"].index(
        "How to read results"
    )
    preview = runtime_root / "staging" / "run" / "report-preview.html"
    write_private_report_preview(request, preview)
    assert preview.read_text(encoding="utf-8") == request["html"]
    with pytest.raises(ContractError, match="private runtime root"):
        write_private_report_preview(request, tmp_path / "public-preview.html")
    for owner in (
        "Han Che",
        "Sean Gayler",
        "Nishal Dsilva",
        "Ilya Matiach",
        "Billy Hu",
    ):
        assert owner in request["html"]
    work_item_request = create_request(
        report,
        "synthetic@microsoft.com",
        dashboard_link=_DASHBOARD_LINK,
        adx_publication=_ADX_PUBLICATION,
        work_items={
            "closed_business_date": "2026-08-23",
            "active_items": [
                {
                    "id": 42,
                    "type": "Bug",
                    "title": "Synthetic & escaped",
                    "assigned_to": "Example Owner",
                    "state": "Active",
                    "url": "https://synthetic.example/workitems/42",
                }
            ],
            "closed_yesterday_items": [
                {
                    "id": 41,
                    "type": "Bug",
                    "title": "Closed synthetic issue",
                    "assigned_to": "Example Owner",
                    "state": "Closed",
                    "url": "https://synthetic.example/workitems/41",
                }
            ],
        },
    )
    assert "Quality work items" in work_item_request["html"]
    assert ">Active</h3>" in work_item_request["html"]
    assert "Closed yesterday (2026-08-23)" in work_item_request["html"]
    assert ">Owner</th>" in work_item_request["html"]
    assert ">Assigned to</th>" not in work_item_request["html"]
    assert ">ID</th>" in work_item_request["html"]
    assert "Synthetic &amp; escaped" in work_item_request["html"]
    assert 'href="https://synthetic.example/workitems/42"' in work_item_request["html"]
    assert 'href="https://synthetic.example/workitems/41"' in work_item_request["html"]
    assert "Removed items are excluded" in work_item_request["html"]
    test_request = create_request(
        report,
        "synthetic-user@microsoft.com",
        project_link=_PROJECT_LINK,
        adx_publication={"status": "skipped_test", "error_code": None},
        test_run=True,
    )
    assert test_request["subject"].startswith("[TEST] [Agent Insights Quality]")
    assert test_request["delivery_mode"] == "test_email_only"
    assert "TEST RUN" in test_request["html"]
    assert "intentionally not published to ADX" in test_request["html"]
    assert "Open quality trend dashboard" not in test_request["html"]
    assert "Not published" in test_request["html"]
    with pytest.raises(ContractError, match="dashboard publication to be skipped"):
        create_request(
            report,
            "synthetic-user@microsoft.com",
            dashboard_link=_DASHBOARD_LINK,
            adx_publication={"status": "skipped_test", "error_code": None},
            test_run=True,
        )
    incomplete = deepcopy(report)
    incomplete["status"] = "INCOMPLETE"
    incomplete["summary"]["quality_score"] = None
    incomplete["summary"]["incomplete"] = True
    incomplete["summary"]["incomplete_reasons"] = ["clean_window_not_empty"]
    incomplete["score_comparison"] = None
    incomplete_request = create_request(
        incomplete,
        "synthetic@microsoft.com",
        dashboard_link=_DASHBOARD_LINK,
        adx_publication={"status": "failed", "error_code": "query_failed"},
    )
    assert "Quality Score: N/A (change N/A)" in incomplete_request["html"]
    assert "pre-existing telemetry" in incomplete_request["html"]
    assert "no Agent traffic was sent" in incomplete_request["html"]
    assert "ADX publication failed for this run" in incomplete_request["html"]
    staging = deepcopy(report)
    staging["profile"] = "staging"
    staging["score_comparison"] = {
        "report_date": "2026-08-27",
        "run_id": "aiq-20260827-r29",
        "quality_score": 47.8,
        "delta": 0.5,
    }
    staging_request = create_request(
        staging,
        "synthetic@microsoft.com",
    )
    assert "(+0.5) &middot; PASS</span>" in staging_request["html"]
    receipt = {
        "schema_version": "2.0.0",
        "content_digest": request["content_digest"],
        "status": "sent",
        "provider_reference": "sha256:" + "a" * 64,
        "retry_allowed": False,
    }
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    output = tmp_path / "imported.json"
    import_receipt(request, path, output)
    assert output.exists()
    validate_published_receipt(path, request["content_digest"])
    with pytest.raises(ContractError, match="does not match"):
        validate_published_receipt(path, "sha256:" + "b" * 64)
    receipt["status"] = "failed"
    receipt["provider_reference"] = None
    receipt["retry_allowed"] = True
    path.write_text(json.dumps(receipt), encoding="utf-8")
    import_receipt(request, path, output)
    with pytest.raises(ContractError, match="confirmed"):
        validate_published_receipt(path, request["content_digest"])

    receipt = {
        "schema_version": "2.0.0",
        "content_digest": request["content_digest"],
        "status": "unknown",
        "provider_reference": None,
        "retry_allowed": False,
    }
    path.write_text(json.dumps(receipt), encoding="utf-8")
    import_receipt(request, path, output)
    receipt["retry_allowed"] = True
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ContractError, match="cannot be retried"):
        import_receipt(request, path, output)
