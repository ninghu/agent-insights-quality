from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality.adx import (
    PAYLOAD_VERSION,
    AdxError,
    QualityAnalytics,
    build_publication_payload,
    publish_daily_report,
    publish_daily_report_best_effort,
    render_dashboard,
    resolve_dashboard_link,
    resolve_quality_analytics,
    resolve_report_catalogs,
)
from agent_insights_quality.catalogs import load_catalogs, source_integrity_digest
from agent_insights_quality.report_summary import (
    improvement_rows,
    working_capabilities,
)
from agent_insights_quality.reporting import _summary_metrics, issue_primary_card
from agent_insights_quality.scoring import issue_outcome
from agent_insights_quality.util import ROOT, content_hash, read_json


class _FakeAdxClient:
    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []
        self.commands: list[str] = []
        self.closed = 0

    def query(self, database: str, query: str) -> list[dict[str, str]]:
        assert database == "AgentInsightsQuality"
        assert "DailyQualityPublications" in query
        assert "PayloadVersion == '3.0.0'" in query
        return list(self.rows)

    def manage(self, database: str, command: str) -> None:
        assert database == "AgentInsightsQuality"
        self.commands.append(command)
        assert "PayloadVersion='3.0.0'" in command
        assert "aiq-v3-run:" in command
        assert command.index("Payload=parse_json") < command.index(
            "PayloadVersion="
        )
        match = re.search(r"SourceDigest='(sha256:[0-9a-f]{64})'", command)
        assert match is not None
        self.rows = [{"SourceDigest": match.group(1)}]

    def close(self) -> None:
        self.closed += 1


def _report() -> dict:
    report = deepcopy(read_json(_report_path()))
    report["schema_version"] = "3.0.0"
    report.pop("status", None)
    report["test_region"] = "WestUS2"
    agents, issues = load_catalogs(require_paths=False)
    report["catalog_hashes"]["agents"] = content_hash(agents)
    report["catalog_hashes"]["issues"] = content_hash(issues)
    report["source_integrity"] = {
        "verified": True,
        "contract_digest": source_integrity_digest(agents, issues),
    }
    issue_by_id = {item["id"]: item for item in issues["issues"]}
    selected: list[dict] = []
    for agent in agents["agents"]:
        selected.extend(
            [
                item
                for item in report["issues"]
                if item["agent"] == agent["name"]
            ][:4]
        )
    report["issues"] = selected
    for item in report["issues"]:
        item["title"] = issue_by_id[item["issue_id"]]["title"]
        assessment = item["assessment"]
        assessment["fields"].pop("root_cause", None)
        assessment.setdefault("reasoning", assessment["ownership_reason"])
        for card in assessment["card_evaluations"]:
            card["fields"].pop("root_cause", None)
            if card["finding_type"] in {"PARTIAL", "MISMATCHED"}:
                card["field_reasons"] = {
                    field: (
                        f"The synthetic historical fixture marks {field} "
                        "incorrect for this card."
                    )
                    for field, passed in card["fields"].items()
                    if passed is False
                }
        item["observed_count"] = len(assessment["card_evaluations"])
        item.pop("result", None)
        item["outcome"] = issue_outcome(assessment["card_evaluations"])
        item.pop("shadow_v2_primary", None)
    report["summary"] = _summary_metrics(report["baseline"], report["issues"])
    return report


def _report_path() -> Path:
    return ROOT / "reports" / "daily" / "2026" / "08" / "26" / "report.json"


def _private_runtime(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / ".aiq-runtime" / "agent-insights-quality"
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", str(root))
    return root


def _analytics() -> QualityAnalytics:
    return QualityAnalytics(
        cluster_name="aiqadxsynthetic",
        cluster_uri="https://aiqadxsynthetic.westus2.kusto.windows.net",
    )


def test_publication_payload_contains_public_safe_explanations() -> None:
    report = _report()
    payload = build_publication_payload(
        report,
        source_path=_report_path(),
    )
    assert payload["schema_version"] == PAYLOAD_VERSION
    assert len(payload["agents"]) == 5
    assert len(payload["baselines"]) == 5
    assert len(payload["issues"]) == 20
    assert len(payload["cards"]) == sum(
        len(item["assessment"]["card_evaluations"])
        for item in [*report["baseline"], *report["issues"]]
    )
    assert len(payload["fields"]) == sum(
        len((issue_primary_card(item) or {}).get("fields", {}))
        for item in report["issues"]
    )
    assert payload["run"]["quality_score_formula"] == (
        "correct_over_expected_plus_noise_v1"
    )
    assert payload["run"]["report_url"].endswith(
        "/reports/daily/2026/08/26/report.md"
    )
    assert all(item["owner"] for item in payload["agents"])
    issue = payload["issues"][0]
    assert issue["title"]
    assert issue["expected_root_cause"]
    assert issue["expected_fix"]
    assert issue["ownership_reason"]
    assert issue["issue_url"].endswith(f"#{issue['issue_id']}")
    assert issue["fields_expected"] in {0, 6}
    card = payload["cards"][0]
    assert card["reasoning"]
    assert card["ownership_reason"]
    assert card["report_url"].startswith("https://github.com/")
    committed_reasoning = {
        card["reasoning"]
        for item in report["baseline"]
        for card in item["assessment"]["card_evaluations"]
    } | {
        card["reasoning"]
        for item in report["issues"]
        for card in item["assessment"]["card_evaluations"]
    }
    assert {item["reasoning"] for item in payload["cards"]} == committed_reasoning
    expected_highlights = {
        ("What is working", title, description, "")
        for title, description in working_capabilities(report)
    } | {
        ("What needs improvement", title, what_happened, needed_behavior)
        for title, what_happened, needed_behavior in improvement_rows(report)
    }
    assert {
        (
            item["section"],
            item["title"],
            item["what_happened"],
            item["needed_behavior"],
        )
        for item in payload["highlights"]
    } == expected_highlights
    assert {item["field"] for item in payload["fields"]} == {
        "category",
        "description",
        "linked_traces",
        "proposed_fix",
        "severity",
        "title",
    }
    rendered = json.dumps(payload, sort_keys=True)
    assert "coverage_quality_precision_v2" not in rendered
    for excluded_key in (
        "catalog_hashes",
        "delivery",
        "evidence_reference",
        "foundry_version",
        "manifest_reference",
        "reference",
    ):
        assert f'"{excluded_key}":' not in rendered
    for excluded_value in (
        ".services.ai.azure.com",
        "/subscriptions/",
        "active_items",
        "closed_yesterday_items",
    ):
        assert excluded_value not in rendered


def test_historical_report_resolves_its_reviewed_catalog_snapshot(
    monkeypatch,
) -> None:
    report = _report()
    current = load_catalogs(require_paths=False)
    historical = deepcopy(current)
    for agent in historical[0]["agents"]:
        agent.pop("owner", None)
    report["catalog_hashes"]["agents"] = content_hash(historical[0])
    report["catalog_hashes"]["issues"] = content_hash(historical[1])
    original = "a" * 40
    monkeypatch.setattr(
        "agent_insights_quality.adx._run_git",
        lambda _arguments: original,
    )
    monkeypatch.setattr(
        "agent_insights_quality.adx._catalogs_at_commit",
        lambda _commit: historical,
    )
    agents, issues = resolve_report_catalogs(
        report,
        source_path=_report_path(),
        current_catalogs=current,
    )
    assert {item["name"] for item in agents["agents"]} == {
        item["agent"] for item in report["baseline"]
    }
    assert {
        item["id"]
        for item in issues["issues"]
        if item["id"] in {result["issue_id"] for result in report["issues"]}
    } == {result["issue_id"] for result in report["issues"]}
    with pytest.raises(AdxError, match="catalog snapshot"):
        resolve_report_catalogs(report, current_catalogs=current)


def test_invalid_historical_catalog_candidate_is_skipped(monkeypatch) -> None:
    report = _report()
    current = load_catalogs(require_paths=False)
    historical = deepcopy(current)
    for agent in historical[0]["agents"]:
        agent.pop("owner", None)
    report["catalog_hashes"]["agents"] = content_hash(historical[0])
    report["catalog_hashes"]["issues"] = content_hash(historical[1])
    commits = ["a" * 40, "b" * 40]
    calls = iter(
        [
            AdxError(
                "Synthetic invalid catalog",
                code="catalog_snapshot_unavailable",
            ),
            historical,
        ]
    )

    def catalog_at_commit(_commit: str):
        value = next(calls)
        if isinstance(value, AdxError):
            raise value
        return value

    monkeypatch.setattr(
        "agent_insights_quality.adx._run_git",
        lambda _arguments: "\n".join(commits),
    )
    monkeypatch.setattr(
        "agent_insights_quality.adx._catalogs_at_commit",
        catalog_at_commit,
    )
    assert resolve_report_catalogs(
        report,
        source_path=_report_path(),
        current_catalogs=current,
    ) == historical


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
        source_path=_report_path(),
        analytics=_analytics(),
        client_factory=factory,
    )
    second = publish_daily_report(
        report,
        source_path=_report_path(),
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
            source_path=_report_path(),
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
            source_path=_report_path(),
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
    receipt = publish_daily_report_best_effort(
        _report(),
        source_path=_report_path(),
    )
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
        source_path=_report_path(),
        analytics=_analytics(),
        client_factory=lambda _uri: client,
    )
    assert receipt["status"] == "published"
    assert receipt["payload_version"] == PAYLOAD_VERSION


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
    assert [page["name"] for page in dashboard["pages"]] == [
        "Trend",
        "Daily Results",
        "Agent and Issue Explanation",
    ]
    assert len(dashboard["parameters"]) == 6
    assert len(dashboard["tiles"]) == 17
    assert dashboard["parameters"][0]["defaultValue"]["count"] == 90
    assert {
        parameter.get("variableName")
        for parameter in dashboard["parameters"]
        if parameter.get("variableName")
    } == {
        "_reportDate",
        "_agent",
        "_issue",
        "_result",
        "_ownership",
    }

    assert resolve_dashboard_link() == "https://aka.ms/agent-insights/quality"


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
    analytics = (ROOT / "infra" / "analytics.bicep").read_text(encoding="utf-8")
    module = (
        ROOT / "infra" / "modules" / "quality-analytics.bicep"
    ).read_text(encoding="utf-8")
    schema = (ROOT / "infra" / "quality-analytics.kql").read_text(
        encoding="utf-8"
    )
    assert "agent-insights-quality-rg" in main
    assert "quality-analytics.bicep" not in main
    assert "Standard_E2ads_v5" in analytics
    assert "@minValue(2)" in analytics
    assert "tier: 'Standard'" in module
    assert "enableAutoStop: false" in module
    assert "softDeletePeriod: 'P730D'" in module
    assert "hotCachePeriod: 'P90D'" in module
    for function in (
        "AIQDailyRuns",
        "AIQDailyAgents",
        "AIQDailyBaselines",
        "AIQDailyIssues",
        "AIQDailyCards",
        "AIQDailyFields",
        "AIQDailyHighlights",
    ):
        assert function in schema
    assert "where PayloadVersion == '3.0.0'" in schema
