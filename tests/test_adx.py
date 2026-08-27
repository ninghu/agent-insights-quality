from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality.adx import (
    AdxError,
    QualityAnalytics,
    build_publication_payload,
    configure_dashboard_link,
    publish_daily_report,
    publish_daily_report_best_effort,
    render_dashboard,
    resolve_dashboard_link,
    resolve_quality_analytics,
)
from agent_insights_quality.util import ROOT, read_json


class _FakeAdxClient:
    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []
        self.commands: list[str] = []
        self.closed = 0

    def query(self, database: str, query: str) -> list[dict[str, str]]:
        assert database == "AgentInsightsQuality"
        assert "DailyQualityPublications" in query
        return list(self.rows)

    def manage(self, database: str, command: str) -> None:
        assert database == "AgentInsightsQuality"
        self.commands.append(command)
        match = re.search(r"SourceDigest='(sha256:[0-9a-f]{64})'", command)
        assert match is not None
        self.rows = [{"SourceDigest": match.group(1)}]

    def close(self) -> None:
        self.closed += 1


def _report() -> dict:
    return deepcopy(read_json(ROOT / "reports" / "daily" / "2026" / "08" / "26" / "report.json"))


def _private_runtime(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / ".aiq-runtime" / "agent-insights-quality"
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", str(root))
    return root


def _analytics() -> QualityAnalytics:
    return QualityAnalytics(
        cluster_name="aiqadxsynthetic",
        cluster_uri="https://aiqadxsynthetic.westus2.kusto.windows.net",
    )


def test_publication_payload_contains_only_sanitized_metrics() -> None:
    payload = build_publication_payload(_report())
    assert len(payload["agents"]) == 5
    assert len(payload["issues"]) == 25
    assert len(payload["fields"]) == 175
    assert payload["run"]["quality_score_formula"] == "field_weighted_v1"
    assert {item["field"] for item in payload["fields"]} == {
        "category",
        "description",
        "linked_traces",
        "proposed_fix",
        "root_cause",
        "severity",
        "title",
    }
    rendered = json.dumps(payload, sort_keys=True)
    for excluded in (
        "catalog_hashes",
        "delivery",
        "evidence_reference",
        "foundry_version",
        "ownership_reason",
        "reasoning",
    ):
        assert excluded not in rendered


def test_publication_is_idempotent_and_rejects_digest_conflicts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    private_root = _private_runtime(tmp_path, monkeypatch)
    report = _report()
    client = _FakeAdxClient()

    def factory(_uri: str) -> _FakeAdxClient:
        return client

    first = publish_daily_report(
        report,
        analytics=_analytics(),
        client_factory=factory,
    )
    second = publish_daily_report(
        report,
        analytics=_analytics(),
        client_factory=factory,
    )
    assert first["status"] == "published"
    assert second["status"] == "already_published"
    assert len(client.commands) == 1
    assert client.closed == 2
    receipt = (
        private_root
        / "adx-publications"
        / f"{report['run_id']}.json"
    )
    assert read_json(receipt)["status"] == "already_published"

    changed = deepcopy(report)
    changed["issues"][0]["assessment"]["confidence"] = 0.5
    with pytest.raises(AdxError, match="different payload") as caught:
        publish_daily_report(
            changed,
            analytics=_analytics(),
            client_factory=factory,
        )
    assert caught.value.code == "digest_conflict"
    failed = read_json(receipt)
    assert failed["status"] == "failed"
    assert failed["error_code"] == "digest_conflict"


def test_publication_failure_writes_private_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    private_root = _private_runtime(tmp_path, monkeypatch)

    def fail(_uri: str):
        raise AdxError("Synthetic ADX failure", code="query_failed")

    with pytest.raises(AdxError, match="Synthetic ADX failure"):
        publish_daily_report(
            _report(),
            analytics=_analytics(),
            client_factory=fail,
        )
    report = _report()
    receipt = read_json(
        private_root / "adx-publications" / f"{report['run_id']}.json"
    )
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "query_failed"
    assert receipt["source_digest"] is None


def test_best_effort_publication_returns_operational_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _private_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "agent_insights_quality.adx.resolve_quality_analytics",
        lambda: (_ for _ in ()).throw(
            AdxError("Synthetic discovery failure", code="resource_resolution_failed")
        ),
    )
    receipt = publish_daily_report_best_effort(_report())
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "resource_resolution_failed"


def test_three_digit_rerun_identity_is_publishable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _private_runtime(tmp_path, monkeypatch)
    report = _report()
    report["run_id"] = "aiq-20260826-r100"
    client = _FakeAdxClient()
    receipt = publish_daily_report(
        report,
        analytics=_analytics(),
        client_factory=lambda _uri: client,
    )
    assert receipt["status"] == "published"


def test_dashboard_rendering_and_private_share_link(
    tmp_path: Path,
    monkeypatch,
) -> None:
    private_root = _private_runtime(tmp_path, monkeypatch)
    output = render_dashboard(analytics=_analytics())
    assert output.is_relative_to(private_root)
    dashboard = read_json(output)
    assert dashboard["dataSources"][0]["clusterUri"] == _analytics().cluster_uri
    assert dashboard["dataSources"][0]["database"] == "AgentInsightsQuality"
    assert "{{ADX_" not in output.read_text(encoding="ascii")
    assert len(dashboard["tiles"]) == 10
    assert dashboard["parameters"][0]["defaultValue"]["count"] == 90

    link = "https://dataexplorer.azure.com/dashboards/synthetic-quality?source=email"
    configured = configure_dashboard_link(link)
    assert configured.is_relative_to(private_root)
    assert resolve_dashboard_link() == link
    for invalid in (
        "http://dataexplorer.azure.com/dashboards/example",
        "https://example.com/dashboards/example",
        "https://dataexplorer.azure.com/",
    ):
        with pytest.raises(AdxError, match="invalid"):
            configure_dashboard_link(invalid)


def test_quality_analytics_resource_is_resolved_by_tags(monkeypatch) -> None:
    responses = iter(
        [
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "id": "/subscriptions/synthetic/resourceGroups/"
                            "agent-insights-quality-rg/providers/"
                            "Microsoft.Kusto/clusters/aiqadxsynthetic",
                            "name": "aiqadxsynthetic",
                            "tags": {
                                "purpose": "agent-insights-quality",
                                "component": "quality-analytics",
                            },
                        }
                    ]
                ),
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "properties": {
                            "uri": "https://aiqadxsynthetic.westus2.kusto.windows.net"
                        }
                    }
                ),
            ),
        ]
    )
    monkeypatch.setattr("agent_insights_quality.adx.azure_cli", lambda: "az")
    monkeypatch.setattr(
        "agent_insights_quality.adx.subprocess.run",
        lambda *args, **kwargs: next(responses),
    )
    assert resolve_quality_analytics() == _analytics()


def test_adx_infrastructure_uses_existing_production_resource_group() -> None:
    main = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
    module = (
        ROOT / "infra" / "modules" / "quality-analytics.bicep"
    ).read_text(encoding="utf-8")
    schema = (ROOT / "infra" / "quality-analytics.kql").read_text(
        encoding="utf-8"
    )
    assert "agent-insights-quality-rg" in main
    assert "Standard_E2ads_v5" in main
    assert "@minValue(2)" in main
    assert "tier: 'Standard'" in module
    assert "enableAutoStop: false" in module
    assert "softDeletePeriod: 'P730D'" in module
    assert "hotCachePeriod: 'P90D'" in module
    for function in (
        "AIQDailyRuns",
        "AIQDailyAgents",
        "AIQDailyIssues",
        "AIQDailyFields",
    ):
        assert function in schema
