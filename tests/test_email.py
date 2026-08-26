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
)
from agent_insights_quality.util import ContractError


def test_email_requires_reviewed_domain_and_one_success(tmp_path: Path) -> None:
    assert resolve_recipient() == "agentinsightsteam@microsoft.com"
    report = {
        "status": "PASS",
        "report_date": "2026-08-24",
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
    }
    with pytest.raises(ContractError, match="microsoft.com"):
        create_request(report, "test@example.com")
    request = create_request(report, "synthetic@microsoft.com")
    assert request["channel"] == "copilot_email"
    assert request["send_once"] is True
    assert request["retry_ambiguous"] is False
    assert "Recommended human validation" not in request["html"]
    assert ">Test agent</th>" in request["html"]
    assert ">Tested issues</th>" in request["html"]
    assert ">Report</th>" in request["html"]
    assert ">Ownership</th>" not in request["html"]
    assert "Quality Score: 100/100" in request["html"]
    assert "How Scoring Works" in request["html"]
    assert "docs/QUALITY_BAR.md#quality-score" in request["html"]
    work_item_request = create_request(
        report,
        "synthetic@microsoft.com",
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
    assert ">ID</th>" in work_item_request["html"]
    assert "Synthetic &amp; escaped" in work_item_request["html"]
    assert 'href="https://synthetic.example/workitems/42"' in work_item_request["html"]
    assert 'href="https://synthetic.example/workitems/41"' in work_item_request["html"]
    assert "Removed items are excluded" in work_item_request["html"]
    incomplete = deepcopy(report)
    incomplete["status"] = "INCOMPLETE"
    incomplete["summary"]["quality_score"] = None
    incomplete["summary"]["incomplete"] = True
    incomplete["summary"]["incomplete_reasons"] = ["clean_window_not_empty"]
    incomplete_request = create_request(incomplete, "synthetic@microsoft.com")
    assert "pre-existing telemetry" in incomplete_request["html"]
    assert "no Agent traffic was sent" in incomplete_request["html"]
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
