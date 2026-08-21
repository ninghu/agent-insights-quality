from __future__ import annotations

from copy import deepcopy
import json

import pytest

from agent_insights_quality.ado import (
    AdoClient,
    AdoPolicy,
    AdoRuntimeConfig,
    automatic_bug_eligible,
    build_repro_html,
    classify_duplicate,
    plan_bug_action,
    sanitize_log,
)
from agent_insights_quality.memory import issue_fingerprint, reconcile_memory
from agent_insights_quality.contracts import ContractError
from agent_insights_quality.cli import main
from agent_insights_quality.finalizer import build_failure_report
from agent_insights_quality.planning import generate_daily_plan
from datetime import date, timedelta


SHA = "sha256:" + "a" * 64


def enabled_ado_policy() -> AdoPolicy:
    return AdoPolicy.from_config(
        {
            "schema_version": "1.0.0",
            "policy_version": "1.0.0",
            "candidate_reporting_enabled": True,
            "auto_apply_enabled": True,
        },
        environ={},
    )


def finding() -> dict:
    root = "Insight combines two independent fixes"
    surface = "collection grouping"
    target = "aiq-scn-041-guardrail-bypass"
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


@pytest.fixture(autouse=True)
def isolate_memory_catalog_semantics(monkeypatch):
    monkeypatch.setattr(
        "agent_insights_quality.memory.validate_canonical_report_semantics",
        lambda *_args, **_kwargs: None,
    )


def reconciliation_contract(day: int, *, complete: bool = True, rerun: int = 0):
    report_day = date(2026, 8, 3)
    for _ in range(day - 1):
        report_day += timedelta(days=1)
        while report_day.weekday() >= 5:
            report_day += timedelta(days=1)
    report_date = report_day.isoformat()
    plan = generate_daily_plan(date.fromisoformat(report_date), rerun=rerun)
    failure = {
        "failed_phase": "test setup",
        "last_confirmed_stage": "plan",
        "reason": "Synthetic incomplete run.",
        "affected_agents": [],
        "diagnostics_reference": SHA,
        "next_action": "Retry.",
        "completed_scenarios": [],
    }
    report = build_failure_report(
        plan,
        failure,
        generated_at=f"{report_date}T08:{rerun:02d}:00Z",
    )
    if complete:
        report["status"] = "NOT AT BAR"
        report["scorecard"]["verdict"] = "NOT AT BAR"
        report["scorecard"]["complete"] = True
        report["scorecard"]["counts"]["completed_scenarios"] = len(plan["assignments"])
        report["scorecard"]["violations"] = ["overall_recall"]
        report["failure"] = None
        for result in report["scenario_results"]:
            result["completed"] = True
            result["verdict"] = "missed"
    return plan, report


def reconcile(memory: dict, findings: list[dict], day: int, complete: bool = True):
    plan, report = reconciliation_contract(day, complete=complete)
    return reconcile_memory(
        memory,
        findings,
        plan=plan,
        report=report,
        run_id=f"run-{day}",
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


def test_omitted_rotating_scenario_does_not_advance_clean_streak() -> None:
    memory = reconcile(empty_memory(), [finding()], 1)
    plan, report = reconciliation_contract(2)
    omitted = plan["selection"]["omitted_scenario_ids"][0]
    memory["issues"][0]["affected_scenarios"] = [omitted]

    updated, changes = reconcile_memory(
        memory,
        [],
        plan=plan,
        report=report,
        run_id="run-2",
    )

    assert updated["issues"][0]["state"] == "new"
    assert updated["issues"][0]["consecutive_clean_complete_runs"] == 0
    assert updated["issues"][0]["resolution_evidence"] == []
    assert changes == []


def test_incomplete_run_does_not_create_or_change_memory() -> None:
    memory = reconcile(empty_memory(), [finding()], 1, complete=False)
    assert memory == empty_memory()


def test_trust_failure_does_not_advance_memory() -> None:
    plan, report = reconciliation_contract(1)
    report["scorecard"]["violations"] = ["structural_failure"]

    updated, changes = reconcile_memory(
        empty_memory(),
        [finding()],
        plan=plan,
        report=report,
        run_id="run-1",
    )

    assert updated == empty_memory()
    assert changes == []


def test_replayed_complete_report_does_not_advance_memory() -> None:
    memory = reconcile(empty_memory(), [finding()], 1)
    replayed = reconcile(memory, [finding()], 1)
    assert replayed == memory
    assert memory["issues"][0]["occurrence_count"] == 1
    assert len(memory["processed_runs"]) == 1


def test_published_report_with_memory_changes_replays_as_noop() -> None:
    plan, draft = reconciliation_contract(1)
    memory, changes = reconcile_memory(
        empty_memory(),
        [finding()],
        plan=plan,
        report=draft,
        run_id="run-1",
    )
    published = deepcopy(draft)
    published["memory_changes"] = changes

    replayed, replay_changes = reconcile_memory(
        memory,
        [finding()],
        plan=plan,
        report=published,
        run_id="run-1",
    )

    assert replayed == memory
    assert replay_changes == []


def test_report_and_run_ids_are_immutable() -> None:
    memory = reconcile(empty_memory(), [finding()], 1)
    plan, report = reconciliation_contract(1)
    with pytest.raises(ContractError, match="immutable"):
        reconcile_memory(
            memory,
            [],
            plan=plan,
            report=report,
            run_id="different-run",
        )


def test_out_of_order_complete_report_cannot_advance_memory() -> None:
    memory = reconcile(empty_memory(), [finding()], 3)
    with pytest.raises(ContractError, match="chronological|backward"):
        reconcile(memory, [], 2)


def test_memory_rejects_sensitive_content_in_any_record() -> None:
    value = finding()
    value["description"] = "Synthetic SSN 123-45-6789"
    with pytest.raises(ContractError, match="secret or PII"):
        reconcile(empty_memory(), [value], 1)


@pytest.mark.parametrize(
    "private_url",
    [
        "https://ai.azure.com/nextgen/r/sub,rg,,account,project/build/agents/aiq-001-weather-v1/insights",
        "https://private.internal.example.test/quality",
    ],
)
def test_memory_rejects_private_runtime_urls(private_url) -> None:
    value = finding()
    value["description"] = f"Private runtime location: {private_url}"
    with pytest.raises(ContractError, match="private runtime URL"):
        reconcile(empty_memory(), [value], 1)


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
            "https://ai.azure.com/nextgen/r/sub,rg,,account,aiq-20260821/"
            "build/agents/aiq-001-weather-v1/monitor/insights"
        ),
        "runtime_link_context": {
            "subscription": "sub",
            "resource_group": "rg",
            "account": "account",
            "project": "aiq-20260821",
        },
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


def test_auto_bug_rejects_canonical_link_for_different_agent(
    trusted_candidate,
) -> None:
    trusted_candidate["insights_url"] = (
        "https://ai.azure.com/nextgen/r/sub,rg,,account,aiq-20260821/"
        "build/agents/aiq-002-support-v1/monitor/insights"
    )
    assert not automatic_bug_eligible(
        trusted_candidate, duplicate_search_completed=True
    )


def test_auto_bug_rejects_link_for_different_runtime_project(
    trusted_candidate,
) -> None:
    trusted_candidate["runtime_link_context"]["project"] = "other-project"
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
    active_plan = plan_bug_action(
        trusted_candidate, [active], mode="dry-run", policy=enabled_ado_policy()
    )
    resolved_plan = plan_bug_action(
        trusted_candidate, [resolved], mode="dry-run", policy=enabled_ado_policy()
    )
    create_plan = plan_bug_action(
        trusted_candidate, [], mode="dry-run", policy=enabled_ado_policy()
    )
    assert active_plan["action"] == "candidate"
    assert active_plan["planned_action"] == "updated"
    assert resolved_plan["action"] == "candidate"
    assert resolved_plan["planned_action"] == "reopened"
    assert create_plan["action"] == "candidate"
    assert create_plan["planned_action"] == "created"
    assert plan_bug_action(trusted_candidate, [], mode="candidate-only")["action"] == "candidate"


def test_duplicate_prefers_active_exact_match_over_closed_exact(
    trusted_candidate,
) -> None:
    closed = {
        "id": 1,
        "fields": {"System.State": "Closed", "System.Description": SHA},
    }
    active = {
        "id": 2,
        "fields": {"System.State": "Active", "System.Description": SHA},
    }
    result = plan_bug_action(
        trusted_candidate,
        [closed, active],
        mode="apply",
        policy=enabled_ado_policy(),
    )
    assert result["action"] == "candidate"
    assert result["planned_action"] == "updated"
    assert result["matched_reference"] == result["work_item_reference"]


def test_multiple_active_exact_duplicates_fail_closed(
    trusted_candidate,
) -> None:
    matches = [
        {
            "id": item_id,
            "fields": {"System.State": "Active", "System.Description": SHA},
        }
        for item_id in (1, 2)
    ]
    result = plan_bug_action(
        trusted_candidate,
        matches,
        mode="apply",
        policy=enabled_ado_policy(),
    )
    assert result["action"] == "candidate"
    assert result["planned_action"] == "candidate"
    assert result["reason"] == "ambiguous_active_exact_matches"


def test_semantic_duplicate_tie_prefers_active_and_closed_fallback_is_stable() -> None:
    value = candidate()
    fields = {
        "System.Title": value["title"],
        "System.Tags": "AgentInsights; Quality",
        "System.Description": value["root_cause"],
    }
    closed_high = {"id": 9, "fields": fields | {"System.State": "Closed"}}
    closed_low = {"id": 3, "fields": fields | {"System.State": "Resolved"}}
    active = {"id": 7, "fields": fields | {"System.State": "Active"}}

    assert classify_duplicate(value, [closed_low, active, closed_high])["id"] == 7
    assert classify_duplicate(value, [closed_high, closed_low])["id"] == 3


def test_multiple_active_tied_semantic_duplicates_fail_closed(
    trusted_candidate,
) -> None:
    fields = {
        "System.Title": trusted_candidate["title"],
        "System.Tags": "AgentInsights; Quality",
        "System.Description": trusted_candidate["root_cause"],
        "System.State": "Active",
    }
    matches = [{"id": item_id, "fields": deepcopy(fields)} for item_id in (10, 11)]

    result = plan_bug_action(
        trusted_candidate,
        matches,
        mode="apply",
        policy=enabled_ado_policy(),
    )

    assert result["planned_action"] == "candidate"
    assert result["reason"] == "ambiguous_active_semantic_matches"


def test_ado_apply_persists_ambiguous_candidate_without_mutation(
    tmp_path,
    monkeypatch,
    trusted_candidate,
) -> None:
    candidate_path = tmp_path / "candidate.json"
    output_path = tmp_path / "result.json"
    candidate_path.write_text(json.dumps(trusted_candidate), encoding="utf-8")
    matches = [
        {
            "id": item_id,
            "fields": {"System.State": "Active", "System.Description": SHA},
        }
        for item_id in (1, 2)
    ]
    monkeypatch.setattr(AdoPolicy, "load", classmethod(lambda cls: enabled_ado_policy()))
    monkeypatch.setattr(
        AdoRuntimeConfig,
        "from_env",
        classmethod(
            lambda cls: AdoRuntimeConfig(
                "org", "project", "team", "template", "runtime-token"
            )
        ),
    )
    monkeypatch.setattr(AdoClient, "search_duplicates", lambda *_args: matches)
    monkeypatch.setattr(
        AdoClient,
        "create_bug",
        lambda *_args: pytest.fail("ambiguous candidate attempted mutation"),
    )
    monkeypatch.setattr(
        AdoClient,
        "update_bug",
        lambda *_args: pytest.fail("ambiguous candidate attempted mutation"),
    )
    monkeypatch.setattr(
        AdoClient,
        "reopen",
        lambda *_args: pytest.fail("ambiguous candidate attempted mutation"),
    )

    assert main(
        [
            "ado-apply",
            "--candidate",
            str(candidate_path),
            "--output",
            str(output_path),
        ]
    ) == 0
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["action"] == "candidate"
    assert result["reason"] == "ambiguous_active_exact_matches"


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
        AdoRuntimeConfig("org", "project", "team", "template", "runtime-token"),
        enabled_ado_policy(),
    )
    assert client.search_duplicates(candidate()) == []
    query = calls[0][2]["query"]
    assert "[System.State]" not in query
    assert "CONTAINS WORDS" in query
    assert "root" in query.casefold()


def test_duplicate_detail_fetch_batches_all_wiql_ids() -> None:
    calls = []

    class RecordingClient(AdoClient):
        def _request(self, method, route, *, body=None, content_type="application/json"):
            calls.append((method, route))
            if method == "POST":
                return {"workItems": [{"id": value} for value in range(1, 452)]}
            ids = route.split("ids=", 1)[1].split("&", 1)[0].split(",")
            return {"value": [{"id": int(value)} for value in ids]}

    client = RecordingClient(
        AdoRuntimeConfig("org", "project", "team", "template", "runtime-token"),
        enabled_ado_policy(),
    )
    results = client.search_duplicates(candidate())

    detail_calls = [route for method, route in calls if method == "GET"]
    assert len(detail_calls) == 3
    assert all(len(route.split("ids=", 1)[1].split("&", 1)[0].split(",")) <= 200 for route in detail_calls)
    assert [item["id"] for item in results] == list(range(1, 452))


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
        AdoRuntimeConfig("org", "project", "team", "template", "runtime-token"),
        enabled_ado_policy(),
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
        AdoRuntimeConfig("org", "project", "team", "template", "runtime-token"),
        enabled_ado_policy(),
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
        AdoRuntimeConfig("org", "project", "team", "template", "runtime-token"),
        enabled_ado_policy(),
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
        AdoRuntimeConfig("org", "project", "team", "template", "short-lived-token"),
        enabled_ado_policy(),
    )
    client.get_work_item(7)
    assert captured["authorization"] == "Bearer short-lived-token"
    assert captured["timeout"] == 30


def test_ado_policy_defaults_disabled_and_runtime_cannot_enable() -> None:
    configured_off = {
        "schema_version": "1.0.0",
        "policy_version": "1.0.0",
        "candidate_reporting_enabled": True,
        "auto_apply_enabled": False,
    }
    assert AdoPolicy.from_config(
        configured_off,
        environ={"AIQ_ADO_AUTO_APPLY_ENABLED": "true"},
    ).auto_apply_enabled is False

    configured_on = configured_off | {"auto_apply_enabled": True}
    assert AdoPolicy.from_config(configured_on, environ={}).auto_apply_enabled is True
    assert AdoPolicy.from_config(
        configured_on,
        environ={"AIQ_ADO_AUTO_APPLY_ENABLED": "false"},
    ).auto_apply_enabled is False
    assert AdoPolicy.from_config(
        configured_on,
        environ={"AIQ_ADO_AUTO_APPLY_ENABLED": "invalid"},
    ).auto_apply_enabled is False


@pytest.mark.parametrize(
    ("method_name", "arguments", "requested_action"),
    [
        ("create_bug", (candidate(), {"fields": {}}), "create"),
        ("comment_occurrence", (7, candidate()), "comment"),
        ("update_bug", (7, candidate()), "update"),
        (
            "reopen",
            (
                7,
                candidate(),
                {"fields": {"System.State": "New", "System.Reason": "Regression"}},
            ),
            "reopen",
        ),
    ],
)
def test_disabled_policy_makes_every_write_path_candidate_only(
    method_name,
    arguments,
    requested_action,
) -> None:
    calls = []

    class RecordingClient(AdoClient):
        def _request(self, method, route, *, body=None, content_type="application/json"):
            calls.append((method, route))
            return {"id": 7}

    client = RecordingClient(
        AdoRuntimeConfig("org", "project", "team", "template", "runtime-token")
    )
    result = getattr(client, method_name)(*arguments)

    assert calls == []
    assert result["mode"] == "candidate-only"
    assert result["action"] == "candidate"
    assert result["requested_action"] == requested_action
    assert result["applied"] is False
    assert result["reason"] == "ado_auto_apply_disabled"


def test_disabled_policy_still_allows_template_lookup_wiql_and_reads() -> None:
    calls = []

    class RecordingClient(AdoClient):
        def _request(self, method, route, *, body=None, content_type="application/json"):
            calls.append((method, route))
            if "wiql" in route:
                return {"workItems": []}
            return {}

    client = RecordingClient(
        AdoRuntimeConfig("org", "project", "team", "template", "runtime-token")
    )
    client.fetch_template()
    client.get_work_item(7)
    client.search_duplicates(candidate())

    assert [method for method, _ in calls] == ["GET", "GET", "POST"]


def test_low_level_mutating_request_is_guarded_before_http(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_insights_quality.ado.client.urlopen",
        lambda *_args, **_kwargs: pytest.fail("disabled mutation reached HTTP"),
    )
    client = AdoClient(
        AdoRuntimeConfig("org", "project", "team", "template", "runtime-token")
    )

    result = client._request(
        "PATCH",
        "_apis/wit/workitems/7?api-version=7.1",
        body=[],
        content_type="application/json-patch+json",
    )

    assert result["mode"] == "candidate-only"
    assert result["applied"] is False


def test_ado_apply_fails_closed_without_loading_private_runtime(
    tmp_path,
    monkeypatch,
) -> None:
    candidate_path = tmp_path / "candidate.json"
    output_path = tmp_path / "result.json"
    candidate_path.write_text(json.dumps(candidate()), encoding="utf-8")
    monkeypatch.setattr(
        AdoRuntimeConfig,
        "from_env",
        classmethod(lambda cls: pytest.fail("disabled apply loaded private runtime")),
    )

    assert main(
        [
            "ado-apply",
            "--candidate",
            str(candidate_path),
            "--output",
            str(output_path),
        ]
    ) == 0
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["mode"] == "candidate-only"
    assert result["action"] == "candidate"
    assert result["applied"] is False
    assert result["reason"] == "ado_auto_apply_disabled"
