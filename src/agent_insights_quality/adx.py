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

from agent_insights_quality.azure_cli import azure_cli
from agent_insights_quality.profiles import RESOURCE_GROUP
from agent_insights_quality.reporting import REQUIRED_FIELDS, validate_report
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    atomic_json,
    canonical_bytes,
    content_hash,
    read_json,
    runtime_root,
)

ADX_DATABASE = "AgentInsightsQuality"
ADX_TABLE = "DailyQualityPublications"
_ADX_API_VERSION = "2025-02-14"
_RUN_ID = re.compile(r"aiq-[0-9]{8}(?:-r[0-9]{2,})?", re.ASCII)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
_DASHBOARD_TEMPLATE = ROOT / "dashboards" / "agent-insights-quality.template.json"


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
        process = subprocess.run(
            [azure_cli(), *arguments],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
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
    if (
        dashboard.get("title") != "Agent Insights Quality Trends"
        or not isinstance(data_sources, list)
        or len(data_sources) != 1
        or not isinstance(tiles, list)
        or len(tiles) < 8
        or not isinstance(pages, list)
        or len(pages) != 2
    ):
        raise AdxError(
            "ADX dashboard template is structurally invalid",
            code="invalid_dashboard_template",
        )
    source = data_sources[0]
    if (
        source.get("clusterUri") != analytics.cluster_uri
        or source.get("database") != analytics.database_name
        or source.get("kind") != "manual-kusto"
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


def dashboard_config_path() -> Path:
    return runtime_root() / "config" / "adx-dashboard.json"


def configure_dashboard_link(value: str) -> Path:
    link = _validate_dashboard_link(value)
    path = dashboard_config_path()
    atomic_json(
        path,
        {
            "schema_version": "1.0.0",
            "dashboard_url": link,
        },
    )
    return path


def resolve_dashboard_link() -> str:
    path = dashboard_config_path()
    if not path.is_file():
        raise AdxError(
            "The private ADX dashboard share link is not configured",
            code="dashboard_link_unavailable",
        )
    value = read_json(path)
    if set(value) != {"schema_version", "dashboard_url"} or value.get(
        "schema_version"
    ) != "1.0.0":
        raise AdxError(
            "The private ADX dashboard configuration is invalid",
            code="dashboard_link_invalid",
        )
    return _validate_dashboard_link(str(value.get("dashboard_url") or ""))


def _validate_dashboard_link(value: str) -> str:
    link = value.strip()
    try:
        parsed = urlsplit(link)
        port = parsed.port
    except ValueError as error:
        raise AdxError(
            "The ADX dashboard share link is invalid",
            code="dashboard_link_invalid",
        ) from error
    path = parsed.path.casefold()
    if (
        not link.isascii()
        or len(link) > 2048
        or parsed.scheme != "https"
        or parsed.hostname != "dataexplorer.azure.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or not path.startswith(("/dashboard/", "/dashboards/"))
        or path.rstrip("/") in {"/dashboard", "/dashboards"}
    ):
        raise AdxError(
            "The ADX dashboard share link is invalid",
            code="dashboard_link_invalid",
        )
    return link


def build_publication_payload(report: dict[str, Any]) -> dict[str, Any]:
    _validate_daily_source(report)
    summary = report["summary"]
    issues = report["issues"]
    baseline_by_agent = {item["agent"]: item for item in report["baseline"]}
    issues_by_agent = {
        agent: [item for item in issues if item["agent"] == agent]
        for agent in baseline_by_agent
    }
    agents = []
    for agent in sorted(baseline_by_agent, key=str.casefold):
        baseline = baseline_by_agent[agent]
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
                "baseline_status": baseline["status"],
                "baseline_verdict": baseline["assessment"]["verdict"],
                "baseline_ownership": baseline["assessment"]["ownership"],
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
    issue_metrics = [
        {
            "agent": item["agent"],
            "issue_id": item["issue_id"],
            "status": item["status"],
            "result": item["result"],
            "detail": item["detail"],
            "ownership": item["assessment"]["ownership"],
            "finding_type": item["assessment"]["finding_type"],
            "observed_count": item["observed_count"],
            "runtime_evidence_complete": item["runtime_evidence_complete"],
            "confidence": item["assessment"]["confidence"],
            "fields_passed": sum(
                value is True for value in item["assessment"]["fields"].values()
            ),
            "fields_expected": len(item["assessment"]["fields"]),
        }
        for item in issues
    ]
    field_metrics = [
        {
            "agent": item["agent"],
            "issue_id": item["issue_id"],
            "field": field,
            "passed": passed,
        }
        for item in issues
        for field, passed in sorted(item["assessment"]["fields"].items())
    ]
    return {
        "schema_version": "1.0.0",
        "run": {
            "status": report["status"],
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
        "issues": issue_metrics,
        "fields": field_metrics,
    }


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
    analytics: QualityAnalytics | None = None,
    client_factory: Callable[[str], AdxClient] = _default_client_factory,
    receipt_path: Path | None = None,
) -> dict[str, Any]:
    run_id = str(report.get("run_id") or "")
    report_date = str(report.get("report_date") or "")
    _validate_run_id(run_id)
    receipt_path = receipt_path or publication_receipt_path(run_id)
    try:
        payload = build_publication_payload(report)
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
) -> dict[str, Any]:
    try:
        return publish_daily_report(report)
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
    tag = f"aiq-run:{run_id}"
    return (
        f".set-or-append {ADX_TABLE} "
        f"with (tags='[\"{tag}\"]', ingestIfNotExists='[\"{tag}\"]') <|\n"
        f"print ReportDate=datetime({report_date}), RunId='{run_id}', "
        "PublishedAt=now(), "
        f"SourceDigest='{source_digest}', "
        f"Payload=parse_json(base64_decode_tostring('{encoded}'))"
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
        "schema_version": "1.0.0",
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
