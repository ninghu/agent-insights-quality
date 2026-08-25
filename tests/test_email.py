from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_insights_quality.email import (
    create_request,
    import_receipt,
    validate_published_receipt,
)
from agent_insights_quality.util import ContractError


def test_email_requires_reviewed_domain_and_one_success(tmp_path: Path) -> None:
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
        },
        "issues": [],
    }
    with pytest.raises(ContractError, match="microsoft.com"):
        create_request(report, "test@example.com")
    request = create_request(report, "synthetic@microsoft.com")
    assert "Recommended human validation" not in request["html"]
    assert ">Test agent</th>" in request["html"]
    assert ">Assigned issues</th>" in request["html"]
    assert ">Report</th>" in request["html"]
    assert ">Ownership</th>" in request["html"]
    receipt = {
        "schema_version": "1.0.0",
        "content_digest": request["content_digest"],
        "status": "sent",
        "attempts": [
            {
                "transport": "connected_microsoft_mail",
                "status": "sent",
                "content_digest": request["content_digest"],
                "provider_reference": "sha256:" + "a" * 64,
            }
        ],
    }
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    output = tmp_path / "imported.json"
    import_receipt(request, path, output)
    assert output.exists()
    validate_published_receipt(path, request["content_digest"])
    with pytest.raises(ContractError, match="expected content"):
        validate_published_receipt(path, "sha256:" + "b" * 64)
    receipt["status"] = "failed"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ContractError, match="cannot contain"):
        import_receipt(request, path, output)

    receipt = {
        "schema_version": "1.0.0",
        "content_digest": request["content_digest"],
        "status": "sent",
        "attempts": [
            {
                "transport": "connected_microsoft_mail",
                "status": "unavailable",
                "content_digest": request["content_digest"],
            },
            {
                "transport": "graph",
                "status": "unauthorized",
                "content_digest": request["content_digest"],
            },
            {
                "transport": "outlook",
                "status": "sent",
                "content_digest": request["content_digest"],
                "provider_reference": "sha256:" + "c" * 64,
            },
        ],
    }
    path.write_text(json.dumps(receipt), encoding="utf-8")
    import_receipt(request, path, output)
