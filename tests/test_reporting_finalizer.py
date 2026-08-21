from __future__ import annotations

import json
from copy import deepcopy

import pytest

from agent_insights_quality.contracts import ContractError
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
    render_email_html,
    render_trend,
    resolve_recipient,
    validate_report_consistency,
)
from agent_insights_quality.artifact_io import content_hash
from agent_insights_quality.planning import generate_daily_plan
from agent_insights_quality.links import RuntimeLinkContext, agent_insights_url
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
    return RuntimeLinkContext("sub", "rg", "account", value["plan_id"])


def runtime_agent_links(value: dict) -> dict[str, str]:
    return {
        agent["id"]: agent_insights_url(
            runtime_link_context(value),
            agent["name"],
            standalone_tab=False,
        )
        for agent in value["agents"]
    }


def test_email_has_exactly_four_sections_every_agent_and_escaped_content() -> None:
    value = report()
    value["summary"] = "Quality met bar <without injection>."
    trend = render_trend([value])
    links = runtime_agent_links(value)
    subject, body = render_email_html(
        value, trend, links, runtime_link_context(value)
    )
    assert subject.startswith("[Agent Insights Quality] AT BAR")
    assert body.count("<h2 ") == 4
    assert "&lt;without injection&gt;" not in body
    assert "<without injection>" not in body
    assert all(agent["id"] in body for agent in value["agents"])
    assert "Healthy controls produced no insights." in body
    assert "Expected 0 findings; observed 0." in body
    assert "No quality gaps or regressions were observed." in body
    assert 'bgcolor="#f3f6fa"' in body
    assert 'bgcolor="#12304a"' in body
    assert "max-width:760px" in body
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
        "What we are doing well",
        "Gaps and regressions",
        "Test agents and Agent Insights links",
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
    _, body = render_email_html(
        value,
        render_trend([value]),
        runtime_agent_links(value),
        runtime_link_context(value),
    )

    assert "Expected 4 findings; observed 5." in body
    assert "Extra findings were noise." in body
    assert "Missing findings were missed issues." in body


def test_not_at_bar_email_names_relationship_violation_and_candidate_action() -> None:
    value = report()
    value["status"] = "NOT AT BAR"
    value["scorecard"]["verdict"] = "NOT AT BAR"
    value["scorecard"]["violations"] = ["umbrella"]
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

    assert "Umbrella insight relationship gate failed." in body
    assert "1 bug candidate prepared; no work-item mutation was claimed." in body
    assert "bug created" not in body.casefold()


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
            "No quality conclusion can be made",
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
            "reason": "Synthetic evidence was incomplete.",
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
        assert "Expected findings: N/A; observed findings: N/A." in body
        assert "Correct findings were supported" not in body
        assert "Healthy controls produced no insights." not in body
        assert "No quality gaps or regressions were observed." not in body


def test_email_trend_is_a_bordered_four_column_outlook_table() -> None:
    values = [report("2026-08-20"), report("2026-08-21")]
    trend = render_trend(values)
    _, body = render_email_html(
        values[-1],
        trend,
        runtime_agent_links(values[-1]),
        runtime_link_context(values[-1]),
    )
    assert ">Trusted insight trend</th>" in body
    assert ">Rate</th>" in body
    assert body.count("2026-08-20") == 1
    assert body.count("2026-08-21") >= 2
    assert 'border:1px solid #d6deea' in body
    assert 'bgcolor="#107c10"' in body


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
    value["summary"] = "See https://" + "dev." + "azure.com/private"
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
    value["summary"] = "See https://" + "ai.azure.com/resource/deep-link"
    with pytest.raises(ContractError, match="private runtime URL"):
        write_daily_artifacts(tmp_path, plan, value)
    value["summary"] = "See http://" + "ai.azure.com/resource/deep-link"
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
    value["summary"] = "Synthetic SSN 123-45-6789"
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

    wrong_context = RuntimeLinkContext("sub", "rg", "account", "other-project")
    with pytest.raises(ContractError, match="does not match the report plan"):
        render_email_html(
            value, trend, runtime_agent_links(value), wrong_context
        )
