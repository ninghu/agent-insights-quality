from __future__ import annotations

import json
import re
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
from agent_insights_quality.github_preview import (
    bind_preview_publication,
    preview_links,
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
        "profile": "daily",
        "run_id": "aiq-20260824",
        "report_date": "2026-08-24",
        "test_region": "WestUS2",
        "score_comparison": {
            "report_date": "2026-08-23",
            "quality_score": 94.1,
            "delta": 5.9,
        },
        "summary": {
            "issues_correct": 20,
            "issues_incorrect": 0,
            "issues_missing": 0,
            "issues_expected": 20,
            "eligible_issue_count": 20,
            "skipped_issue_count": 0,
            "skipped_issues": [],
            "baseline_passed": 5,
            "baseline_coverage": {
                "eligible_agents": [
                    "weather-agent",
                    "healthcare-agent",
                    "finance-agent",
                    "travel-agent",
                    "support-ticket-agent",
                ],
                "missing_agents": [],
            },
            "noise_cards": 0,
            "duplicate_cards": 0,
            "quality_score": 100,
            "quality_score_formula": "correct_over_expected_plus_noise_v1",
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
    assert "100 / 100 (+5.9)" in request["html"]
    assert "How Scoring Works" in request["html"]
    assert request["html"].index("Quality score") < request["html"].index(
        "How Scoring Works"
    )
    assert request["subject"] == (
        "[Agent Insights Quality] 100/100 - 2026-08-24 - 20/20 issues - WestUS2"
    )
    assert re.search(r"\b(?:PASS|FAIL)\b", request["subject"]) is None
    assert re.search(r"\b(?:PASS|FAIL)\b", request["html"]) is None
    assert "status" not in report
    assert "docs/QUALITY_BAR.md#quality-score" in request["html"]
    assert "Open quality trend dashboard" in request["html"]
    assert _DASHBOARD_LINK in request["html"]
    assert "Agent Insights Quality</h1>" in request["html"]
    assert "View Insight Engine Improvement Report" in request["html"]
    assert 'href="https://github.com/ninghu/agent-insights-quality/blob/main/reports/insight-engine-improvement.md"' in request["html"]
    assert request["html"].index(">Report</th>") < request["html"].index(
        "View Insight Engine Improvement Report"
    )
    assert "Test Region: WestUS2" in request["html"]
    assert request["html"].index("Test Agents</h2>") < request["html"].index(
        "Foundry Project:"
    )
    assert request["html"].index("Foundry Project:") < request["html"].index(
        "Test Region:"
    )
    assert request["html"].index("Test Region:") < request["html"].index(
        ">Test agent</th>"
    )
    assert _PROJECT_LINK in request["html"]
    assert (
        "20 correct / 20 scored (0 incorrect, 0 missing, 0 skipped)"
        in request["html"]
    )
    assert "Complete for all 5 Test Agents" in request["html"]
    assert ">Skipped issues</td>" in request["html"]

    reduced = deepcopy(report)
    reduced["summary"].update(
        {
            "issues_correct": 19,
            "issues_expected": 19,
            "eligible_issue_count": 19,
            "skipped_issue_count": 1,
            "skipped_issues": [
                {
                    "issue_id": "issue-006",
                    "status": "skipped_agent_activation",
                    "reason_code": "agent_activation_below_threshold",
                }
            ],
            "baseline_coverage": {
                "eligible_agents": [
                    "healthcare-agent",
                    "finance-agent",
                    "travel-agent",
                    "support-ticket-agent",
                ],
                "missing_agents": ["weather-agent"],
            },
        }
    )
    reduced_request = create_request(
        reduced,
        "synthetic@microsoft.com",
        project_link=_PROJECT_LINK,
        dashboard_link=_DASHBOARD_LINK,
        adx_publication=_ADX_PUBLICATION,
    )
    assert "Missing: weather-agent" in reduced_request["html"]
    assert (
        "issue-006 (skipped_agent_activation: "
        "agent_activation_below_threshold)"
        in reduced_request["html"]
    )
    assert "0 noise, 0 duplicate" in request["html"]
    preview = runtime_root / "staging" / "run" / "report-preview.html"
    write_private_report_preview(request, preview)
    assert preview.read_text(encoding="utf-8") == request["html"]
    assert re.search(
        r"\b(?:PASS|FAIL)\b",
        preview.read_text(encoding="utf-8"),
    ) is None
    assert "coverage_quality_precision_v2" not in request["html"]
    staging_report = deepcopy(report)
    staging_report["profile"] = "staging"
    staging_request = create_request(
        staging_report,
        "synthetic@microsoft.com",
        dashboard_link=_DASHBOARD_LINK,
        adx_publication=_ADX_PUBLICATION,
    )
    assert "Quality score" in staging_request["html"]
    assert "View Insight Engine Improvement Report" not in staging_request["html"]
    assert "Test Region: WestUS2" in staging_request["html"]
    assert "WestUS2" not in staging_request["subject"]
    staging_preview = runtime_root / "staging" / "score" / "report-preview.html"
    write_private_report_preview(staging_request, staging_preview)
    assert "Quality score" in staging_preview.read_text(encoding="utf-8")
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
    assert test_request["subject"].endswith(" - WestUS2")
    assert test_request["delivery_mode"] == "test_email_only"
    assert "TEST RUN" in test_request["html"]
    assert "intentionally not published to ADX" in test_request["html"]
    assert "Open quality trend dashboard" not in test_request["html"]
    assert "Not published" in test_request["html"]
    assert "View Insight Engine Improvement Report" not in test_request["html"]
    preview_run_report = deepcopy(report)
    preview_run_report["run_id"] = "aiq-20260824-r01"
    links = preview_links(preview_run_report["run_id"])
    preview_request = create_request(
        preview_run_report,
        "synthetic-user@microsoft.com",
        project_link=_PROJECT_LINK,
        adx_publication={"status": "skipped_test", "error_code": None},
        test_run=True,
        preview_links=links,
    )
    assert links["report_url"] in preview_request["html"]
    assert all(url in preview_request["html"] for url in links["agent_urls"].values())
    assert "Not published" not in preview_request["html"]
    assert "View Insight Engine Improvement Report" not in preview_request["html"]
    publication = {
        "schema_version": "1.0.0",
        "kind": "daily-email-test-preview-publication",
        **links,
        "created_at": "2026-08-24T16:00:00+00:00",
        "commit_sha": "1" * 40,
        "content_digest": "sha256:" + "2" * 64,
        "manifest_digest": "sha256:" + "3" * 64,
    }
    bound_request = bind_preview_publication(preview_request, publication)
    assert bound_request["preview"] == publication
    assert bound_request["content_digest"] == preview_request["content_digest"]
    with pytest.raises(ContractError, match="dashboard publication to be skipped"):
        create_request(
            report,
            "synthetic-user@microsoft.com",
            dashboard_link=_DASHBOARD_LINK,
            adx_publication={"status": "skipped_test", "error_code": None},
            test_run=True,
        )
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
    assert "(+0.5)" in staging_request["html"]
    lower_score = deepcopy(report)
    lower_score["summary"]["quality_score"] = 80
    lower_score["summary"]["issues_correct"] = 16
    lower_score["summary"]["issues_incorrect"] = 4
    lower_score_request = create_request(
        lower_score,
        "synthetic@microsoft.com",
        dashboard_link=_DASHBOARD_LINK,
        adx_publication=_ADX_PUBLICATION,
    )
    assert "80/100" in lower_score_request["subject"]
    assert "80 / 100" in lower_score_request["html"]
    assert re.search(r"\b(?:PASS|FAIL)\b", lower_score_request["subject"]) is None
    assert re.search(r"\b(?:PASS|FAIL)\b", lower_score_request["html"]) is None
    assert "Overall judgment" not in lower_score_request["html"]
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
    import_receipt(request, path, tmp_path / "imported-failed.json")
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
    unknown_output = tmp_path / "imported-unknown.json"
    import_receipt(request, path, unknown_output)
    receipt["retry_allowed"] = True
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ContractError, match="cannot be retried"):
        import_receipt(request, path, unknown_output)
