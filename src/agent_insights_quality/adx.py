from __future__ import annotations

import base64
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import yaml

from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.progress import ProgressReporter
from agent_insights_quality.catalogs import load_catalogs, validate_semantics
from agent_insights_quality.profiles import RESOURCE_GROUP
from agent_insights_quality.report_summary import (
    improvement_rows,
    working_capabilities,
)
from agent_insights_quality.reporting import REQUIRED_FIELDS, validate_report
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    atomic_json,
    canonical_bytes,
    content_hash,
    read_json,
    read_yaml,
    runtime_root,
)

ADX_DATABASE = "AgentInsightsQuality"
_PROGRESS = ProgressReporter("aiq-adx")
ADX_TABLE = "DailyQualityPublications"
PAYLOAD_VERSION = "2.0.0"
_ADX_API_VERSION = "2025-02-14"
_RUN_ID = re.compile(r"aiq-[0-9]{8}(?:-r[0-9]{2,})?", re.ASCII)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
_DASHBOARD_TEMPLATE = ROOT / "dashboards" / "agent-insights-quality.template.json"
_PUBLIC_REPOSITORY_URL = "https://github.com/ninghu/agent-insights-quality/blob/main"
_REVIEWED_DASHBOARD_URL = "https://aka.ms/agent-insights/quality"


class AdxError(ContractError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class QualityAnalytics:
    cluster_name: str
    cluster_uri: str
    database_name: str = ADX_DATABASE


class AdxClient(Protocol):
    def query(self, database: str, query: str) -> list[dict[str, Any]]: ...

    def manage(self, database: str, command: str) -> None: ...

    def close(self) -> None: ...


class _AzureKustoClient:
    def __init__(self, cluster_uri: str) -> None:
        try:
            from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
            from azure.kusto.data.exceptions import (
                KustoClientError,
                KustoServiceError,
            )
        except ImportError as error:
            raise AdxError(
                "Azure Data Explorer client dependency is unavailable",
                code="dependency_unavailable",
            ) from error
        self._error_types = (KustoClientError, KustoServiceError)
        connection = KustoConnectionStringBuilder.with_az_cli_authentication(
            cluster_uri
        )
        self._client = KustoClient(connection)

    def query(self, database: str, query: str) -> list[dict[str, Any]]:
        try:
            with _PROGRESS.heartbeat("ADX verification query"):
                response = self._client.execute(database, query)
        except self._error_types as error:
            raise AdxError(
                "Azure Data Explorer verification query failed",
                code="query_failed",
            ) from error
        if not response.primary_results:
            raise AdxError(
                "Azure Data Explorer returned no query result",
                code="query_failed",
            )
        return [row.to_dict() for row in response.primary_results[0]]

    def manage(self, database: str, command: str) -> None:
        try:
            with _PROGRESS.heartbeat("ADX management command"):
                self._client.execute_mgmt(database, command)
        except self._error_types as error:
            raise AdxError(
                "Azure Data Explorer publication command failed",
                code="ingestion_failed",
            ) from error

    def close(self) -> None:
        self._client.close()


def _default_client_factory(cluster_uri: str) -> AdxClient:
    return _AzureKustoClient(cluster_uri)


def _run_azure(arguments: list[str], *, code: str) -> Any:
    try:
        with _PROGRESS.heartbeat("ADX Azure operation") as progress:
            process = subprocess.run(
                [azure_cli(), *arguments],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if process.returncode != 0:
                progress.fail()
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AdxError(
            "Azure Data Explorer resource discovery failed",
            code=code,
        ) from error
    if process.returncode != 0:
        raise AdxError(
            "Azure Data Explorer resource discovery failed",
            code=code,
        )
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise AdxError(
            "Azure Data Explorer resource discovery returned invalid JSON",
            code=code,
        ) from error


def resolve_quality_analytics() -> QualityAnalytics:
    resources = _run_azure(
        [
            "resource",
            "list",
            "--resource-group",
            RESOURCE_GROUP,
            "--resource-type",
            "Microsoft.Kusto/clusters",
            "--output",
            "json",
        ],
        code="resource_resolution_failed",
    )
    if not isinstance(resources, list):
        raise AdxError(
            "Azure Data Explorer resource discovery returned an invalid payload",
            code="resource_resolution_failed",
        )
    matches = [
        item
        for item in resources
        if isinstance(item, dict)
        and isinstance(item.get("tags"), dict)
        and item["tags"].get("purpose") == "agent-insights-quality"
        and item["tags"].get("component") == "quality-analytics"
    ]
    if len(matches) != 1:
        raise AdxError(
            "The fixed Azure Data Explorer cluster could not be resolved uniquely",
            code="resource_resolution_failed",
        )
    resource_id = str(matches[0].get("id") or "")
    cluster_name = str(matches[0].get("name") or "")
    if (
        not resource_id
        or re.fullmatch(r"[a-z][a-z0-9]{3,21}", cluster_name, re.ASCII) is None
    ):
        raise AdxError(
            "The Azure Data Explorer resource identity is invalid",
            code="resource_resolution_failed",
        )
    detail = _run_azure(
        [
            "resource",
            "show",
            "--ids",
            resource_id,
            "--api-version",
            _ADX_API_VERSION,
            "--output",
            "json",
        ],
        code="resource_resolution_failed",
    )
    if not isinstance(detail, dict) or not isinstance(detail.get("properties"), dict):
        raise AdxError(
            "The Azure Data Explorer resource detail is invalid",
            code="resource_resolution_failed",
        )
    cluster_uri = str(detail["properties"].get("uri") or "")
    _validate_cluster_uri(cluster_uri)
    return QualityAnalytics(cluster_name=cluster_name, cluster_uri=cluster_uri)


def _validate_cluster_uri(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise AdxError(
            "The Azure Data Explorer cluster URI is invalid",
            code="resource_resolution_failed",
        ) from error
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
        or not parsed.hostname.endswith(".kusto.windows.net")
    ):
        raise AdxError(
            "The Azure Data Explorer cluster URI is invalid",
            code="resource_resolution_failed",
        )


def render_dashboard(
    output: Path | None = None,
    *,
    analytics: QualityAnalytics | None = None,
) -> Path:
    private_root = runtime_root()
    output = (
        output
        if output is not None
        else private_root / "dashboards" / "agent-insights-quality.json"
    ).resolve()
    if not output.is_relative_to(private_root):
        raise AdxError(
            "Rendered ADX dashboard must stay under the private runtime root",
            code="invalid_dashboard_output",
        )
    target = analytics or resolve_quality_analytics()
    template = read_json(_DASHBOARD_TEMPLATE)
    replacements = {
        "{{ADX_CLUSTER_NAME}}": target.cluster_name,
        "{{ADX_CLUSTER_URI}}": target.cluster_uri,
        "{{ADX_DATABASE}}": target.database_name,
    }
    rendered = _replace_dashboard_values(template, replacements)
    serialized = json.dumps(rendered, sort_keys=True)
    if "{{ADX_" in serialized:
        raise AdxError(
            "Rendered ADX dashboard contains unresolved placeholders",
            code="invalid_dashboard_template",
        )
    _validate_dashboard(rendered, target)
    atomic_json(output, rendered)
    return output


def _replace_dashboard_values(
    value: Any,
    replacements: dict[str, str],
) -> Any:
    if isinstance(value, dict):
        return {
            key: _replace_dashboard_values(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_dashboard_values(item, replacements) for item in value]
    if isinstance(value, str):
        for placeholder, replacement in replacements.items():
            value = value.replace(placeholder, replacement)
    return value


def _validate_dashboard(
    dashboard: dict[str, Any],
    analytics: QualityAnalytics,
) -> None:
    data_sources = dashboard.get("dataSources")
    tiles = dashboard.get("tiles")
    pages = dashboard.get("pages")
    parameters = dashboard.get("parameters")
    if (
        dashboard.get("title") != "Agent Insights Quality Trends"
        or not isinstance(data_sources, list)
        or len(data_sources) != 1
        or not isinstance(tiles, list)
        or len(tiles) < 8
        or not isinstance(pages, list)
        or len(pages) != 3
        or any(not isinstance(page, dict) for page in pages)
        or [page.get("name") for page in pages]
        != ["Trend", "Daily Results", "Agent and Issue Explanation"]
        or not isinstance(parameters, list)
        or len(parameters) != 6
    ):
        raise AdxError(
            "ADX dashboard template is structurally invalid",
            code="invalid_dashboard_template",
        )
    source = data_sources[0]
    tile_queries = [
        str(tile.get("query") or "")
        for tile in tiles
        if isinstance(tile, dict)
    ]
    parameter_queries = [
        str(parameter["dataSource"].get("query") or "")
        for parameter in parameters
        if isinstance(parameter, dict)
        and isinstance(parameter.get("dataSource"), dict)
    ]
    required_views = {
        "AIQDailyRuns",
        "AIQDailyAgents",
        "AIQDailyBaselines",
        "AIQDailyIssues",
        "AIQDailyCards",
        "AIQDailyFields",
        "AIQDailyHighlights",
    }
    if (
        source.get("clusterUri") != analytics.cluster_uri
        or source.get("database") != analytics.database_name
        or source.get("kind") != "manual-kusto"
        or len(parameter_queries) != 5
        or any(not query.startswith("AIQDaily") for query in parameter_queries)
        or any(view not in "\n".join(tile_queries) for view in required_views)
        or any(
            not isinstance(tile, dict)
            or tile.get("dataSourceId") != source.get("id")
            or not str(tile.get("query") or "").startswith("AIQDaily")
            for tile in tiles
        )
    ):
        raise AdxError(
            "ADX dashboard data source or query contract is invalid",
            code="invalid_dashboard_template",
        )


def resolve_dashboard_link() -> str:
    value = read_yaml(ROOT / "config" / "reporting.yaml").get("dashboard_url")
    if value != _REVIEWED_DASHBOARD_URL:
        raise AdxError(
            "The reviewed ADX dashboard link is invalid",
            code="dashboard_link_invalid",
        )
    return _REVIEWED_DASHBOARD_URL


def build_publication_payload(
    report: dict[str, Any],
    *,
    source_path: Path | None = None,
    catalogs: tuple[dict[str, Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    _validate_daily_source(report)
    agent_catalog, issue_catalog = resolve_report_catalogs(
        report,
        source_path=source_path,
        current_catalogs=catalogs,
    )
    summary = report["summary"]
    issues = report["issues"]
    agent_by_name = {
        item["name"]: item for item in agent_catalog["agents"]
    }
    current_agents, _ = load_catalogs(require_paths=False)
    current_agent_by_name = {
        item["name"]: item for item in current_agents["agents"]
    }
    owner_by_agent = {
        name: context.get("owner")
        or current_agent_by_name.get(name, {}).get("owner")
        or "Unassigned"
        for name, context in agent_by_name.items()
    }
    issue_by_id = {
        item["id"]: item for item in issue_catalog["issues"]
    }
    _validate_catalog_context(report, agent_by_name, issue_by_id)
    baseline_by_agent = {item["agent"]: item for item in report["baseline"]}
    issues_by_agent = {
        agent: [item for item in issues if item["agent"] == agent]
        for agent in baseline_by_agent
    }
    report_url = _report_url(report)
    agents = []
    baselines = []
    cards = []
    for agent in sorted(baseline_by_agent, key=str.casefold):
        baseline = baseline_by_agent[agent]
        agent_context = agent_by_name[agent]
        agent_issues = issues_by_agent[agent]
        issue_cards = [
            card
            for item in agent_issues
            for card in item["assessment"].get("card_evaluations", [])
        ]
        baseline_cards = baseline["assessment"].get("card_evaluations", [])
        fields = [
            passed
            for item in agent_issues
            for passed in item["assessment"]["fields"].values()
        ]
        agents.append(
            {
                "agent": agent,
                "owner": owner_by_agent[agent],
                "agent_type": agent_context["type"],
                "framework": agent_context["framework"],
                "agent_report_url": _agent_report_url(report, agent),
                "selected_issue_ids": [
                    item["issue_id"] for item in agent_issues
                ],
                "baseline_status": baseline["status"],
                "baseline_verdict": baseline["assessment"]["verdict"],
                "baseline_ownership": baseline["assessment"]["ownership"],
                "baseline_ownership_reason": baseline["assessment"][
                    "ownership_reason"
                ],
                "baseline_insight_count": baseline["insight_count"],
                "baseline_runtime_evidence_complete": baseline[
                    "runtime_evidence_complete"
                ],
                "issues_expected": len(agent_issues),
                "issues_correct": sum(
                    item["result"] == "PASS" for item in agent_issues
                ),
                "issues_partial": sum(
                    item["detail"] == "PARTIAL" for item in agent_issues
                ),
                "issues_failed": sum(
                    item["result"] == "FAIL" for item in agent_issues
                ),
                "issues_incomplete": sum(
                    item["result"] == "INCOMPLETE" for item in agent_issues
                ),
                "noise_cards": sum(
                    card.get("evaluation") == "noise" for card in baseline_cards
                )
                + sum(
                    card.get("finding_type") in {"NOISE", "DUPLICATE"}
                    for card in issue_cards
                ),
                "fields_passed": sum(passed is True for passed in fields),
                "fields_expected": len(fields),
            }
        )
        baseline_cards = baseline["assessment"].get("card_evaluations", [])
        baselines.append(
            {
                "agent": agent,
                "owner": owner_by_agent[agent],
                "agent_type": agent_context["type"],
                "framework": agent_context["framework"],
                "status": baseline["status"],
                "verdict": baseline["assessment"]["verdict"],
                "ownership": baseline["assessment"]["ownership"],
                "ownership_reason": baseline["assessment"]["ownership_reason"],
                "confidence": baseline["assessment"]["confidence"],
                "insight_count": baseline["insight_count"],
                "runtime_evidence_complete": baseline[
                    "runtime_evidence_complete"
                ],
                "card_count": len(baseline_cards),
                "agent_report_url": _agent_report_url(report, agent),
                "report_url": report_url,
            }
        )
        cards.extend(
            _card_metric(
                report=report,
                agent=agent,
                issue_id=None,
                issue_title="Healthy baseline",
                result="BASELINE",
                card=card,
                card_index=index,
            )
            for index, card in enumerate(baseline_cards, start=1)
        )
    issue_metrics = []
    for item in issues:
        context = issue_by_id[item["issue_id"]]
        agent_context = agent_by_name[item["agent"]]
        fields = item["assessment"]["fields"]
        card_evaluations = item["assessment"].get("card_evaluations", [])
        issue_metrics.append(
            {
                "agent": item["agent"],
                "owner": owner_by_agent[item["agent"]],
                "agent_type": agent_context["type"],
                "framework": agent_context["framework"],
                "issue_id": item["issue_id"],
                "title": context["title"],
                "category": context["category"],
                "severity": context["severity"],
                "expected_root_cause": context["root_cause"],
                "expected_fix": context["expected_fix"],
                "status": item["status"],
                "error_code": item.get("error_code"),
                "result": item["result"],
                "detail": item["detail"],
                "verdict": item["assessment"]["verdict"],
                "ownership": item["assessment"]["ownership"],
                "ownership_reason": item["assessment"]["ownership_reason"],
                "finding_type": item["assessment"]["finding_type"],
                "observed_count": item["observed_count"],
                "runtime_evidence_complete": item[
                    "runtime_evidence_complete"
                ],
                "confidence": item["assessment"]["confidence"],
                "card_count": len(card_evaluations),
                "passing_fields": _field_names(fields, expected=True),
                "failing_fields": _field_names(fields, expected=False),
                "fields_passed": sum(value is True for value in fields.values()),
                "fields_expected": len(fields),
                "issue_url": _issue_url(item["issue_id"]),
                "agent_report_url": _agent_report_url(
                    report,
                    item["agent"],
                ),
                "report_url": report_url,
            }
        )
        cards.extend(
            _card_metric(
                report=report,
                agent=item["agent"],
                issue_id=item["issue_id"],
                issue_title=context["title"],
                result=item["result"],
                card=card,
                card_index=index,
            )
            for index, card in enumerate(card_evaluations, start=1)
        )
    field_metrics = [
        {
            "agent": item["agent"],
            "issue_id": item["issue_id"],
            "issue_title": issue_by_id[item["issue_id"]]["title"],
            "result": item["result"],
            "field": field,
            "passed": passed,
        }
        for item in issues
        for field, passed in sorted(item["assessment"]["fields"].items())
    ]
    highlights = [
        {
            "section": "What is working",
            "ordinal": index,
            "title": title,
            "what_happened": description,
            "needed_behavior": "",
        }
        for index, (title, description) in enumerate(
            working_capabilities(report),
            start=1,
        )
    ] + [
        {
            "section": "What needs improvement",
            "ordinal": index,
            "title": title,
            "what_happened": what_happened,
            "needed_behavior": needed_behavior,
        }
        for index, (title, what_happened, needed_behavior) in enumerate(
            improvement_rows(report),
            start=1,
        )
    ]
    return {
        "schema_version": PAYLOAD_VERSION,
        "run": {
            "status": report["status"],
            "report_url": report_url,
            "baseline_passed": summary["baseline_passed"],
            "issues_correct": summary["issues_correct"],
            "issues_expected": summary["issues_expected"],
            "issues_partial": summary["issues_partial"],
            "quality_failures": summary["quality_failures"],
            "incomplete": summary["incomplete"],
            "incomplete_reasons": summary.get("incomplete_reasons", []),
            "noise_cards": summary["noise_cards"],
            "unverified_cards": summary["unverified_cards"],
            "observed_cards": summary["observed_cards"],
            "field_quality_score": summary["field_quality_score"],
            "clean_card_precision": summary["clean_card_precision"],
            "quality_score": summary["quality_score"],
            "quality_threshold": summary["quality_threshold"],
            "quality_score_formula": summary["quality_score_formula"],
        },
        "agents": agents,
        "baselines": baselines,
        "issues": issue_metrics,
        "cards": cards,
        "fields": field_metrics,
        "highlights": highlights,
    }


def resolve_report_catalogs(
    report: dict[str, Any],
    *,
    source_path: Path | None = None,
    current_catalogs: tuple[dict[str, Any], dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = current_catalogs or load_catalogs(require_paths=False)
    if _catalogs_match_report(report, current):
        return current
    if source_path is None:
        raise AdxError(
            "The daily report catalog snapshot is unavailable",
            code="catalog_snapshot_unavailable",
        )
    try:
        relative = source_path.resolve().relative_to(ROOT).as_posix()
    except ValueError as error:
        raise AdxError(
            "Historical report must be inside the repository",
            code="catalog_snapshot_unavailable",
        ) from error
    commits = _run_git(
        ["log", "--follow", "--format=%H", "--", relative]
    ).splitlines()
    for commit in commits:
        if re.fullmatch(r"[0-9a-f]{40}", commit, re.ASCII) is None:
            continue
        try:
            historical = _catalogs_at_commit(commit)
        except AdxError:
            continue
        if _catalogs_match_report(report, historical):
            return historical
    raise AdxError(
        "The reviewed catalog snapshot for this daily report was not found",
        code="catalog_snapshot_unavailable",
    )


def _catalogs_match_report(
    report: dict[str, Any],
    catalogs: tuple[dict[str, Any], dict[str, Any]],
) -> bool:
    agents, issues = catalogs
    hashes = report.get("catalog_hashes", {})
    return (
        isinstance(hashes, dict)
        and hashes.get("agents") == content_hash(agents)
        and hashes.get("issues") == content_hash(issues)
    )


def _catalogs_at_commit(
    commit: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    values = []
    try:
        for path in (
            "catalogs/AGENT_CATALOG.yaml",
            "catalogs/ISSUE_CATALOG.yaml",
        ):
            value = yaml.safe_load(_run_git(["show", f"{commit}:{path}"]))
            if not isinstance(value, dict):
                raise AdxError(
                    "Historical catalog snapshot is invalid",
                    code="catalog_snapshot_unavailable",
                )
            values.append(value)
    except yaml.YAMLError as error:
        raise AdxError(
            "Historical catalog snapshot is invalid",
            code="catalog_snapshot_unavailable",
        ) from error
    agents, issues = values
    try:
        validate_semantics(agents, issues, require_paths=False)
    except ContractError as error:
        raise AdxError(
            "Historical catalog snapshot is invalid",
            code="catalog_snapshot_unavailable",
        ) from error
    return agents, issues


def _run_git(arguments: list[str]) -> str:
    try:
        process = subprocess.run(
            ["git", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AdxError(
            "Historical catalog lookup failed",
            code="catalog_snapshot_unavailable",
        ) from error
    if process.returncode != 0:
        raise AdxError(
            "Historical catalog lookup failed",
            code="catalog_snapshot_unavailable",
        )
    return process.stdout


def _validate_catalog_context(
    report: dict[str, Any],
    agent_by_name: dict[str, dict[str, Any]],
    issue_by_id: dict[str, dict[str, Any]],
) -> None:
    if set(agent_by_name) != {item["agent"] for item in report["baseline"]}:
        raise AdxError(
            "Report Agents do not match the reviewed catalog snapshot",
            code="invalid_report",
        )
    for item in report["issues"]:
        context = issue_by_id.get(item["issue_id"])
        if (
            context is None
            or context["agent"] != item["agent"]
            or context["title"] != item["title"]
        ):
            raise AdxError(
                "Report issues do not match the reviewed catalog snapshot",
                code="invalid_report",
            )


def _field_names(fields: dict[str, Any], *, expected: bool) -> list[str]:
    return sorted(
        field for field, value in fields.items() if value is expected
    )


def _card_metric(
    *,
    report: dict[str, Any],
    agent: str,
    issue_id: str | None,
    issue_title: str,
    result: str,
    card: dict[str, Any],
    card_index: int,
) -> dict[str, Any]:
    fields = card.get("fields")
    if not isinstance(fields, dict):
        fields = {}
    return {
        "target_kind": "baseline" if issue_id is None else "issue",
        "target": issue_id or "v0",
        "agent": agent,
        "issue_id": issue_id or "",
        "issue_title": issue_title,
        "result": result,
        "card_index": card_index,
        "title": card["title"],
        "category": card["category"],
        "severity": card["severity"],
        "verdict": card.get("verdict", ""),
        "evaluation": card.get("evaluation", ""),
        "finding_type": card.get("finding_type", ""),
        "ownership": card["ownership"],
        "ownership_reason": card["ownership_reason"],
        "confidence": card["confidence"],
        "reasoning": card["reasoning"],
        "passing_fields": _field_names(fields, expected=True),
        "failing_fields": _field_names(fields, expected=False),
        "fields_passed": sum(value is True for value in fields.values()),
        "fields_expected": len(fields),
        "issue_url": _issue_url(issue_id) if issue_id is not None else "",
        "agent_report_url": _agent_report_url(report, agent),
        "report_url": _report_url(report),
    }


def _report_url(report: dict[str, Any]) -> str:
    date_path = str(report["report_date"]).replace("-", "/")
    return f"{_PUBLIC_REPOSITORY_URL}/reports/daily/{date_path}/report.md"


def _agent_report_url(report: dict[str, Any], agent: str) -> str:
    date_path = str(report["report_date"]).replace("-", "/")
    return (
        f"{_PUBLIC_REPOSITORY_URL}/reports/daily/{date_path}/agents/{agent}.md"
    )


def _issue_url(issue_id: str) -> str:
    return f"{_PUBLIC_REPOSITORY_URL}/ISSUE_CATALOG.md#{issue_id}"


def _validate_daily_source(report: dict[str, Any]) -> None:
    validate_report(report)
    summary = report["summary"]
    if (
        report["profile"] != "daily"
        or len(report["issues"]) != 25
        or summary["issues_expected"] != 25
        or len({item["issue_id"] for item in report["issues"]}) != 25
    ):
        raise AdxError(
            "ADX publication requires one valid 25-issue daily report",
            code="invalid_report",
        )
    baseline_agents = {item["agent"] for item in report["baseline"]}
    issue_agents = {item["agent"] for item in report["issues"]}
    if len(baseline_agents) != 5 or issue_agents != baseline_agents:
        raise AdxError(
            "Daily report Agent coverage is inconsistent",
            code="invalid_report",
        )
    if any(
        set(item["assessment"]["fields"]) != REQUIRED_FIELDS
        for item in report["issues"]
    ):
        raise AdxError(
            "Daily report field coverage is inconsistent",
            code="invalid_report",
        )
    score = summary["quality_score"]
    threshold = summary["quality_threshold"]
    valid_status = (
        report["status"] == "INCOMPLETE"
        and summary["incomplete"] is True
        and score is None
    ) or (
        report["status"] == "PASS"
        and summary["incomplete"] is False
        and score is not None
        and score >= threshold
    ) or (
        report["status"] == "FAIL"
        and summary["incomplete"] is False
        and score is not None
        and score < threshold
    )
    if not valid_status:
        raise AdxError(
            "Daily report status and score are inconsistent",
            code="invalid_report",
        )


def publication_receipt_path(run_id: str) -> Path:
    _validate_run_id(run_id)
    return runtime_root() / "adx-publications" / f"{run_id}.json"


def publish_daily_report(
    report: dict[str, Any],
    *,
    source_path: Path | None = None,
    catalogs: tuple[dict[str, Any], dict[str, Any]] | None = None,
    analytics: QualityAnalytics | None = None,
    client_factory: Callable[[str], AdxClient] = _default_client_factory,
    receipt_path: Path | None = None,
) -> dict[str, Any]:
    run_id = str(report.get("run_id") or "")
    report_date = str(report.get("report_date") or "")
    _validate_run_id(run_id)
    receipt_path = receipt_path or publication_receipt_path(run_id)
    try:
        payload = build_publication_payload(
            report,
            source_path=source_path,
            catalogs=catalogs,
        )
        source_digest = content_hash(payload)
        target = analytics or resolve_quality_analytics()
        client = client_factory(target.cluster_uri)
        try:
            existing = _existing_publications(client, target.database_name, run_id)
            if existing:
                _validate_existing(existing, source_digest)
                status = "already_published"
            else:
                client.manage(
                    target.database_name,
                    _publication_command(
                        report_date=report_date,
                        run_id=run_id,
                        source_digest=source_digest,
                        payload=payload,
                    ),
                )
                _validate_existing(
                    _existing_publications(client, target.database_name, run_id),
                    source_digest,
                )
                status = "published"
        finally:
            client.close()
        receipt = _publication_receipt(
            report_date=report_date,
            run_id=run_id,
            source_digest=source_digest,
            status=status,
        )
        atomic_json(receipt_path, receipt)
        return receipt
    except AdxError as error:
        receipt = _publication_receipt(
            report_date=report_date,
            run_id=run_id,
            source_digest=None,
            status="failed",
            error_code=error.code,
        )
        atomic_json(receipt_path, receipt)
        raise


def publish_daily_report_best_effort(
    report: dict[str, Any],
    *,
    source_path: Path | None = None,
    catalogs: tuple[dict[str, Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        return publish_daily_report(
            report,
            source_path=source_path,
            catalogs=catalogs,
        )
    except AdxError as error:
        if error.code == "invalid_report":
            raise
        return _publication_receipt(
            report_date=str(report.get("report_date") or ""),
            run_id=str(report.get("run_id") or ""),
            source_digest=None,
            status="failed",
            error_code=error.code,
        )


def read_publication_receipt(run_id: str) -> dict[str, Any]:
    path = publication_receipt_path(run_id)
    with path.open(encoding="ascii") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise AdxError(
            "ADX publication receipt is invalid",
            code="invalid_receipt",
        )
    return value


def _existing_publications(
    client: AdxClient,
    database: str,
    run_id: str,
) -> list[dict[str, Any]]:
    _validate_run_id(run_id)
    return client.query(
        database,
        f"{ADX_TABLE}\n"
        f"| where RunId == '{run_id}'\n"
        f"| where PayloadVersion == '{PAYLOAD_VERSION}'\n"
        "| project SourceDigest",
    )


def _validate_existing(
    rows: list[dict[str, Any]],
    source_digest: str,
) -> None:
    if len(rows) != 1:
        raise AdxError(
            "ADX publication verification did not find exactly one run",
            code="verification_failed",
        )
    existing = str(rows[0].get("SourceDigest") or "")
    if existing != source_digest:
        raise AdxError(
            "ADX already contains a different payload for this run",
            code="digest_conflict",
        )


def _publication_command(
    *,
    report_date: str,
    run_id: str,
    source_digest: str,
    payload: dict[str, Any],
) -> str:
    _validate_run_id(run_id)
    if _DIGEST.fullmatch(source_digest) is None:
        raise AdxError(
            "ADX source digest is invalid",
            code="invalid_report",
        )
    encoded = base64.b64encode(canonical_bytes(payload)).decode("ascii")
    tag = f"aiq-v2-run:{run_id}"
    return (
        f".set-or-append {ADX_TABLE} "
        f"with (tags='[\"{tag}\"]', ingestIfNotExists='[\"{tag}\"]') <|\n"
        f"print ReportDate=datetime({report_date}), RunId='{run_id}', "
        "PublishedAt=now(), "
        f"SourceDigest='{source_digest}', "
        f"Payload=parse_json(base64_decode_tostring('{encoded}')), "
        f"PayloadVersion='{PAYLOAD_VERSION}'"
    )


def _publication_receipt(
    *,
    report_date: str,
    run_id: str,
    source_digest: str | None,
    status: str,
    error_code: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "2.0.0",
        "payload_version": PAYLOAD_VERSION,
        "report_date": report_date,
        "run_id": run_id,
        "source_digest": source_digest,
        "status": status,
        "error_code": error_code,
    }


def _validate_run_id(run_id: str) -> None:
    if _RUN_ID.fullmatch(run_id) is None:
        raise AdxError(
            "ADX publication run identity is invalid",
            code="invalid_report",
        )
