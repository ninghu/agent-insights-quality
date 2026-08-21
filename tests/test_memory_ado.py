from __future__ import annotations

from copy import deepcopy

import pytest

from agent_insights_quality.ado import (
    AdoClient,
    AdoRuntimeConfig,
    automatic_bug_eligible,
    build_repro_html,
    plan_bug_action,
    sanitize_log,
)
from agent_insights_quality.memory import issue_fingerprint, reconcile_memory
from agent_insights_quality.contracts import ContractError


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
    return {
        "schema_version": "1.0.0",
        "updated_at": None,
        "processed_runs": [],
        "issues": [],
    }


def reconcile(memory: dict, findings: list[dict], day: int, complete: bool = True):
    report_date = f"2026-08-{day:02d}"
    return reconcile_memory(
        memory,
        findings,
        report_id=f"aiq-202608{day:02d}",
        run_id=f"run-{day}",
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


def test_replayed_complete_report_does_not_advance_memory() -> None:
    memory = reconcile(empty_memory(), [finding()], 1)
    replayed = reconcile(memory, [finding()], 1)
    assert replayed == memory
    assert memory["issues"][0]["occurrence_count"] == 1
    assert len(memory["processed_runs"]) == 1


def test_report_and_run_ids_are_immutable() -> None:
    memory = reconcile(empty_memory(), [finding()], 1)
    with pytest.raises(ContractError, match="immutable"):
        reconcile_memory(
            memory,
            [],
            report_id="aiq-20260801",
            run_id="different-run",
            report_date="2026-08-01",
            report_path="reports/daily/2026/08/01/report.md",
            generated_at="2026-08-01T09:00:00Z",
            complete=True,
        )


def candidate() -> dict:
    return {
        "fingerprint": SHA,
        "title": "Duplicate insight for one root cause",
        "root_cause": "One root cause generated duplicate insight cards",
        "daily_plan": {
            "report_date": "2026-08-21",
            "project": {"name": "aiq-20260821"},
            "assignments": [
                {
                    "scenario_id": "aiq-scn-010-duplicate",
                    "traffic_seed": 7,
                }
            ],
        },
        "evidence_bundle": {
            "bundle_id": "00000000-0000-4000-8000-000000000010",
            "bundle_hash": SHA,
            "scenario": {"id": "aiq-scn-010-duplicate"},
            "agent": {"name": "aiq-001-weather-v1"},
            "run": {
                "run_id": "run-1",
                "engine_build": "build-1",
                "generator_model": "gpt-5.6-terra",
            },
            "trace_evidence": [{"trace_id": "1" * 32}],
        },
        "evidence_bundles": [{"bundle_hash": SHA}],
        "primary": {
            "bundle_id": "00000000-0000-4000-8000-000000000010",
            "judge_role": "primary",
            "confidence": 0.99,
            "defect_fingerprint": SHA,
            "mapping": {"scenario_id": "aiq-scn-010-duplicate", "insight_id": "i1"},
            "verdict": "incorrect_noise",
            "output_hash": SHA,
        },
        "primary_judgments": [{"output_hash": SHA}],
        "verifier": {
            "bundle_id": "00000000-0000-4000-8000-000000000010",
            "judge_role": "blinded_verifier",
            "confidence": 0.98,
            "defect_fingerprint": SHA,
            "mapping": {"scenario_id": "aiq-scn-010-duplicate", "insight_id": "i1"},
            "verdict": "incorrect_noise",
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
        "insights_url": (
            "https://ai.azure.com/nextgen/r/sub,rg,,account,project/"
            "build/agents/aiq-001-weather-v1/monitor/insights"
        ),
        "acceptance_criteria": "One distinct card is emitted.",
    }


@pytest.fixture
def trusted_candidate(monkeypatch):
    monkeypatch.setattr(
        "agent_insights_quality.ado.client.validate_judgment_for_bundle",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "agent_insights_quality.ado.client.score_run",
        lambda *_args: {"complete": True, "violations": []},
    )
    return candidate()


def test_auto_bug_requires_both_matching_high_confidence_judges(
    trusted_candidate,
) -> None:
    value = trusted_candidate
    assert automatic_bug_eligible(value, duplicate_search_completed=True)
    value["verifier"]["confidence"] = 0.94
    assert not automatic_bug_eligible(value, duplicate_search_completed=True)
    value = candidate()
    value["verifier"]["defect_fingerprint"] = "sha256:" + "b" * 64
    assert not automatic_bug_eligible(value, duplicate_search_completed=True)
    value = candidate()
    value["verifier"]["verdict"] = "partially_useful"
    assert not automatic_bug_eligible(value, duplicate_search_completed=True)
    value = candidate()
    value["primary"]["verdict"] = "correct"
    value["verifier"]["verdict"] = "correct"
    assert not automatic_bug_eligible(value, duplicate_search_completed=True)


def test_auto_bug_binds_reproduction_to_validated_bundle(trusted_candidate) -> None:
    trusted_candidate["engine_build"] = "forged-build"
    assert not automatic_bug_eligible(
        trusted_candidate, duplicate_search_completed=True
    )


def test_auto_bug_rejects_noncanonical_agent_insights_link(
    trusted_candidate,
) -> None:
    trusted_candidate["insights_url"] = "https://example.test/insights"
    assert not automatic_bug_eligible(
        trusted_candidate, duplicate_search_completed=True
    )


def test_duplicate_action_updates_or_reopens_across_states(trusted_candidate) -> None:
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
    assert plan_bug_action(trusted_candidate, [active], mode="dry-run")["action"] == "updated"
    assert plan_bug_action(trusted_candidate, [resolved], mode="dry-run")["action"] == "reopened"
    assert plan_bug_action(trusted_candidate, [], mode="dry-run")["action"] == "created"
    assert plan_bug_action(trusted_candidate, [], mode="candidate-only")["action"] == "candidate"


def test_repro_html_escapes_dynamic_content_and_sanitizes_logs() -> None:
    value = candidate()
    value["trace_ids"] = ["4111111111111111abcdefabcdefabcd"]
    body = build_repro_html(value)
    assert "&lt;misleading&gt;" in body
    assert "<misleading>" not in body
    assert "[REDACTED_TOKEN]" in sanitize_log("Bearer " + "a" * 40)
    assert SHA in body
    assert value["trace_ids"][0] in body


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
            if method == "GET":
                return {
                    "id": 7,
                    "rev": 3,
                    "fields": {"System.Tags": "ExistingTag"},
                }
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
    patch = calls[1][2]
    assert any(item["value"] == "Approved state" for item in patch)
    assert any(item["value"] == "Approved reason" for item in patch)
    assert any("Regression" in str(item["value"]) for item in patch)
    assert any("ExistingTag" in str(item["value"]) for item in patch)
    assert patch[0] == {"op": "test", "path": "/rev", "value": 3}
    assert all(item["path"] != "/fields/System.Description" for item in patch)


def test_update_validates_repro_before_any_mutation() -> None:
    calls = []

    class RecordingClient(AdoClient):
        def _request(self, method, route, *, body=None, content_type="application/json"):
            calls.append((method, route))
            return {"id": 7}

    value = candidate()
    value["artifact_url"] = "not-a-runtime-url"
    client = RecordingClient(
        AdoRuntimeConfig("org", "project", "team", "template", "runtime-token")
    )
    with pytest.raises(ContractError, match="HTTPS runtime link"):
        client.update_bug(7, value)
    assert calls == []


def test_template_route_includes_project_team_shape() -> None:
    calls = []

    class RecordingClient(AdoClient):
        def _request(self, method, route, *, body=None, content_type="application/json"):
            calls.append((method, route))
            return {}

    client = RecordingClient(
        AdoRuntimeConfig("org", "project", "team", "template", "runtime-token")
    )
    client.fetch_template()
    assert calls == [
        ("GET", "team/_apis/wit/templates/template?api-version=7.1")
    ]


def test_request_uses_runtime_bearer_token(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("agent_insights_quality.ado.client.urlopen", fake_urlopen)
    client = AdoClient(
        AdoRuntimeConfig("org", "project", "team", "template", "short-lived-token")
    )
    client.get_work_item(7)
    assert captured["authorization"] == "Bearer short-lived-token"
    assert captured["timeout"] == 30
