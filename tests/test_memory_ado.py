from __future__ import annotations

from copy import deepcopy

from agent_insights_quality.ado import (
    AdoClient,
    AdoRuntimeConfig,
    automatic_bug_eligible,
    build_repro_html,
    plan_bug_action,
    sanitize_log,
)
from agent_insights_quality.memory import issue_fingerprint, reconcile_memory


SHA = "sha256:" + "a" * 64


def finding() -> dict:
    root = "Insight combines two independent fixes"
    surface = "collection grouping"
    target = "aiq-scn-010-duplicate"
    return {
        "fingerprint": issue_fingerprint(root, surface, target),
        "root_cause": root,
        "engine_surface": surface,
        "validation_target": target,
        "title": "Independent defects merged",
        "description": "The generated card is an umbrella across separate fixes.",
        "affected_scenarios": [target],
        "affected_domains": ["weather"],
        "affected_agent_types": ["prompt"],
        "engine_builds": ["build-1"],
        "generator_models": ["gpt-5.6-terra"],
        "judge_prompt_versions": ["primary-v1"],
        "primary_confidence": 0.99,
        "verifier_confidence": 0.98,
        "evidence_references": [SHA],
    }


def empty_memory() -> dict:
    return {"schema_version": "1.0.0", "updated_at": None, "issues": []}


def reconcile(memory: dict, findings: list[dict], day: int, complete: bool = True):
    report_date = f"2026-08-{day:02d}"
    return reconcile_memory(
        memory,
        findings,
        report_date=report_date,
        report_path=f"reports/daily/2026/08/{day:02d}/report.md",
        generated_at=f"{report_date}T08:00:00Z",
        complete=complete,
    )[0]


def test_memory_requires_three_complete_clean_runs_and_regresses() -> None:
    memory = reconcile(empty_memory(), [finding()], 1)
    assert memory["issues"][0]["state"] == "new"
    memory = reconcile(memory, [], 2)
    assert memory["issues"][0]["state"] == "improving"
    memory = reconcile(memory, [], 3)
    assert memory["issues"][0]["state"] == "improving"
    unchanged = reconcile(memory, [], 4, complete=False)
    assert unchanged["issues"][0]["consecutive_clean_complete_runs"] == 2
    memory = reconcile(unchanged, [], 5)
    assert memory["issues"][0]["state"] == "resolved"
    memory = reconcile(memory, [finding()], 6)
    assert memory["issues"][0]["state"] == "regressed"
    assert memory["issues"][0]["consecutive_clean_complete_runs"] == 0


def test_incomplete_run_does_not_create_or_change_memory() -> None:
    memory = reconcile(empty_memory(), [finding()], 1, complete=False)
    assert memory == empty_memory()


def candidate() -> dict:
    return {
        "fingerprint": SHA,
        "title": "Duplicate insight for one root cause",
        "root_cause": "One root cause generated duplicate insight cards",
        "complete_reproduction": True,
        "agent_insights_owned": True,
        "deterministic_checks_pass": True,
        "provenance_checks_pass": True,
        "retained_evidence": True,
        "duplicate_search_succeeded": True,
        "primary": {
            "role": "primary",
            "confidence": 0.99,
            "defect_fingerprint": SHA,
        },
        "verifier": {
            "role": "blinded_verifier",
            "confidence": 0.98,
            "defect_fingerprint": SHA,
        },
        "customer_impact": "<misleading> duplicate card",
        "report_date": "2026-08-21",
        "run_id": "run-1",
        "engine_build": "build-1",
        "generator_model": "gpt-5.6-terra",
        "project_label": "aiq-20260821",
        "agent": "aiq-001-weather-v1",
        "scenario_id": "aiq-scn-010-duplicate",
        "traffic_seed": 7,
        "expected": "One card",
        "actual": "Two cards",
        "field_assessment": {"title": "<bad>"},
        "reproduction_steps": ["Run <synthetic> traffic"],
        "trace_ids": ["1" * 32],
        "artifact_url": "https://artifacts.example/item",
        "insights_url": "https://insights.example/agent",
        "acceptance_criteria": "One distinct card is emitted.",
    }


def test_auto_bug_requires_both_matching_high_confidence_judges() -> None:
    value = candidate()
    assert automatic_bug_eligible(value)
    value["verifier"]["confidence"] = 0.94
    assert not automatic_bug_eligible(value)
    value = candidate()
    value["verifier"]["defect_fingerprint"] = "sha256:" + "b" * 64
    assert not automatic_bug_eligible(value)


def test_duplicate_action_updates_or_reopens_across_states() -> None:
    active = {
        "id": 7,
        "fields": {
            "System.State": "In Review",
            "System.Tags": "AgentInsights; Quality",
            "System.Description": SHA,
        },
    }
    resolved = deepcopy(active)
    resolved["fields"]["System.State"] = "Removed"
    assert plan_bug_action(candidate(), [active], mode="dry-run")["action"] == "updated"
    assert plan_bug_action(candidate(), [resolved], mode="dry-run")["action"] == "reopened"
    assert plan_bug_action(candidate(), [], mode="dry-run")["action"] == "created"
    assert plan_bug_action(candidate(), [], mode="candidate-only")["action"] == "candidate"


def test_repro_html_escapes_dynamic_content_and_sanitizes_logs() -> None:
    body = build_repro_html(candidate())
    assert "&lt;misleading&gt;" in body
    assert "<misleading>" not in body
    assert "[REDACTED_TOKEN]" in sanitize_log("Bearer " + "a" * 40)


def test_wiql_search_has_no_state_filter_and_uses_root_cause() -> None:
    calls = []

    class RecordingClient(AdoClient):
        def _request(self, method, route, *, body=None, content_type="application/json"):
            calls.append((method, route, body, content_type))
            return {"workItems": []}

    client = RecordingClient(
        AdoRuntimeConfig("org", "project", "team", "template", "runtime-token")
    )
    assert client.search_duplicates(candidate()) == []
    query = calls[0][2]["query"]
    assert "[System.State]" not in query
    assert "CONTAINS WORDS" in query
    assert "root" in query.casefold()


def test_reopen_uses_runtime_template_state_reason_and_tags() -> None:
    calls = []

    class RecordingClient(AdoClient):
        def _request(self, method, route, *, body=None, content_type="application/json"):
            calls.append((method, route, body, content_type))
            return {"id": 7}

    client = RecordingClient(
        AdoRuntimeConfig("org", "project", "team", "template", "runtime-token")
    )
    client.reopen(
        7,
        candidate(),
        {
            "fields": {
                "System.State": "Approved state",
                "System.Reason": "Approved reason",
                "System.Tags": "ApprovedTag",
            }
        },
    )
    patch = calls[0][2]
    assert any(item["value"] == "Approved state" for item in patch)
    assert any(item["value"] == "Approved reason" for item in patch)
    assert any("Regression" in item["value"] for item in patch)
