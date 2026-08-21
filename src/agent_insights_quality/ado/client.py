from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from agent_insights_quality.contracts import ContractError
from agent_insights_quality.judging import (
    judgments_agree_for_auto_bug,
    validate_judgment_for_bundle,
)
from agent_insights_quality.privacy import sanitize_sensitive_text
from agent_insights_quality.links import validate_agent_insights_url
from agent_insights_quality.runtime import SHA256_PATTERN, content_hash
from agent_insights_quality.scoring import score_run


_ACTIVE_STATES = {"new", "active", "in review", "committed"}
_RESOLVED_STATES = {"resolved", "done", "removed", "closed"}


@dataclass(frozen=True)
class AdoRuntimeConfig:
    organization: str
    project: str
    team: str
    template_id: str
    token: str

    @classmethod
    def from_env(cls) -> "AdoRuntimeConfig":
        names = {
            "organization": "AIQ_ADO_ORGANIZATION",
            "project": "AIQ_ADO_PROJECT",
            "team": "AIQ_ADO_TEAM",
            "template_id": "AIQ_ADO_TEMPLATE_ID",
            "token": "AIQ_ADO_ENTRA_TOKEN",
        }
        values = {key: os.environ.get(name, "") for key, name in names.items()}
        missing = [names[key] for key, value in values.items() if not value]
        if missing:
            raise ContractError(
                "ADO runtime configuration is missing protected values: "
                + ", ".join(sorted(missing))
            )
        return cls(**values)

    @property
    def base_url(self) -> str:
        if "/" in self.organization or not self.organization.strip():
            raise ContractError("ADO organization must be a simple runtime name")
        return "https://" + "dev.azure.com/" + quote(self.organization, safe="")


def sanitize_log(value: str) -> str:
    return sanitize_sensitive_text(value)


def automatic_bug_eligible(
    candidate: dict[str, Any],
    *,
    duplicate_search_completed: bool = False,
) -> bool:
    fingerprint = candidate.get("fingerprint")
    primary = candidate.get("primary", {})
    verifier = candidate.get("verifier", {})
    bundle = candidate.get("evidence_bundle")
    plan = candidate.get("daily_plan")
    evidence_bundles = candidate.get("evidence_bundles")
    primary_judgments = candidate.get("primary_judgments")
    if (
        not isinstance(bundle, dict)
        or not isinstance(plan, dict)
        or not isinstance(evidence_bundles, list)
        or not isinstance(primary_judgments, list)
    ):
        return False
    try:
        validate_judgment_for_bundle(primary, bundle)
        validate_judgment_for_bundle(verifier, bundle)
        if not any(
            item.get("bundle_hash") == bundle.get("bundle_hash")
            for item in evidence_bundles
            if isinstance(item, dict)
        ) or not any(
            item.get("output_hash") == primary.get("output_hash")
            for item in primary_judgments
            if isinstance(item, dict)
        ):
            return False
        deterministic = score_run(plan, evidence_bundles, primary_judgments)
    except (ContractError, KeyError, TypeError):
        return False

    deterministic_blockers = {
        "structural_failure",
        "provenance_failure",
        "capability_fix_mismatch",
        "secret_or_pii",
        "over_five_insights",
        "cross_version_stale",
        "unresolved_judgment",
    }
    trace_ids = {
        trace["trace_id"] for trace in bundle.get("trace_evidence", [])
    }
    assignment = next(
        (
            item
            for item in plan.get("assignments", [])
            if item.get("scenario_id") == bundle.get("scenario", {}).get("id")
        ),
        None,
    )
    reproduced = (
        assignment is not None
        and bool(candidate.get("expected"))
        and bool(candidate.get("actual"))
        and bool(candidate.get("reproduction_steps"))
        and bool(candidate.get("artifact_url"))
        and bool(candidate.get("insights_url"))
        and bool(candidate.get("trace_ids"))
        and set(candidate["trace_ids"]).issubset(trace_ids)
        and candidate.get("run_id") == bundle.get("run", {}).get("run_id")
        and candidate.get("engine_build") == bundle.get("run", {}).get("engine_build")
        and candidate.get("generator_model")
        == bundle.get("run", {}).get("generator_model")
        and candidate.get("project_label") == plan.get("project", {}).get("name")
        and candidate.get("agent") == bundle.get("agent", {}).get("name")
        and candidate.get("scenario_id") == bundle.get("scenario", {}).get("id")
        and candidate.get("traffic_seed") == assignment.get("traffic_seed")
        and candidate.get("report_date") == plan.get("report_date")
    )
    try:
        validate_agent_insights_url(str(candidate.get("insights_url", "")))
    except ContractError:
        reproduced = False
    return (
        isinstance(fingerprint, str)
        and bool(SHA256_PATTERN.fullmatch(fingerprint))
        and duplicate_search_completed
        and reproduced
        and deterministic.get("complete") is True
        and not (set(deterministic["violations"]) & deterministic_blockers)
        and judgments_agree_for_auto_bug(
            primary, verifier, defect_fingerprint=fingerprint
        )
        and primary.get("mapping") == verifier.get("mapping")
    )


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) >= 4
    }


def classify_duplicate(
    candidate: dict[str, Any],
    work_items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    fingerprint = candidate["fingerprint"]
    title_tokens = _tokens(candidate["title"])
    root_tokens = _tokens(candidate["root_cause"])
    best: tuple[int, dict[str, Any]] | None = None
    for work_item in work_items:
        fields = work_item.get("fields", {})
        searchable = " ".join(
            str(fields.get(key, ""))
            for key in (
                "System.Title",
                "System.Tags",
                "System.Description",
                "Microsoft.VSTS.TCM.ReproSteps",
            )
        )
        exact = fingerprint in searchable
        overlap = len((_tokens(searchable) & title_tokens) | (_tokens(searchable) & root_tokens))
        tagged = "agentinsights" in searchable.casefold().replace(" ", "")
        score = 1000 if exact else overlap + (2 if tagged else 0)
        if exact or (tagged and overlap >= 3):
            if best is None or score > best[0]:
                best = (score, work_item)
    return best[1] if best else None


def plan_bug_action(
    candidate: dict[str, Any],
    work_items: list[dict[str, Any]],
    *,
    mode: Literal["candidate-only", "dry-run", "apply"],
) -> dict[str, Any]:
    if mode not in {"candidate-only", "dry-run", "apply"}:
        raise ContractError("ADO mode must be candidate-only, dry-run, or apply")
    eligible = automatic_bug_eligible(
        candidate,
        duplicate_search_completed=mode in {"dry-run", "apply"},
    )
    duplicate = classify_duplicate(candidate, work_items)
    if mode == "candidate-only" or not eligible:
        action = "candidate"
    elif duplicate is None:
        action = "created"
    else:
        state = str(duplicate.get("fields", {}).get("System.State", "")).casefold()
        action = "reopened" if state in _RESOLVED_STATES else "updated"
    return {
        "fingerprint": candidate["fingerprint"],
        "eligible": eligible,
        "mode": mode,
        "action": action,
        "matched_reference": (
            content_hash({"work_item_id": duplicate.get("id")}) if duplicate else None
        ),
        "would_apply": mode == "dry-run" and action in {"created", "updated", "reopened"},
    }


def build_repro_html(candidate: dict[str, Any]) -> str:
    required = (
        "customer_impact",
        "report_date",
        "run_id",
        "engine_build",
        "generator_model",
        "project_label",
        "agent",
        "scenario_id",
        "traffic_seed",
        "expected",
        "actual",
        "field_assessment",
        "reproduction_steps",
        "trace_ids",
        "artifact_url",
        "insights_url",
        "fingerprint",
        "acceptance_criteria",
    )
    missing = [key for key in required if key not in candidate]
    if missing:
        raise ContractError("ADO candidate is missing repro fields: " + ", ".join(missing))
    for key in ("artifact_url", "insights_url"):
        parsed = urlparse(str(candidate[key]))
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ContractError(f"ADO {key} must be an HTTPS runtime link")
    if not SHA256_PATTERN.fullmatch(str(candidate["fingerprint"])):
        raise ContractError("ADO fingerprint must be a SHA-256 reference")
    if any(
        not re.fullmatch(r"[0-9a-fA-F]{32}", str(trace_id))
        for trace_id in candidate["trace_ids"]
    ):
        raise ContractError("ADO trace IDs must be 32 hexadecimal characters")

    def escaped(value: Any) -> str:
        return html.escape(sanitize_log(str(value)), quote=True)

    def identifier(value: Any) -> str:
        return html.escape(str(value), quote=True)

    steps = "".join(
        f"<li>{escaped(step)}</li>" for step in candidate["reproduction_steps"]
    )
    traces = ", ".join(identifier(value) for value in candidate["trace_ids"])
    assessment = "".join(
        f"<tr><th>{escaped(key)}</th><td>{escaped(value)}</td></tr>"
        for key, value in sorted(candidate["field_assessment"].items())
    )
    return (
        "<h2>Customer impact</h2>"
        f"<p>{escaped(candidate['customer_impact'])}</p>"
        "<h2>Occurrence</h2>"
        "<table>"
        f"<tr><th>Report date</th><td>{escaped(candidate['report_date'])}</td></tr>"
        f"<tr><th>Run</th><td>{escaped(candidate['run_id'])}</td></tr>"
        f"<tr><th>Engine</th><td>{escaped(candidate['engine_build'])}</td></tr>"
        f"<tr><th>Generator</th><td>{escaped(candidate['generator_model'])}</td></tr>"
        f"<tr><th>Project</th><td>{escaped(candidate['project_label'])}</td></tr>"
        f"<tr><th>Agent</th><td>{escaped(candidate['agent'])}</td></tr>"
        f"<tr><th>Scenario</th><td>{escaped(candidate['scenario_id'])}</td></tr>"
        f"<tr><th>Traffic seed</th><td>{escaped(candidate['traffic_seed'])}</td></tr>"
        "</table>"
        "<h2>Expected versus actual</h2>"
        f"<p><strong>Expected:</strong> {escaped(candidate['expected'])}</p>"
        f"<p><strong>Actual:</strong> {escaped(candidate['actual'])}</p>"
        "<h2>Field assessment</h2>"
        f"<table>{assessment}</table>"
        "<h2>Reproduction</h2>"
        f"<ol>{steps}</ol>"
        f"<p><strong>Trace operation IDs:</strong> {traces}</p>"
        f"<p><a href=\"{html.escape(str(candidate['artifact_url']), quote=True)}\">"
        "Retained evidence</a></p>"
        f"<p><a href=\"{html.escape(str(candidate['insights_url']), quote=True)}\">"
        "Agent Insights page</a></p>"
        f"<p><strong>Fingerprint:</strong> {identifier(candidate['fingerprint'])}</p>"
        f"<p><strong>Primary confidence:</strong> {escaped(candidate['primary']['confidence'])}; "
        f"<strong>Verifier confidence:</strong> {escaped(candidate['verifier']['confidence'])}</p>"
        "<h2>Acceptance criteria</h2>"
        f"<p>{escaped(candidate['acceptance_criteria'])}</p>"
    )


class AdoClient:
    """Minimal ADO REST client; credentials and private coordinates exist only at runtime."""

    def __init__(self, config: AdoRuntimeConfig) -> None:
        self.config = config

    def _request(
        self,
        method: str,
        route: str,
        *,
        body: Any | None = None,
        content_type: str = "application/json",
    ) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            f"{self.config.base_url}/{quote(self.config.project, safe='')}/{route}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Content-Type": content_type,
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read()
        except (HTTPError, URLError, TimeoutError) as error:
            raise ContractError(f"ADO request failed: {sanitize_log(str(error))}") from error
        return json.loads(payload) if payload else {}

    def fetch_template(self) -> dict[str, Any]:
        team = quote(self.config.team, safe="")
        template = quote(self.config.template_id, safe="")
        return self._request(
            "GET",
            f"{team}/_apis/wit/templates/{template}?api-version=7.1",
        )

    def get_work_item(self, work_item_id: int) -> dict[str, Any]:
        return self._request(
            "GET",
            f"_apis/wit/workitems/{work_item_id}?api-version=7.1",
        )

    @staticmethod
    def _union_tags(*values: str) -> str:
        tags = {
            tag.strip()
            for value in values
            for tag in value.split(";")
            if tag.strip()
        }
        return "; ".join(sorted(tags, key=str.casefold))

    def search_duplicates(self, candidate: dict[str, Any]) -> list[dict[str, Any]]:
        marker = candidate["fingerprint"].replace("'", "''")
        title_terms = " ".join(sorted(_tokens(candidate["title"])))[:120].replace("'", "''")
        root_terms = " ".join(sorted(_tokens(candidate["root_cause"])))[:120].replace("'", "''")
        wiql = {
            "query": (
                "SELECT [System.Id] FROM WorkItems "
                "WHERE [System.WorkItemType] = 'Bug' "
                "AND ([System.Tags] CONTAINS 'AgentInsights' "
                "OR [System.Tags] CONTAINS 'Quality' "
                f"OR [System.Title] CONTAINS WORDS '{title_terms}' "
                f"OR [System.Description] CONTAINS WORDS '{root_terms}' "
                f"OR [System.Description] CONTAINS '{marker}' "
                f"OR [Microsoft.VSTS.TCM.ReproSteps] CONTAINS '{marker}')"
            )
        }
        result = self._request("POST", "_apis/wit/wiql?api-version=7.1", body=wiql)
        ids = [item["id"] for item in result.get("workItems", [])]
        if not ids:
            return []
        return self._request(
            "GET",
            "_apis/wit/workitems?ids="
            + ",".join(str(value) for value in ids)
            + "&$expand=fields&api-version=7.1",
        ).get("value", [])

    def create_bug(
        self,
        candidate: dict[str, Any],
        template: dict[str, Any],
    ) -> dict[str, Any]:
        fields = dict(template.get("fields", {}))
        prefix = fields.pop("System.Title", "")
        fields["System.Tags"] = self._union_tags(
            str(fields.get("System.Tags", "")), "AgentInsights; Quality"
        )
        fields.update(
            {
                "System.Title": f"{prefix}{candidate['title']}",
                "System.Description": build_repro_html(candidate),
            }
        )
        patch = [
            {"op": "add", "path": f"/fields/{key}", "value": value}
            for key, value in fields.items()
        ]
        return self._request(
            "POST",
            "_apis/wit/workitems/$Bug?api-version=7.1",
            body=patch,
            content_type="application/json-patch+json",
        )

    def comment_occurrence(self, work_item_id: int, candidate: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"_apis/wit/workItems/{work_item_id}/comments?api-version=7.1-preview.4",
            body={"text": build_repro_html(candidate)},
        )

    def update_bug(self, work_item_id: int, candidate: dict[str, Any]) -> dict[str, Any]:
        build_repro_html(candidate)
        existing = self.get_work_item(work_item_id)
        revision = existing.get("rev")
        if not isinstance(revision, int):
            raise ContractError("ADO work item response did not include its revision")
        existing_tags = str(existing.get("fields", {}).get("System.Tags", ""))
        self._request(
            "PATCH",
            f"_apis/wit/workitems/{work_item_id}?api-version=7.1",
            body=[
                {"op": "test", "path": "/rev", "value": revision},
                {
                    "op": "add",
                    "path": "/fields/System.Tags",
                    "value": self._union_tags(
                        existing_tags, "AgentInsights; Quality"
                    ),
                }
            ],
            content_type="application/json-patch+json",
        )
        self.comment_occurrence(work_item_id, candidate)
        return {"id": work_item_id}

    def reopen(
        self,
        work_item_id: int,
        candidate: dict[str, Any],
        template: dict[str, Any],
    ) -> dict[str, Any]:
        build_repro_html(candidate)
        template_fields = template.get("fields", {})
        existing = self.get_work_item(work_item_id)
        revision = existing.get("rev")
        if not isinstance(revision, int):
            raise ContractError("ADO work item response did not include its revision")
        existing_tags = str(existing.get("fields", {}).get("System.Tags", ""))
        state = template_fields.get("System.State", "New")
        reason = template_fields.get("System.Reason")
        tags = self._union_tags(
            existing_tags,
            str(template_fields.get("System.Tags", "")),
            "AgentInsights; Quality; Regression",
        )
        patch = [
            {"op": "test", "path": "/rev", "value": revision},
            {"op": "add", "path": "/fields/System.State", "value": state},
            {"op": "add", "path": "/fields/System.Tags", "value": tags},
        ]
        if reason:
            patch.append(
                {"op": "add", "path": "/fields/System.Reason", "value": reason}
            )
        self._request(
            "PATCH",
            f"_apis/wit/workitems/{work_item_id}?api-version=7.1",
            body=patch,
            content_type="application/json-patch+json",
        )
        self.comment_occurrence(work_item_id, candidate)
        return {"id": work_item_id}
