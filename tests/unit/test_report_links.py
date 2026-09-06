from base64 import urlsafe_b64encode
from pathlib import Path
from uuid import UUID

import pytest

from agent_insights_quality.report_context import ReportContextError
from agent_insights_quality.report_links import VerifiedScoringLink, foundry_links, validate_foundry_link
from agent_insights_quality.results import PlannedUnit, UnitId
from agent_insights_quality.state import RuntimeStore


ROOT = Path(__file__).resolve().parents[2]
REVISION = "a" * 40
SUBSCRIPTION = "11111111-2222-3333-4444-555555555555"


def test_scoring_link_requires_exact_reviewed_document_at_immutable_published_revision():
    content = (ROOT / "docs" / "QUALITY_BAR.md").read_bytes()
    requested = []
    def fetch(url):
        requested.append(url)
        return content
    link = VerifiedScoringLink(ROOT, REVISION, fetch=fetch)
    assert link.href.endswith(f"/blob/{REVISION}/docs/QUALITY_BAR.md#score-and-coverage")
    assert requested == [
        f"https://raw.githubusercontent.com/ninghu/agent-insights-quality/{REVISION}/docs/QUALITY_BAR.md"
    ]
    assert VerifiedScoringLink.from_retained(ROOT, link.to_dict()) == link
    with pytest.raises(ReportContextError, match="content_mismatch"):
        VerifiedScoringLink(ROOT, REVISION, fetch=lambda _: b"100*C/(E+N+D)")
    for revision in ("main", "../main", "A" * 40, "", "a" * 39):
        with pytest.raises(ReportContextError, match="revision_invalid"):
            VerifiedScoringLink(ROOT, revision, fetch=lambda _: pytest.fail("Network requested"))


def test_unpublished_revision_does_not_manufacture_a_link():
    def missing(_):
        raise OSError("Synthetic 404")
    with pytest.raises(OSError):
        VerifiedScoringLink(ROOT, REVISION, fetch=missing)


@pytest.mark.parametrize("href", [
    "https://example.invalid/agents/synthetic", "https://ai.azure.com/api/projects/synthetic",
    "javascript:alert(1)", "https://ai.azure.com.evil.invalid/nextgen/r/synthetic",
    "https://ai.azure.com/nextgen/r/synthetic?x=1", "file:///report.md", "cid:report.md",
])
def test_only_verified_portal_route_shape_is_allowed(href):
    with pytest.raises(ReportContextError, match="foundry_link_invalid"):
        validate_foundry_link(href)


def seed_foundry(runtime, *, bad_region=False, different_object=False):
    records = runtime.run("synthetic-daily")
    plan = (PlannedUnit(UnitId("weather-agent", "v0")),
            PlannedUnit(UnitId("weather-agent", "issue-001"), "issue-001"))
    with runtime.ownership():
        records.save_completed("environment", {
            "account_name": "aiq-daily-swedencentral", "project_name": "aiq-daily-swedencentral",
            "location": "westus" if bad_region else "swedencentral", "profile": "daily",
            "project_endpoint": "https://aiq-daily-swedencentral.services.ai.azure.com/api/projects/aiq-daily-swedencentral",
            "application_insights_resource_id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/"
                "agent-insights-quality-rg/providers/Microsoft.Insights/components/synthetic-g30",
        })
        for index, unit in enumerate(plan):
            key = f"targets/{unit.unit_id.agent}/{unit.unit_id.logical_version}"
            records.save_completed(key + "/source", {
                "work_key": key + "/work-synthetic", "traffic_run_id": "synthetic-daily",
            })
            records.save_completed(key + "/work-synthetic/deployment", {
                "target_key": f"{unit.unit_id.agent}/{unit.unit_id.logical_version}",
                "agent_name": "synthetic-other" if index and different_object else "synthetic-actual-object",
                "provider_version": str(index + 5),
            })
    return plan


def test_foundry_link_uses_actual_object_and_frozen_sweden_environment(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    plan = seed_foundry(runtime)
    links, blockers = foundry_links(runtime, "synthetic-daily", plan)
    assert not blockers
    token = urlsafe_b64encode(UUID(SUBSCRIPTION).bytes).decode().rstrip("=")
    assert links["weather-agent"] == (
        f"https://ai.azure.com/nextgen/r/{token},agent-insights-quality-rg,,"
        "aiq-daily-swedencentral,aiq-daily-swedencentral/build/agents/synthetic-actual-object/build"
    )
    assert "/api/" not in links["weather-agent"]


@pytest.mark.parametrize("kwargs", [{"bad_region": True}, {"different_object": True}])
def test_missing_or_conflicting_foundry_identity_is_disclosed_not_guessed(tmp_path, kwargs):
    runtime = RuntimeStore("daily", root=tmp_path)
    plan = seed_foundry(runtime, **kwargs)
    assert foundry_links(runtime, "synthetic-daily", plan) == ({}, ("foundry_link_missing:weather-agent",))
