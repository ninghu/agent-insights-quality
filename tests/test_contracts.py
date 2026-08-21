from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping

import pytest

from agent_insights_quality.contracts import (
    AGENT_SCHEMA,
    ContractError,
    EXPECTED_AGENTS,
    ROOT,
    load_agent_manifests,
    load_data,
    load_scenario_catalog,
    validate_canonical_report_semantics,
    validate_contracts,
    validate_instance,
    validate_historical_report_semantics,
    validate_report_plan_binding,
    validate_reporting_config,
)
from agent_insights_quality.docs import generate_documents
from agent_insights_quality.public_safety import validate_public_repository_content
from agent_insights_quality.security import validate_no_direct_trace_injection


def test_repository_contracts_are_valid() -> None:
    validate_contracts()
    validate_no_direct_trace_injection()
    validate_public_repository_content()


def test_generated_documents_are_current() -> None:
    generate_documents(check=True)


def test_schema_ids_use_public_owner_namespace() -> None:
    forbidden = "microsoft." + "github.io/agent-insights-quality"
    for path in (ROOT / "schemas").glob("*.schema.json"):
        schema = load_data(path)
        assert schema["$id"] == (
            "https://ninghu.github.io/agent-insights-quality/schemas/v1/"
            f"{path.name}"
        )
        assert forbidden not in path.read_text(encoding="ascii")


def test_initial_agent_registry_is_exact_and_stable() -> None:
    manifests = load_agent_manifests()
    assert {item["id"]: item["agent_type"] for item in manifests} == EXPECTED_AGENTS
    assert all(item["id"] == item["required_name_prefix"] for item in manifests)


def test_agent_schema_rejects_unexpected_properties() -> None:
    manifest = deepcopy(load_agent_manifests()[0])
    manifest["unsafe_extension"] = True
    with pytest.raises(ContractError, match="Additional properties"):
        validate_instance(manifest, AGENT_SCHEMA, "agent")


def test_scenario_catalog_has_reviewed_contract_semantics() -> None:
    catalog = load_scenario_catalog(set(EXPECTED_AGENTS))
    assert catalog["scenarios"]
    for scenario in catalog["scenarios"]:
        assert scenario["compatibility"]["agent_types"]
        assert scenario["traffic"]["seed_namespace"]
        assert scenario["evidence"]["minimum_evidence_count"] > 0
        assert scenario["expected"]["fix"]["boundary"]
        assert scenario["version_semantics"]["prior_insight_behavior"]


def test_reporting_promotion_cannot_be_automated() -> None:
    reporting = load_data(ROOT / "config" / "reporting.yaml")
    assert reporting["mode"] == "test"
    assert reporting["automation_can_promote"] is False
    assert reporting["promotion_requires_human_review"] is True
    invalid = deepcopy(reporting)
    invalid["mode"] = "production"
    with pytest.raises(ContractError, match="recipient variable must match"):
        validate_reporting_config(invalid)


def test_persisted_contracts_use_opaque_runtime_references() -> None:
    forbidden_keys = {
        "endpoint",
        "resource_id",
        "artifact_uri",
        "insights_uri",
        "evidence_uris",
        "report_uri",
        "insight_ids",
        "work_item_id",
    }

    def walk(value: object) -> None:
        if isinstance(value, Mapping):
            assert forbidden_keys.isdisjoint(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for path in (ROOT / "schemas").glob("*.schema.json"):
        walk(load_data(path))


def _scorecard(*, complete: bool, completed: int) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "verdict": "AT BAR",
        "complete": complete,
        "counts": {
            "active_scenarios": 1,
            "completed_scenarios": completed,
            "true_positives": 1,
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
        "rates": {
            "high_severity_recall": 1.0,
            "overall_recall": 1.0,
            "precision": 1.0,
            "f1": 1.0,
            "category_accuracy": 1.0,
            "severity_accuracy": 1.0,
            "title_pass_rate": 1.0,
            "description_pass_rate": 1.0,
            "proposed_fix_pass_rate": 1.0,
            "linked_trace_pass_rate": 1.0,
            "duplication_rate": 0.0,
            "umbrella_rate": 0.0,
            "cross_version_stale_rate": 0.0,
        },
        "violations": [],
    }


def _report_fixture(*, completed: bool) -> tuple[dict[str, object], list[dict], dict]:
    agents = load_agent_manifests()
    catalog = deepcopy(load_scenario_catalog(set(EXPECTED_AGENTS)))
    scenario = next(
        item for item in catalog["scenarios"] if item["id"] == "aiq-scn-017-silent-tool-error"
    )
    for item in catalog["scenarios"]:
        item["status"] = "active" if item is scenario else "retired"
    digest = "sha256:" + ("a" * 64)
    reference = "sha256:" + ("b" * 64)
    report = {
        "status": "AT BAR",
        "agents": [
            {
                "id": agent["id"],
                "name": f"{agent['id']}-v1",
                "type": agent["agent_type"],
                "version_digest": digest,
                "insights_reference": reference,
                "human_validation": "N/A",
            }
            for agent in agents
        ],
        "scenario_results": [
            {
                "scenario_id": scenario["id"],
                "agent_id": "aiq-001-weather",
                "agent_version_digest": digest,
                "completed": completed,
                "verdict": "correct",
                "insight_references": [reference],
            }
        ],
        "scorecard": _scorecard(complete=True, completed=int(completed)),
    }
    return report, agents, catalog


def test_at_bar_rejects_incomplete_scenario_results() -> None:
    report, agents, catalog = _report_fixture(completed=False)
    with pytest.raises(ContractError, match="completeness"):
        validate_canonical_report_semantics(report, agents, catalog, "report")


def test_report_must_match_exact_plan_assignment() -> None:
    report, _, catalog = _report_fixture(completed=True)
    scenario_id = report["scenario_results"][0]["scenario_id"]
    report.update(
        {
            "report_id": "aiq-20260820",
            "plan_id": "aiq-20260820",
            "report_date": "2026-08-20",
            "engine": {
                "build": "public-build",
                "generator_model": "gpt-5.6-terra",
                "endpoint_reference": "sha256:" + ("c" * 64),
            },
        }
    )
    plan = {
        "plan_id": "aiq-20260820",
        "report_date": "2026-08-20",
        "engine": deepcopy(report["engine"]),
        "assignments": [
            {
                "scenario_id": scenario_id,
                "agent_id": "aiq-004-travel",
                "agent_version_digest": "sha256:" + ("a" * 64),
            }
        ],
    }
    with pytest.raises(ContractError, match="agent differs"):
        validate_report_plan_binding(report, plan, "report")


def test_historical_report_uses_plan_snapshot_semantics() -> None:
    report, _, catalog = _report_fixture(completed=True)
    report["agents"] = [
        agent for agent in report["agents"] if agent["id"] == "aiq-001-weather"
    ]
    scenario = next(
        item for item in catalog["scenarios"] if item["status"] == "active"
    )
    plan = {
        "assignments": [
            {
                "scenario_id": scenario["id"],
                "agent_id": "aiq-001-weather",
                "agent_type": "prompt",
                "expected": {
                    "category": scenario["expected"]["category"],
                    "severity": scenario["expected"]["severity"],
                    "finding_count": 1,
                    "validation_targets": scenario["expected"]["validation_targets"],
                },
            }
        ]
    }
    validate_historical_report_semantics(report, plan, "report")
    invalid = deepcopy(report)
    invalid["scorecard"]["counts"]["true_positives"] = 0
    with pytest.raises(ContractError, match="true_positives"):
        validate_historical_report_semantics(invalid, plan, "report")
