from __future__ import annotations

import json
import html
import re

import pytest

from agent_insights_quality.contracts import (
    ContractError,
    ROOT,
    load_agent_manifests,
    load_data,
    load_scenario_catalog,
    validate_instance,
    validate_report_plan_binding,
)
from agent_insights_quality.cli import main
from agent_insights_quality.finalizer import (
    build_failure_report,
    build_preflight_plan,
    create_failure_send_request,
    render_failure_email_html,
    write_daily_artifacts,
)
from agent_insights_quality.reporting import (
    create_email_send_request,
    import_email_receipt,
    render_agent_report_markdown,
    render_email_html,
    render_report_markdown,
    render_trend,
    resolve_recipient,
    validate_report_consistency,
)
from agent_insights_quality.reporting.model import attach_structured_report_context
from agent_insights_quality.artifact_io import content_hash
from agent_insights_quality.planning import generate_daily_plan
from agent_insights_quality.links import RuntimeLinkContext, agent_page_url
from agent_insights_quality.privacy import require_privacy_safe
from agent_insights_quality.public_safety import require_public_artifact_safe
from datetime import date


SHA = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def isolate_report_renderer_semantics(monkeypatch):
    monkeypatch.setattr(
        "agent_insights_quality.reporting.render.validate_canonical_report_semantics",
        lambda *_args, **_kwargs: None,
    )


def scorecard(status: str = "AT BAR") -> dict:
    rates = {
        name: 1.0
        for name in (
            "high_severity_recall",
            "medium_severity_recall",
            "low_severity_recall",
            "overall_recall",
            "precision",
            "f1",
            "category_accuracy",
            "severity_accuracy",
            "title_pass_rate",
            "description_pass_rate",
            "proposed_fix_pass_rate",
            "linked_trace_pass_rate",
            "evidence_localization_rate",
            "meaningfulness_rate",
            "actionability_rate",
            "distinctness_rate",
        )
    }
    rates.update(
        {
            "healthy_noise_rate": 0.0,
            "duplication_rate": 0.0,
            "fragmentation_rate": 0.0,
            "umbrella_rate": 0.0,
            "cross_version_stale_rate": 0.0,
        }
    )
    return {
        "schema_version": "1.0.0",
        "verdict": status,
        "complete": status != "INCONCLUSIVE",
        "counts": {
            "active_scenarios": 0,
            "completed_scenarios": 0,
            "true_positives": 0,
            "partially_useful": 0,
            "false_positives": 0,
            "false_negatives": 0,
            "healthy_insights": 0,
            "structural_failures": 0,
            "new_issues": 0,
            "known_issues": 0,
            "resolved_issues": 0,
            "regressed_issues": 0,
        },
        "rates": rates,
        "violations": [] if status == "AT BAR" else ["incomplete_catalog"],
    }


def report(day: str = "2026-08-21") -> dict:
    agents = []
    for index in range(1, 6):
        agent_id = f"aiq-{index:03d}-agent"
        agents.append(
            {
                "id": agent_id,
                "name": agent_id,
                "type": "prompt",
                "version_digest": SHA,
                "insights_reference": SHA,
                "human_validation": "N/A",
            }
        )
    return {
        "schema_version": "1.0.0",
        "report_id": "aiq-" + day.replace("-", ""),
        "report_date": day,
        "generated_at": day + "T08:00:00Z",
        "plan_id": "aiq-" + day.replace("-", ""),
        "status": "AT BAR",
        "summary": "Quality met the strict daily bar.",
        "engine": {
            "build": "build-1",
            "generator_model": "gpt-5.6-terra",
            "endpoint_reference": SHA,
        },
        "scorecard": scorecard(),
        "agents": agents,
        "scenario_results": [],
        "field_judgments": [],
        "collection_analysis": {
            "distinct": 0,
            "duplicates": 0,
            "fragments": 0,
            "umbrellas": 0,
            "stale_version": 0,
        },
        "diagnostics": {
            "engine_latency_ms": 100,
            "model_calls": 1,
            "tokens": 100,
        },
        "bug_actions": [],
        "memory_changes": [],
        "artifact_reference": SHA,
        "failure": None,
        "delivery": {"state": "unsent", "request_reference": None},
    }


def field_judgment(scenario_id: str, reference: str) -> dict:
    return {
        "scenario_id": scenario_id,
        "insight_reference": reference,
        "verdict": "correct",
        "confidence": 0.99,
        "verifier_verdict": "correct",
        "verifier_confidence": 0.99,
        "novel": False,
        "fix_verifiable": True,
        "attributes": {
            name: True
            for name in (
                "root_cause",
                "title",
                "description",
                "proposed_fix",
                "category",
                "severity",
                "linked_traces",
                "meaningfulness",
                "evidence_localization",
                "actionability",
            )
        },
        "relationships": {
            "duplicate": False,
            "fragment": False,
            "umbrella": False,
        },
        "stale_version": False,
    }


def runtime_link_context(value: dict) -> RuntimeLinkContext:
    return RuntimeLinkContext(
        "sub",
        "rg",
        "account",
        value["plan_id"],
        "00000000-0000-0000-0000-000000000001",
    )


def runtime_agent_links(value: dict) -> dict[str, str]:
    return {
        agent["id"]: agent_page_url(
            runtime_link_context(value),
            agent["name"],
        )
        for agent in value["agents"]
    }


def structured_not_at_bar_report(*, full_catalog: bool = False) -> tuple[dict, dict]:
    agents = load_agent_manifests()
    catalog = load_scenario_catalog({agent["id"] for agent in agents})
    plan = generate_daily_plan(
        date(2026, 8, 17),
        agents=agents,
        catalog=catalog,
        full_catalog=full_catalog,
    )
    missed = next(
        assignment
        for assignment in plan["assignments"]
        if assignment["expected"]["finding_count"] == 1
        and assignment["expected"]["severity"] == "high"
    )
    noisy_control = next(
        assignment
        for assignment in plan["assignments"]
        if assignment["expected"]["finding_count"] == 0
    )
    results = []
    for assignment in plan["assignments"]:
        expected = assignment["expected"]["finding_count"]
        if assignment["scenario_id"] == missed["scenario_id"]:
            references = []
            verdict = "missed"
        elif assignment["scenario_id"] == noisy_control["scenario_id"]:
            references = [
                content_hash({"noise": index, "scenario": assignment["scenario_id"]})
                for index in range(2)
            ]
            verdict = "incorrect_noise"
        else:
            references = [
                content_hash({"scenario": assignment["scenario_id"], "index": index})
                for index in range(expected)
            ]
            verdict = "correct"
        current = assignment["version_sequence"][-1]
        results.append(
            {
                "scenario_id": assignment["scenario_id"],
                "agent_id": assignment["agent_id"],
                "run_id": assignment["run_id"],
                "version_sequence": {
                    "phase": current["phase"],
                    "version_digest": current["digest"],
                },
                "agent_version_digest": current["digest"],
                "completed": True,
                "expected_count": expected,
                "observed_count": len(references),
                "verdict": verdict,
                "insight_references": references,
            }
        )
    expected_findings = sum(item["expected_count"] for item in results)
    observed_findings = sum(len(item["insight_references"]) for item in results)
    true_positives = expected_findings - 1
    rates = scorecard()["rates"]
    rates.update(
        {
            "high_severity_recall": 0.8,
            "overall_recall": true_positives / expected_findings,
            "precision": true_positives / observed_findings,
            "f1": 0.9,
            "category_accuracy": 0.8,
            "duplication_rate": 0.1,
            "fragmentation_rate": 0.1,
        }
    )
    names = {
        assignment["agent_id"]: assignment["agent_name"]
        for assignment in plan["assignments"]
    }
    value = {
        "schema_version": "1.0.0",
        "report_id": plan["plan_id"],
        "report_date": plan["report_date"],
        "generated_at": "2026-08-17T08:00:00Z",
        "plan_id": plan["plan_id"],
        "status": "NOT AT BAR",
        "summary": "The complete run did not meet the enforced quality bar.",
        "engine": plan["engine"],
        "scorecard": {
            "schema_version": "1.0.0",
            "verdict": "NOT AT BAR",
            "complete": True,
            "counts": {
                "active_scenarios": len(results),
                "completed_scenarios": len(results),
                "true_positives": true_positives,
                "partially_useful": 0,
                "false_positives": 2,
                "false_negatives": 1,
                "healthy_insights": 2,
                "structural_failures": 0,
                "new_issues": 1,
                "known_issues": 0,
                "resolved_issues": 0,
                "regressed_issues": 0,
            },
            "rates": rates,
            "violations": [
                "finding_count_mismatch",
                "extra_noise",
                "missing_findings",
                "healthy_false_positive",
                "high_severity_recall",
                "precision",
                "attribute_correctness",
                "duplication",
                "fragmentation",
            ],
        },
        "agents": [
            {
                "id": agent["id"],
                "name": names[agent["id"]],
                "type": agent["agent_type"],
                "version_digest": SHA,
                "insights_reference": content_hash(
                    {"plan": plan["plan_id"], "agent": agent["id"]}
                ),
                "human_validation": "N/A",
            }
            for agent in agents
        ],
        "scenario_results": results,
        "field_judgments": [],
        "collection_analysis": {
            "distinct": max(0, observed_findings - 2),
            "duplicates": 1,
            "fragments": 1,
            "umbrellas": 0,
            "stale_version": 0,
        },
        "diagnostics": {
            "engine_latency_ms": 100,
            "model_calls": 25,
            "tokens": 2500,
        },
        "bug_actions": [],
        "memory_changes": [],
        "artifact_reference": SHA,
        "failure": None,
        "delivery": {"state": "unsent", "request_reference": None},
    }
    return attach_structured_report_context(value, plan, catalog), plan


def test_email_has_exactly_four_sections_every_agent_and_escaped_content() -> None:
    value = report()
    value["agents"][-1]["type"] = "hosted_custom_container"
    value["summary"] = "Quality met bar <without injection>."
    trend = render_trend([value])
    links = runtime_agent_links(value)
    subject, body = render_email_html(
        value, trend, links, runtime_link_context(value)
    )
    assert subject.startswith("[Agent Insights Quality] N/A")
    assert "Overall insight quality score: N/A" in body
    assert body.count("<h2 ") == 4
    assert "&lt;without injection&gt;" not in body
    assert "<without injection>" not in body
    assert all(agent["id"] in body for agent in value["agents"])
    assert "No meaningful product problems were expected or found" in body
    assert ">Grade</th>" in body
    assert ">Findings</th>" in body
    assert "Fully correct (content utility)" in body
    assert "Partially useful (content utility)" in body
    assert "Incorrect/noisy insights" in body
    assert "Exact duplicates" not in body
    assert ">Capability</th>" in body
    assert ">Evidence</th>" in body
    assert ">Product gap</th>" in body
    assert ">What happened</th>" in body
    assert ">Needed behavior</th>" in body
    assert ">Agent</th>" in body
    assert ">Report</th>" in body
    assert ">Recommended human validation</th>" in body
    assert ">Assigned to</th>" not in body
    assert ">Test agent</th>" in body
    assert ">Type</th>" in body
    assert "Open agent</a>" in body
    assert body.count("View report</a>") == 5
    assert "/monitor/insights" not in body
    assert "/insights" not in body
    assert "Version expectations" not in body
    assert "What to double-check" not in body
    assert "14-day quality trend" not in body
    assert "Trusted insight trend" not in body
    for agent_id in (
        "aiq-001-agent",
        "aiq-002-agent",
        "aiq-003-agent",
        "aiq-004-agent",
        "aiq-005-agent",
    ):
        assert f"agents/{agent_id}.md" in body
    assert "hosted_custom_container" not in body
    assert ">container</td>" in body
    copilot_row = body[body.index("<strong>GitHub Copilot</strong>") :]
    copilot_row = copilot_row[: copilot_row.index("</tr>")]
    assert ">coding</td>" in copilot_row
    assert copilot_row.count(">N/A</td>") == 2
    assert ">Yes</td>" in copilot_row
    assert body.index("<strong>GitHub Copilot</strong>") > max(
        body.index(f"<strong>{agent['name']}</strong>")
        for agent in value["agents"]
    )
    assert ">Data source</th>" not in body
    assert ">Resolved value</th>" not in body
    assert "Foundry Project:" in body
    assert "account / aiq-20260821" not in body
    assert ">aiq-20260821</a>" in body
    assert (
        "https://ai.azure.com/nextgen/r/sub,rg,,account,aiq-20260821/"
        "home?tid=00000000-0000-0000-0000-000000000001"
    ) in body
    summary = body[body.index(">Summary</h2>") : body.index(">What is working</h2>")]
    assert summary.count("<table cellpadding=") == 1
    assert "Data source: canonical report" not in summary
    assert "No product-quality gap observed" in body
    assert 'bgcolor="#f3f6fa"' in body
    assert 'bgcolor="#12304a"' in body
    assert "max-width:1160px" in body
    assert 'width="1160"' in body
    assert "<!--[if mso]>" in body
    assert 'border-left:5px solid #0078d4' in body
    assert "Agent Insights met the strict daily quality bar" in body
    assert 'bgcolor="#e8eef7"' in body
    assert 'width="100%"' in body
    assert "<style" not in body
    assert "<script" not in body
    assert "<img" not in body
    positions = [body.index(f">{title}</h2>") for title in (
        "Summary",
        "What is working",
        "What needs improvement",
        "Test agents and agent links",
    )]
    assert positions == sorted(positions)


def test_email_uses_simple_expected_observed_noise_and_miss_narrative() -> None:
    value = report()
    value["scenario_results"] = [
        {
            "scenario_id": "aiq-scn-010-test",
            "agent_id": value["agents"][0]["id"],
            "run_id": "run-01-aiq-001-agent",
            "version_sequence": {"phase": "faulted", "version_digest": SHA},
            "agent_version_digest": SHA,
            "completed": True,
            "expected_count": 4,
            "observed_count": 5,
            "verdict": "mixed",
            "insight_references": [
                "sha256:" + f"{index:064x}" for index in range(1, 6)
            ],
        }
    ]
    value["scorecard"]["counts"].update(
        {"true_positives": 3, "false_positives": 2, "false_negatives": 1}
    )
    value["status"] = "NOT AT BAR"
    value["scorecard"]["verdict"] = "NOT AT BAR"
    value["scorecard"]["violations"] = [
        "finding_count_mismatch",
        "extra_noise",
        "missing_findings",
    ]
    subject, body = render_email_html(
        value,
        render_trend([value]),
        runtime_agent_links(value),
        runtime_link_context(value),
    )

    assert "5 distinct observed cards, 3 were fully correct on customer utility and content" in body
    assert "strict quality-bar matching found 3 of 4 expected problems" in body
    assert subject.startswith("[Agent Insights Quality] 75/100")
    assert "Overall insight quality score: 75/100" in body
    assert "4 findings were expected and 5 were observed" in body
    assert "Affected test agents: aiq-001-agent." in body


def test_email_sorts_agents_and_recommends_validation_from_actual_failures() -> None:
    value = report()
    affected = value["agents"][1]
    value["agents"].reverse()
    value["scenario_results"] = [
        {
            "scenario_id": "aiq-scn-010-test",
            "agent_id": affected["id"],
            "run_id": "run-01-aiq-002-agent",
            "version_sequence": {"phase": "faulted", "version_digest": SHA},
            "agent_version_digest": SHA,
            "completed": True,
            "expected_count": 1,
            "observed_count": 0,
            "verdict": "missed",
            "insight_references": [],
        }
    ]
    value["status"] = "NOT AT BAR"
    value["scorecard"]["verdict"] = "NOT AT BAR"
    value["scorecard"]["counts"]["false_negatives"] = 1
    value["scorecard"]["rates"].update(
        {"high_severity_recall": 0.0, "overall_recall": 0.0}
    )
    value["scorecard"]["violations"] = ["missing_findings"]

    _, body = render_email_html(
        value,
        render_trend([value]),
        runtime_agent_links(value),
        runtime_link_context(value),
    )

    ordered_names = sorted(agent["name"] for agent in value["agents"])
    assert [body.index(f"<strong>{name}</strong>") for name in ordered_names] == sorted(
        body.index(f"<strong>{name}</strong>") for name in ordered_names
    )
    rows = {
        name: re.search(
            rf"<strong>{re.escape(name)}</strong>.*?</tr>",
            body,
            flags=re.DOTALL,
        ).group()
        for name in ordered_names
    }
    assert ">Yes</td>" in rows[affected["name"]]
    assert all(
        ">No</td>" in row
        for name, row in rows.items()
        if name != affected["name"]
    )


def test_email_doing_well_names_semantic_coverage_and_collection_integrity() -> None:
    value = report()
    scenario_id = "aiq-scn-010-test"
    value["scenario_results"] = [
        {
            "scenario_id": scenario_id,
            "agent_id": value["agents"][0]["id"],
            "run_id": "run-01-aiq-001-agent",
            "version_sequence": {"phase": "faulted", "version_digest": SHA},
            "agent_version_digest": SHA,
            "completed": True,
            "expected_count": 1,
            "observed_count": 1,
            "verdict": "correct",
            "insight_references": [SHA],
        }
    ]
    value["field_judgments"] = [field_judgment(scenario_id, SHA)]
    value["collection_analysis"]["distinct"] = 1
    value["scorecard"]["counts"].update(
        {"true_positives": 1, "false_positives": 0, "false_negatives": 0}
    )

    _, body = render_email_html(
        value,
        render_trend([value]),
        runtime_agent_links(value),
        runtime_link_context(value),
    )

    assert "Useful diagnostic signal" in body
    assert "1 of 1 observed cards contained useful signal" in body
    assert "1 met the strict quality bar" in body
    assert "Finding content" in body
    assert "All 1 fully correct findings passed required title" in body
    assert "Finding separation" in body
    assert "0 fragment, umbrella, or stale-version relationships" in body
    assert body.count("<h2 ") == 4

    value["field_judgments"] = []
    value["scorecard"]["counts"]["true_positives"] = 0
    value["collection_analysis"].update({"duplicates": 1, "stale_version": 1})
    _, body = render_email_html(
        value,
        render_trend([value]),
        runtime_agent_links(value),
        runtime_link_context(value),
    )
    assert "Useful diagnostic signal" not in body
    assert "Finding separation" not in body


def test_not_at_bar_email_names_relationship_violation_and_candidate_action() -> None:
    value = report()
    scenario_id = "aiq-scn-010-test"
    agent = value["agents"][0]
    value["scenario_results"] = [
        {
            "scenario_id": scenario_id,
            "agent_id": agent["id"],
            "run_id": "run-01-aiq-001-agent",
            "version_sequence": {"phase": "faulted", "version_digest": SHA},
            "agent_version_digest": SHA,
            "completed": True,
            "expected_count": 1,
            "observed_count": 1,
            "verdict": "incorrect_noise",
            "insight_references": [SHA],
        }
    ]
    judgment = field_judgment(scenario_id, SHA)
    judgment["verdict"] = "incorrect_noise"
    judgment["verifier_verdict"] = "incorrect_noise"
    judgment["relationships"]["umbrella"] = True
    value["field_judgments"] = [judgment]
    value["collection_analysis"].update({"distinct": 1, "umbrellas": 1})
    value["status"] = "NOT AT BAR"
    value["scorecard"]["verdict"] = "NOT AT BAR"
    value["scorecard"]["violations"] = ["umbrella"]
    value["scorecard"]["rates"]["umbrella_rate"] = 0.5
    value["bug_actions"] = [
        {
            "fingerprint": SHA,
            "action": "candidate",
            "work_item_reference": None,
            "policy_snapshot": {
                "policy_version": "1.0.0",
                "auto_apply_enabled": False,
            },
            "apply_receipt": None,
        }
    ]

    _, body = render_email_html(
        value,
        render_trend([value]),
        runtime_agent_links(value),
        runtime_link_context(value),
    )

    assert "Related findings were not cleanly separated" in body
    assert "Affected test agents: aiq-001-agent." in body
    assert "Analysis found 1 umbrella relationship." in body
    assert "1 bug candidate prepared; no work-item mutation was claimed." in body
    assert "bug created" not in body.casefold()


def test_content_utility_grades_ignore_lifecycle_and_collection_hygiene() -> None:
    value = report()
    first, second = value["agents"][:2]
    other_reference = "sha256:" + ("b" * 64)
    value["scenario_results"] = [
        {
            "scenario_id": "aiq-scn-010-test",
            "agent_id": first["id"],
            "run_id": "run-01-aiq-001-agent",
            "version_sequence": {"phase": "corrected", "version_digest": SHA},
            "agent_version_digest": SHA,
            "completed": True,
            "expected_count": 1,
            "observed_count": 1,
            "verdict": "incorrect_noise",
            "insight_references": [SHA],
        },
        {
            "scenario_id": "aiq-scn-011-test",
            "agent_id": second["id"],
            "run_id": "run-01-aiq-002-agent",
            "version_sequence": {"phase": "faulted", "version_digest": SHA},
            "agent_version_digest": SHA,
            "completed": True,
            "expected_count": 1,
            "observed_count": 1,
            "verdict": "correct",
            "insight_references": [other_reference],
        },
    ]
    fully_correct = field_judgment("aiq-scn-010-test", SHA)
    fully_correct["verdict"] = "incorrect_noise"
    fully_correct["verifier_verdict"] = "incorrect_noise"
    fully_correct["relationships"]["fragment"] = True
    fully_correct["stale_version"] = True
    partially_useful = field_judgment("aiq-scn-011-test", other_reference)
    partially_useful["attributes"]["category"] = False
    value["field_judgments"] = [fully_correct, partially_useful]
    value["collection_analysis"].update({"distinct": 2, "fragments": 1, "stale_version": 1})
    value["status"] = "NOT AT BAR"
    value["scorecard"]["verdict"] = "NOT AT BAR"
    value["scorecard"]["counts"].update(
        {"true_positives": 0, "false_positives": 2, "false_negatives": 2}
    )
    value["scorecard"]["rates"].update(
        {
            "category_accuracy": 0.5,
            "fragmentation_rate": 0.5,
            "cross_version_stale_rate": 0.5,
        }
    )
    value["scorecard"]["violations"] = [
        "attribute_correctness",
        "fragmentation",
        "cross_version_stale",
    ]

    markdown = render_agent_report_markdown(value, first["id"])
    _, body = render_email_html(
        value,
        render_trend([value]),
        runtime_agent_links(value),
        runtime_link_context(value),
    )

    assert "2 distinct observed cards, 1 was fully correct" in body
    assert "1 was partially useful" in body
    assert "0 were incorrect or noisy" in body
    assert "utility grades intentionally exclude lifecycle behavior and collection hygiene" in body
    assert "Related findings were not cleanly separated" in body
    assert "## Lifecycle and collection hygiene" in markdown
    assert (
        "never change the observed-card content-utility grade" in markdown
    )


def test_r19_like_email_keeps_candidate_status_with_six_metric_gaps() -> None:
    value, _ = structured_not_at_bar_report()
    value["bug_actions"] = [
        {
            "fingerprint": content_hash({"candidate": index}),
            "action": "candidate",
            "work_item_reference": None,
            "policy_snapshot": {
                "policy_version": "1.0.0",
                "auto_apply_enabled": False,
            },
            "apply_receipt": None,
        }
        for index in range(3)
    ]

    _, body = render_email_html(
        value,
        render_trend([value]),
        runtime_agent_links(value),
        runtime_link_context(value),
    )

    assert "3 bug candidates prepared; no work-item mutation was claimed." in body
    assert "2 cards came from healthy controls." in body
    assert "Healthy-control noise:" not in body
    assert "Quality gate failed" not in body
    assert "finding_count_mismatch" not in body
    assert ">Product gap</th>" in body
    assert "Expected roots lacked a strict match" in body
    assert "Incorrect and ambiguous findings" in body
    assert body.count("Affected test agents:") == 5
    assert body.count("<h2 ") == 4


def test_email_names_capability_and_extended_field_failures() -> None:
    value = report()
    scenario_id = "aiq-scn-010-test"
    agent = value["agents"][0]
    value["scenario_results"] = [
        {
            "scenario_id": scenario_id,
            "agent_id": agent["id"],
            "run_id": "run-01-aiq-001-agent",
            "version_sequence": {"phase": "faulted", "version_digest": SHA},
            "agent_version_digest": SHA,
            "completed": True,
            "expected_count": 1,
            "observed_count": 1,
            "verdict": "partially_useful",
            "insight_references": [SHA],
        }
    ]
    judgment = field_judgment(scenario_id, SHA)
    judgment["attributes"]["actionability"] = False
    judgment["fix_verifiable"] = False
    judgment["verdict"] = "partially_useful"
    judgment["verifier_verdict"] = "partially_useful"
    value["field_judgments"] = [judgment]
    agent["human_validation"] = (
        "Required: partially useful judgment; unverifiable fix."
    )
    value["status"] = "NOT AT BAR"
    value["scorecard"]["verdict"] = "NOT AT BAR"
    value["scorecard"]["violations"] = [
        "attribute_correctness",
        "capability_fix_mismatch",
    ]
    value["scorecard"]["rates"]["actionability_rate"] = 0.5

    _, body = render_email_html(
        value,
        render_trend([value]),
        runtime_agent_links(value),
        runtime_link_context(value),
    )

    assert "actionability rate passed 50.0%" in body
    assert "Proposed fixes assumed unavailable capabilities" in body
    assert "One or more proposed fixes referenced a capability" in body
    assert "Affected test agents: aiq-001-agent." in body


def test_not_at_bar_one_pager_names_bar_actuals_metrics_and_agent_versions() -> None:
    value, plan = structured_not_at_bar_report()
    assert len(plan["assignments"]) == 25

    markdown = render_report_markdown(value)
    _, body = render_email_html(
        value,
        render_trend([value]),
        runtime_agent_links(value),
        runtime_link_context(value),
    )

    assert "## Quality bar and result" in markdown
    assert "**Overall insight quality score: 95/100.**" in markdown
    assert "## Summary" in markdown
    assert "| Grade | Findings |" in markdown
    assert "## What is working" in markdown
    assert "| Capability | Evidence |" in markdown
    assert "## What needs improvement" in markdown
    assert "| Product gap | What happened | Needed behavior |" in markdown
    assert "| Gate | Result | Evidence |" not in markdown
    assert "14-day quality trend" not in markdown
    assert "Trusted insight trend" not in markdown
    assert "## Per-agent reports" in markdown
    assert "## Scenario results" not in markdown
    assert "## Source field judgments" not in markdown
    assert "## Human validation one-pager" not in markdown
    assert "Data source: canonical report" in markdown
    assert "ai.azure.com" not in markdown
    assert "account /" not in markdown
    assert (
        markdown.index("## Summary")
        < markdown.index("## What is working")
        < markdown.index("## What needs improvement")
        < markdown.index("## Per-agent reports")
    )
    assert body.count("<h2 ") == 4
    assert "Expected roots lacked a strict match" in body
    assert "Finding count did not match root causes" in body
    assert "Finding content was incomplete or inaccurate" in body
    assert "Related findings were not cleanly separated" in body
    assert "Quality gate failed" not in body
    assert "<style" not in body
    assert "<script" not in body
    assert "<img" not in body
    assert "Version expectations" not in body
    assert "What to double-check" not in body
    assert body.count("View report</a>") == 5
    ordered_names = sorted(agent["name"] for agent in value["agents"])
    assert [markdown.index(name) for name in ordered_names] == sorted(
        markdown.index(name) for name in ordered_names
    )
    for checklist in value["human_validation_checklists"]:
        assert f"agents/{checklist['agent_id']}.md" in markdown
        assert checklist["agent_id"] in body
        agent_markdown = render_agent_report_markdown(
            value,
            checklist["agent_id"],
        )
        assert "## Evidence and human-validation guidance" in agent_markdown
        assert all(
            other["id"] not in agent_markdown
            for other in value["agents"]
            if other["id"] != checklist["agent_id"]
        )
        for version in checklist["versions"]:
            assert version["phase"] in agent_markdown
            assert str(version["expected_insight_count"]) + " expected" in agent_markdown
            assert version["double_check"] in agent_markdown
            assert version["expected_scenarios"][0]["root_cause"] in agent_markdown
            assert html.escape(version["expected_scenarios"][0]["root_cause"]) not in body


def test_real_r19_copy_distinguishes_strict_matches_from_useful_signal() -> None:
    value = load_data(
        ROOT
        / "reports"
        / "daily"
        / "2026"
        / "08"
        / "21"
        / "aiq-20260821-r19"
        / "report.json"
    )
    subject, body = render_email_html(
        value,
        render_trend([value]),
        runtime_agent_links(value),
        runtime_link_context(value),
    )
    markdown = render_report_markdown(value)

    assert subject.startswith("[Agent Insights Quality] 0/100")
    assert "Overall insight quality score: 0/100" in body
    assert re.search(
        r">Overall insight quality score</td><td[^>]*>0/100</td>",
        body,
    )
    assert re.search(
        r">Incorrect/noisy insights</td><td[^>]*>16/21</td>",
        body,
    )
    assert "Exact duplicates" not in body
    assert (
        "The overall score measures strict expected-issue success only." in body
    )
    assert (
        "independent guardrail metric and do not change the 0-100 score" in body
    )
    assert "NOT AT BAR" not in body
    assert "- Canonical audit verdict: `NOT AT BAR`" in markdown
    assert "0 were fully correct on customer utility and content" in body
    assert "5 were partially useful" in body
    assert (
        "Partially useful cards remain visible as customer-useful diagnostic signal but do not "
        "count as strict true positives."
    ) in body
    assert (
        "1 matched the expected root cause but failed category or severity correctness."
        in body
    )
    working_section = body[
        body.index(">What is working</h2>") : body.index(
            ">What needs improvement</h2>"
        )
    ]
    assert "Cross-version stale finding" not in working_section
    assert "Parent-child trace correlation control" in working_section
    assert "Partial tool failure ignored" in working_section
    assert "Task evasion or no-op response" in working_section
    assert (
        "5 of 20 expected roots were true silent misses with no card" in body
    )
    assert (
        "15 expected roots had card output, but 14 had no root-cause-correct match"
        in body
    )
    assert (
        "Across 5 mapped cards with fully or partially useful content "
        "(the scorecard attribute-rate denominator)" in body
    )
    assert "## Daily assessment" in markdown
    assert (
        "Root-cause-correct cards: 1 of 21; true silent misses: 5." in markdown
    )
    for agent in value["agents"]:
        agent_markdown = render_agent_report_markdown(value, agent["id"])
        assert "## Assessment summary" in agent_markdown
        assert "## Lifecycle and collection hygiene" in agent_markdown


def test_markdown_accepts_only_count_reconciled_sanitized_card_evaluations() -> None:
    value = report()
    scenario_id = "aiq-scn-010-test"
    agent = value["agents"][0]
    value["scenario_results"] = [
        {
            "scenario_id": scenario_id,
            "agent_id": agent["id"],
            "run_id": "run-01-aiq-001-agent",
            "version_sequence": {"phase": "faulted", "version_digest": SHA},
            "agent_version_digest": SHA,
            "completed": True,
            "expected_count": 1,
            "observed_count": 1,
            "verdict": "correct",
            "insight_references": [SHA],
        }
    ]
    value["field_judgments"] = [field_judgment(scenario_id, SHA)]
    value["scorecard"]["counts"].update(
        {"true_positives": 1, "false_positives": 0, "false_negatives": 0}
    )
    sidecar = {
        "schema_version": "1.0.0",
        "report_id": value["report_id"],
        "cards": [
            {
                "agent_id": agent["id"],
                "agent_name": agent["name"] + "-v00",
                "category": "output_quality",
                "title": "Synthetic public-safe title",
                "description": "Synthetic public-safe generated-card description.",
                "evaluation": "fully_correct",
                "evaluation_result": "Fully correct - all content attributes passed.",
            }
        ],
    }

    markdown = render_agent_report_markdown(value, agent["id"], sidecar)
    assert "## Generated insight evaluation" in markdown
    assert f"### {agent['name']}-v00" in markdown
    assert (
        "| output_quality | Synthetic public-safe title | "
        "Synthetic public-safe generated-card description. | "
        "Fully correct - all content attributes passed. |"
    ) in markdown

    sidecar["cards"][0]["evaluation"] = "partially_useful"
    with pytest.raises(ContractError, match="counts contradict"):
        render_agent_report_markdown(value, agent["id"], sidecar)


def test_structured_bar_and_checklist_plan_drift_are_rejected() -> None:
    value, plan = structured_not_at_bar_report()
    value["bar_definition"]["actuals"]["high_severity_recall"] = 1.0
    with pytest.raises(ContractError, match="bar actuals"):
        validate_report_consistency(value)

    value, plan = structured_not_at_bar_report()
    value["human_validation_checklists"][0]["versions"][0][
        "expected_insight_count"
    ] += 1
    with pytest.raises(ContractError, match="drift from the immutable daily plan"):
        validate_report_plan_binding(value, plan, "report")


def test_lifecycle_checklist_counts_follow_each_planned_phase() -> None:
    value, _ = structured_not_at_bar_report(full_catalog=True)
    versions = {
        (
            expected["scenario_id"],
            version["phase"],
        ): version["expected_insight_count"]
        for checklist in value["human_validation_checklists"]
        for version in checklist["versions"]
        for expected in version["expected_scenarios"]
    }
    assert versions[("aiq-scn-058-cross-version-stale-finding", "faulted")] == 1
    assert versions[("aiq-scn-058-cross-version-stale-finding", "corrected")] == 1
    assert versions[("aiq-scn-060-fixed-issue-recurrence", "faulted")] == 1
    assert versions[("aiq-scn-060-fixed-issue-recurrence", "corrected")] == 0
    assert versions[("aiq-scn-060-fixed-issue-recurrence", "recurred")] == 1


def test_canonical_schema_versions_require_new_context_without_breaking_history() -> None:
    schema = load_data(ROOT / "schemas" / "canonical-report.schema.json")
    assert "$defs" in schema
    assert "$defs" not in schema["properties"]["agents"]["items"]
    assert "allOf" in schema
    value, _ = structured_not_at_bar_report()
    structured = dict(value)
    structured.pop("bar_definition")
    structured.pop("human_validation_checklists")
    with pytest.raises(ContractError, match="required property"):
        validate_instance(
            structured,
            ROOT / "schemas" / "canonical-report.schema.json",
            "structured report",
        )

    structured["schema_version"] = "1.0.0"
    validate_instance(
        structured,
        ROOT / "schemas" / "canonical-report.schema.json",
        "historical report",
    )


def test_schema_validation_rejects_unresolved_internal_refs(
    tmp_path,
) -> None:
    value, _ = structured_not_at_bar_report()
    schema = load_data(ROOT / "schemas" / "canonical-report.schema.json")
    schema["properties"]["bar_definition"]["$ref"] = "#/$defs/missing"
    schema_path = tmp_path / "broken.schema.json"
    schema_path.write_text(json.dumps(schema), encoding="ascii")
    with pytest.raises(Exception, match="PointerToNowhere|missing"):
        validate_instance(value, schema_path, "broken schema")


def test_report_rejects_unconfirmed_or_disabled_bug_mutation() -> None:
    value = report()
    value["status"] = "NOT AT BAR"
    value["scorecard"]["verdict"] = "NOT AT BAR"
    value["scorecard"]["violations"] = ["umbrella"]
    value["bug_actions"] = [
        {
            "fingerprint": SHA,
            "action": "created",
            "work_item_reference": SHA,
            "policy_snapshot": {
                "policy_version": "1.0.0",
                "auto_apply_enabled": False,
            },
            "apply_receipt": None,
        }
    ]
    with pytest.raises(ContractError, match="disabled ADO policy"):
        validate_report_consistency(value)

    value["bug_actions"][0]["policy_snapshot"]["auto_apply_enabled"] = True
    with pytest.raises(ContractError, match="confirmed receipt"):
        validate_report_consistency(value)

    value["bug_actions"][0]["apply_receipt"] = {
        "confirmed": True,
        "operation_reference": SHA,
        "work_item_reference": SHA,
    }
    validate_report_consistency(value)


def test_inconclusive_report_cannot_claim_confirmed_bug_mutation() -> None:
    value = report()
    value["status"] = "INCONCLUSIVE"
    value["scorecard"]["verdict"] = "INCONCLUSIVE"
    value["scorecard"]["complete"] = False
    value["scorecard"]["violations"] = ["incomplete_catalog"]
    value["failure"] = {
        "failed_phase": "judgment",
        "last_confirmed_stage": "evidence",
        "reason": "Judgments were incomplete.",
        "affected_agents": [],
        "diagnostics_reference": SHA,
        "next_action": "Retry.",
    }
    value["bug_actions"] = [
        {
            "fingerprint": SHA,
            "action": "updated",
            "work_item_reference": SHA,
            "policy_snapshot": {
                "policy_version": "1.0.0",
                "auto_apply_enabled": True,
            },
            "apply_receipt": {
                "confirmed": True,
                "operation_reference": SHA,
                "work_item_reference": SHA,
            },
        }
    ]
    with pytest.raises(ContractError, match="INCONCLUSIVE"):
        validate_report_consistency(value)


@pytest.mark.parametrize(
    ("status", "background", "foreground", "conclusion"),
    [
        (
            "AT BAR",
            "#e6f4ea",
            "#0b6a0b",
            "met the strict daily quality bar",
        ),
        (
            "NOT AT BAR",
            "#fde7e9",
            "#a4262c",
            "did not meet the strict daily quality bar",
        ),
        (
            "INCONCLUSIVE",
            "#fff4ce",
            "#8a5700",
            "No reliable judgment can be made",
        ),
    ],
)
def test_email_status_variants_have_outlook_safe_semantic_colors(
    status,
    background,
    foreground,
    conclusion,
) -> None:
    value = report()
    value["status"] = status
    value["scorecard"]["verdict"] = status
    value["scorecard"]["complete"] = status != "INCONCLUSIVE"
    value["scorecard"]["violations"] = (
        [] if status == "AT BAR" else ["incomplete_catalog"]
    )
    if status == "INCONCLUSIVE":
        value["failure"] = {
            "failed_phase": "judgment import",
            "last_confirmed_stage": "evidence",
            "reason": "Synthetic <evidence> was incomplete.",
            "affected_agents": [],
            "diagnostics_reference": SHA,
            "next_action": "Retry the bounded import.",
        }
    _, body = render_email_html(
        value,
        render_trend([value]),
        runtime_agent_links(value),
        runtime_link_context(value),
    )
    assert f'background-color:{background}' in body
    assert f"color:{foreground}" in body
    assert conclusion in body
    if status == "INCONCLUSIVE":
        assert "Treat both generated findings and missing findings as untrusted" in body
        assert "Overall insight quality score</td>" in body
        assert "Incorrect/noisy insights</td>" in body
        assert "Exact duplicates</td>" not in body
        assert "Useful diagnostic signal" not in body
        assert "No product-quality gap observed" not in body
        assert "Synthetic &lt;evidence&gt; was incomplete." in body
        assert "Synthetic <evidence> was incomplete." not in body


def test_email_validates_trend_but_omits_it_from_leadership_presentation() -> None:
    values = [report("2026-08-20"), report("2026-08-21")]
    trend = render_trend(values)
    _, body = render_email_html(
        values[-1],
        trend,
        runtime_agent_links(values[-1]),
        runtime_link_context(values[-1]),
    )
    assert "14-day quality trend" not in body
    assert "Trusted insight trend" not in body
    assert "2026-08-20" not in body
    assert body.count("2026-08-21") >= 1


def test_summary_uses_consistent_outlook_safe_typography() -> None:
    value = report()
    _, body = render_email_html(
        value,
        render_trend([value]),
        runtime_agent_links(value),
        runtime_link_context(value),
    )
    style = (
        "font-family:Segoe UI,Arial,sans-serif;font-size:14px;line-height:21px;"
    )
    summary = body[
        body.index(">Summary</h2>") : body.index(">What is working</h2>")
    ]
    assert summary.count(style) >= 12
    assert "font-size:15px" not in summary
    assert "font-size:12px" not in summary


def test_trend_is_bounded_to_fourteen_days() -> None:
    reports = []
    for day in range(1, 17):
        reports.append(report(f"2026-08-{day:02d}"))
    trend = render_trend(reports)
    assert len(trend["days"]) == 14
    assert trend["days"][0]["report_date"] == "2026-08-03"


def test_historical_trend_does_not_use_current_catalog(monkeypatch) -> None:
    def fail_current_catalog(*_args):
        raise AssertionError("historical report was checked against current catalog")

    monkeypatch.setattr(
        "agent_insights_quality.reporting.render.validate_canonical_report_semantics",
        fail_current_catalog,
    )
    assert render_trend([report()])["days"]


def test_email_requires_all_agent_links() -> None:
    value = report()
    with pytest.raises(ContractError, match="every agent"):
        render_email_html(
            value, render_trend([value]), {}, runtime_link_context(value)
        )


def test_email_rejects_current_trend_entry_that_contradicts_report() -> None:
    value = report()
    trend = render_trend([value])
    trend["days"][0]["status"] = "NOT AT BAR"
    links = runtime_agent_links(value)
    with pytest.raises(ContractError, match="trend entry contradicts"):
        render_email_html(value, trend, links, runtime_link_context(value))
    trend = render_trend([value])
    trend["days"][0]["report_path"] = "reports/daily/2026/08/20/report.md"
    with pytest.raises(ContractError, match="trend entry contradicts"):
        render_email_html(value, trend, links, runtime_link_context(value))


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda item: item.update(verdict="partially_useful"),
            "partially useful judgment; primary/verifier disagreement",
        ),
        (
            lambda item: item.update(verifier_verdict="incorrect_noise"),
            "primary/verifier disagreement",
        ),
        (lambda item: item.update(confidence=0.90), "low-confidence judgment"),
        (lambda item: item.update(novel=True), "novel finding"),
        (lambda item: item.update(fix_verifiable=False), "unverifiable fix"),
    ],
)
def test_human_validation_is_derived_from_judgments(mutation, reason) -> None:
    value = report()
    scenario_id = "aiq-scn-010-test"
    reference = SHA
    value["scenario_results"] = [
        {
            "scenario_id": scenario_id,
            "agent_id": value["agents"][0]["id"],
            "run_id": "run-01-aiq-001-agent",
            "version_sequence": {"phase": "faulted", "version_digest": SHA},
            "agent_version_digest": SHA,
            "completed": True,
            "expected_count": 1,
            "observed_count": 1,
            "verdict": "correct",
            "insight_references": [reference],
        }
    ]
    item = field_judgment(scenario_id, reference)
    mutation(item)
    value["field_judgments"] = [item]
    value["agents"][0]["human_validation"] = "N/A"
    with pytest.raises(ContractError, match="derived"):
        validate_report_consistency(value)
    value["agents"][0]["human_validation"] = f"Required: {reason}."
    validate_report_consistency(value)


def test_recipient_supports_authenticated_user_and_guards_domain() -> None:
    config = {
        "mode": "test",
        "recipient_variable": "TEST_RECIPIENT",
        "allowed_domain": "microsoft.com",
    }
    assert resolve_recipient(config, {})["mode"] == "authenticated_user"
    with pytest.raises(ContractError, match="outside"):
        resolve_recipient(config, {"TEST_RECIPIENT": "person@example.test"})
    recipient = resolve_recipient(
        config,
        {"TEST_RECIPIENT": "person@" + "microsoft.com"},
    )
    assert recipient["mode"] == "address"


def test_send_request_remains_unsent_until_matching_provider_receipt() -> None:
    value = report()
    trend = render_trend([value])
    links = runtime_agent_links(value)
    request = create_email_send_request(
        value,
        trend,
        links,
        runtime_link_context(value),
        {
            "mode": "authenticated_user",
            "address": None,
            "source": "connected_microsoft_mailbox",
        },
    )
    assert request["state"] == "unsent"
    assert request["transport_strategy"]["attempt_order"] == [
        "connected_copilot_mail",
        "microsoft_graph",
        "local_outlook_com",
    ]
    assert request["transport_strategy"]["stop_after_first_confirmed_success"]
    assert request["transport_strategy"]["logic_app_forbidden"]
    with pytest.raises(ContractError, match="confirmation"):
        import_email_receipt(
            request,
            {
                "schema_version": "1.0.0",
                "request_hash": request["request_hash"],
                "content_digest": request["content_digest"],
                "state": "sent",
                "completed_at": "2026-08-21T09:00:00Z",
                "successful_transport": "connected_copilot_mail",
                "attempts": [
                    {
                        "transport": "connected_copilot_mail",
                        "state": "sent",
                        "content_digest": request["content_digest"],
                        "host_id": None,
                        "authorization_confirmed": True,
                        "mailbox_match_verified": True,
                        "sent_items_verified": False,
                        "provider_reference": None,
                        "error": None,
                    }
                ],
                "provider_reference": None,
                "error": None,
            },
        )


def _mail_attempt(
    request,
    transport,
    state,
    *,
    host_id=None,
    authorized=False,
    mailbox_verified=False,
    sent_items_verified=False,
):
    sent = state == "sent"
    return {
        "transport": transport,
        "state": state,
        "content_digest": request["content_digest"],
        "host_id": host_id,
        "authorization_confirmed": authorized,
        "mailbox_match_verified": mailbox_verified,
        "sent_items_verified": sent_items_verified,
        "provider_reference": SHA if sent else None,
        "error": None if sent else f"{transport} {state}",
    }


def test_local_outlook_fallback_requires_order_and_sent_items_verification() -> None:
    value = report()
    request = create_email_send_request(
        value,
        render_trend([value]),
        runtime_agent_links(value),
        runtime_link_context(value),
        {
            "mode": "authenticated_user",
            "address": None,
            "source": "connected_microsoft_mailbox",
        },
    )
    attempts = [
        _mail_attempt(request, "connected_copilot_mail", "unavailable"),
        _mail_attempt(request, "microsoft_graph", "unauthorized"),
        _mail_attempt(
            request,
            "local_outlook_com",
            "sent",
            host_id="local",
            mailbox_verified=True,
            sent_items_verified=True,
        ),
    ]
    receipt = {
        "schema_version": "1.0.0",
        "request_hash": request["request_hash"],
        "content_digest": request["content_digest"],
        "state": "sent",
        "completed_at": "2026-08-21T09:00:00Z",
        "successful_transport": "local_outlook_com",
        "attempts": attempts,
        "provider_reference": SHA,
        "error": None,
    }
    assert import_email_receipt(request, receipt)["state"] == "sent"
    receipt["attempts"][-1]["sent_items_verified"] = False
    with pytest.raises(ContractError, match="Sent Items"):
        import_email_receipt(request, receipt)


def test_authenticated_user_handoff_rejects_literal_or_uncanonical_mailbox() -> None:
    value = report()
    links = runtime_agent_links(value)
    with pytest.raises(ContractError, match="connected_microsoft_mailbox"):
        create_email_send_request(
            value,
            render_trend([value]),
            links,
            runtime_link_context(value),
            {
                "mode": "authenticated_user",
                "address": "different@" + "microsoft.com",
                "source": "untrusted",
            },
        )


def test_receipt_rejects_duplicate_send_after_success() -> None:
    value = report()
    request = create_email_send_request(
        value,
        render_trend([value]),
        runtime_agent_links(value),
        runtime_link_context(value),
        {
            "mode": "authenticated_user",
            "address": None,
            "source": "connected_microsoft_mailbox",
        },
    )
    receipt = {
        "schema_version": "1.0.0",
        "request_hash": request["request_hash"],
        "content_digest": request["content_digest"],
        "state": "sent",
        "completed_at": "2026-08-21T09:00:00Z",
        "successful_transport": "microsoft_graph",
        "attempts": [
            _mail_attempt(
                request,
                "connected_copilot_mail",
                "sent",
                authorized=True,
            ),
            _mail_attempt(
                request,
                "microsoft_graph",
                "sent",
                authorized=True,
            ),
        ],
        "provider_reference": SHA,
        "error": None,
    }
    with pytest.raises(ContractError, match="stop after first"):
        import_email_receipt(request, receipt)


def test_receipt_rejects_changed_content_digest_and_unauthorized_graph() -> None:
    value = report()
    request = create_email_send_request(
        value,
        render_trend([value]),
        runtime_agent_links(value),
        runtime_link_context(value),
        {
            "mode": "authenticated_user",
            "address": None,
            "source": "connected_microsoft_mailbox",
        },
    )
    graph = _mail_attempt(request, "microsoft_graph", "sent")
    receipt = {
        "schema_version": "1.0.0",
        "request_hash": request["request_hash"],
        "content_digest": request["content_digest"],
        "state": "sent",
        "completed_at": "2026-08-21T09:00:00Z",
        "successful_transport": "microsoft_graph",
        "attempts": [
            _mail_attempt(request, "connected_copilot_mail", "unavailable"),
            graph,
        ],
        "provider_reference": SHA,
        "error": None,
    }
    with pytest.raises(ContractError, match="requires confirmed authorization"):
        import_email_receipt(request, receipt)
    receipt["attempts"][1]["authorization_confirmed"] = True
    receipt["attempts"][1]["content_digest"] = "sha256:" + ("b" * 64)
    with pytest.raises(ContractError, match="same content digest"):
        import_email_receipt(request, receipt)


def test_failure_finalizer_is_inconclusive_and_has_no_mutations() -> None:
    plan = build_preflight_plan("2026-08-21", "2026-08-21T08:00:00Z")
    failure = {
        "failed_phase": "runtime readiness",
        "last_confirmed_stage": "contracts",
        "reason": "Production orchestrator is not ready.",
        "affected_agents": [],
        "diagnostics_reference": SHA,
        "next_action": "Complete the missing reviewed components.",
        "completed_scenarios": [],
    }
    value = build_failure_report(plan, failure, generated_at="2026-08-21T08:00:00Z")
    assert value["status"] == "INCONCLUSIVE"
    assert value["memory_changes"] == []
    assert value["bug_actions"] == []
    assert value["delivery"]["state"] == "unsent"
    body = render_failure_email_html(value)
    assert body.count("<h2 ") == 4
    assert 'bgcolor="#12304a"' in body
    assert "max-width:1160px" in body
    assert ">What is working</h2>" in body
    assert ">What needs improvement</h2>" in body
    assert "Expected findings: N/A; observed findings: N/A." in body
    assert "Correct findings were supported" not in body
    assert "Healthy controls produced no insights." not in body
    assert "No quality gaps or regressions were observed." not in body
    request = create_failure_send_request(
        value,
        {
            "mode": "authenticated_user",
            "address": None,
            "source": "connected_microsoft_mailbox",
        },
    )
    assert request["state"] == "unsent"
    assert request["content_digest"].startswith("sha256:")
    assert request["transport_strategy"]["local_outlook_host_id"] == "local"


def test_failure_summary_remains_bounded_for_maximum_reason() -> None:
    plan = build_preflight_plan("2026-08-21", "2026-08-21T08:00:00Z")
    failure = {
        "failed_phase": "runtime readiness",
        "last_confirmed_stage": "contracts",
        "reason": "x" * 2000,
        "affected_agents": [],
        "diagnostics_reference": SHA,
        "next_action": "Retry.",
        "completed_scenarios": [],
    }
    value = build_failure_report(plan, failure, generated_at="2026-08-21T08:00:00Z")
    assert len(value["summary"]) <= 2000
    assert value["failure"]["reason"] == "x" * 2000


def test_report_contradiction_is_rejected() -> None:
    value = report()
    value["status"] = "NOT AT BAR"
    with pytest.raises(ContractError, match="contradicts"):
        validate_report_consistency(value)


def test_render_report_cli_rejects_sensitive_payload(tmp_path) -> None:
    value = report()
    value["summary"] = "Synthetic payment card 4111 1111 1111 1111"
    report_path = tmp_path / "report.json"
    output_path = tmp_path / "report.md"
    report_path.write_text(json.dumps(value), encoding="ascii")

    assert main(
        [
            "render-report",
            "--report",
            str(report_path),
            "--output",
            str(output_path),
        ]
    ) == 1
    assert not output_path.exists()


def test_privacy_scanner_ignores_numeric_metrics_but_rejects_card_text() -> None:
    require_privacy_safe({"seed": 4111111111111111}, "Numeric plan fields")
    with pytest.raises(ContractError, match="payment card number"):
        require_privacy_safe(
            {"summary": "4111111111111111"},
            "String report fields",
        )


@pytest.mark.parametrize(
    "private_url",
    [
        "https://ai.azure.com/nextgen/r/sub,rg,,account,project/build/agents/aiq-001-agent/insights",
        "https://internal.example.test/agent-insights",
    ],
)
def test_render_report_cli_rejects_private_runtime_urls(
    tmp_path,
    private_url,
) -> None:
    value = report()
    value["summary"] = f"Private runtime location: {private_url}"
    report_path = tmp_path / "report.json"
    output_path = tmp_path / "report.md"
    report_path.write_text(json.dumps(value), encoding="ascii")

    assert main(
        [
            "render-report",
            "--report",
            str(report_path),
            "--output",
            str(output_path),
        ]
    ) == 1
    assert not output_path.exists()


def test_public_artifact_writer_rejects_private_link(tmp_path) -> None:
    plan = build_preflight_plan("2026-08-21", "2026-08-21T08:00:00Z")
    failure = {
        "failed_phase": "runtime readiness",
        "last_confirmed_stage": "contracts",
        "reason": "Not ready.",
        "affected_agents": [],
        "diagnostics_reference": SHA,
        "next_action": "Complete readiness.",
        "completed_scenarios": [],
    }
    value = build_failure_report(plan, failure, generated_at="2026-08-21T08:00:00Z")
    value["failure"]["next_action"] = "See https://" + "dev." + "azure.com/private"
    with pytest.raises(ContractError, match="private Azure DevOps"):
        write_daily_artifacts(tmp_path, plan, value)


def test_public_artifact_writer_rejects_agent_insights_deep_link(tmp_path) -> None:
    plan = build_preflight_plan("2026-08-21", "2026-08-21T08:00:00Z")
    failure = {
        "failed_phase": "runtime readiness",
        "last_confirmed_stage": "contracts",
        "reason": "Not ready.",
        "affected_agents": [],
        "diagnostics_reference": SHA,
        "next_action": "Complete readiness.",
        "completed_scenarios": [],
    }
    value = build_failure_report(plan, failure, generated_at="2026-08-21T08:00:00Z")
    value["failure"]["next_action"] = "See https://" + "ai.azure.com/resource/deep-link"
    with pytest.raises(ContractError, match="private runtime URL"):
        write_daily_artifacts(tmp_path, plan, value)
    value["failure"]["next_action"] = "See http://" + "ai.azure.com/resource/deep-link"
    with pytest.raises(ContractError, match="private runtime URL"):
        write_daily_artifacts(tmp_path, plan, value)


@pytest.mark.parametrize(
    "private_url",
    [
        "https:%2F%2F" + "vssps.dev." + "azure.com/org",
        "https:" + "\\\\" + "dev." + "azure.com\\org\\project",
        "//" + "account.services.ai.azure.com/project",
        "https://" + "org." + "visualstudio.com./project).",
        "[https://" + "portal.azure.com./resource]",
    ],
)
def test_public_safety_rejects_canonicalized_private_urls(private_url) -> None:
    with pytest.raises(ContractError, match="private"):
        require_public_artifact_safe(f"See {private_url}", "public output")


@pytest.mark.parametrize(
    "public_url",
    [
        "https://" + "notdev.azure.com.example.test/path",
        "https://" + "visualstudio.com.example.test/path",
        "https://" + "ai.azure.com.example.test/path",
    ],
)
def test_public_safety_avoids_private_host_near_matches(public_url) -> None:
    require_public_artifact_safe(public_url, "public output")


def test_public_artifact_writer_rejects_comprehensive_pii(tmp_path) -> None:
    plan = build_preflight_plan("2026-08-21", "2026-08-21T08:00:00Z")
    failure = {
        "failed_phase": "runtime readiness",
        "last_confirmed_stage": "contracts",
        "reason": "Not ready.",
        "affected_agents": [],
        "diagnostics_reference": SHA,
        "next_action": "Complete readiness.",
        "completed_scenarios": [],
    }
    value = build_failure_report(plan, failure, generated_at="2026-08-21T08:00:00Z")
    value["failure"]["next_action"] = "Synthetic SSN 123-45-6789"
    with pytest.raises(ContractError, match="secret or PII"):
        write_daily_artifacts(tmp_path, plan, value)


def test_rerun_artifacts_and_trend_preserve_plan_identity(tmp_path) -> None:
    plan = generate_daily_plan(date(2026, 8, 21), rerun=1)
    failure = {
        "failed_phase": "runtime readiness",
        "last_confirmed_stage": "contracts",
        "reason": "Not ready.",
        "affected_agents": [],
        "diagnostics_reference": SHA,
        "next_action": "Complete readiness.",
        "completed_scenarios": [],
    }
    value = build_failure_report(plan, failure, generated_at="2026-08-21T08:00:00Z")

    target = write_daily_artifacts(tmp_path, plan, value)
    trend = render_trend([value])

    assert target == tmp_path / plan["artifact_directory"]
    assert target.name == "aiq-20260821-r01"
    assert trend["days"][0]["report_path"] == (
        "reports/daily/2026/08/21/aiq-20260821-r01/report.md"
    )


def test_email_rejects_arbitrary_or_mismatched_agent_insights_links() -> None:
    value = report()
    trend = render_trend([value])
    links = runtime_agent_links(value)
    links[value["agents"][0]["id"]] = "https://example.test/insights"
    with pytest.raises(ContractError, match="approved runtime route"):
        render_email_html(value, trend, links, runtime_link_context(value))

    links = runtime_agent_links(value)
    links[value["agents"][0]["id"]] = links[value["agents"][1]["id"]]
    with pytest.raises(ContractError, match="authorized runtime context"):
        render_email_html(value, trend, links, runtime_link_context(value))

    wrong_context = RuntimeLinkContext(
        "sub",
        "rg",
        "account",
        "other-project",
        "00000000-0000-0000-0000-000000000001",
    )
    with pytest.raises(ContractError, match="does not match the report plan"):
        render_email_html(
            value, trend, runtime_agent_links(value), wrong_context
        )
