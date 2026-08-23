from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any

from agent_insights_quality.contracts import ContractError, load_scenario_catalog


STRUCTURED_REPORT_VERSION = "1.1.0"
TRUST_VIOLATIONS = (
    "structural_failure",
    "provenance_failure",
    "secret_or_pii",
    "judge_schema_failure",
    "unresolved_judgment",
)
REQUIRED_FIELD_RATES = (
    "category_accuracy",
    "severity_accuracy",
    "title_pass_rate",
    "description_pass_rate",
    "proposed_fix_pass_rate",
    "linked_trace_pass_rate",
    "evidence_localization_rate",
    "meaningfulness_rate",
    "actionability_rate",
)
STANDARD_HUMAN_CHECKS = (
    "Verify the healthy baseline has no insight cards.",
    "Verify the expected root cause, category, and severity for each immutable version.",
    "Inspect each card title, description, and proposed fix for specificity and correctness.",
    "Confirm linked traces belong to the current immutable version and half-open window.",
    "For lifecycle scenarios, confirm only planned prior evidence is used and never presented as current evidence.",
    "Check for duplicate, fragmented, or umbrella cards across the run.",
    "Record the human outcome and any discrepancy before promotion.",
)


def _physical_insight_count(report: dict[str, Any]) -> int:
    return len(
        {
            (result["run_id"], result["agent_id"], reference)
            for result in report["scenario_results"]
            for reference in result["insight_references"]
        }
    )


def _count_mismatch_runs(report: dict[str, Any]) -> int:
    expected: dict[tuple[str, str], int] = defaultdict(int)
    observed: dict[tuple[str, str], set[str]] = defaultdict(set)
    for result in report["scenario_results"]:
        key = (result["run_id"], result["agent_id"])
        expected[key] += result["expected_count"]
        observed[key].update(result["insight_references"])
    return sum(len(observed[key]) != value for key, value in expected.items())


def derive_bar_definition(report: dict[str, Any]) -> dict[str, Any]:
    score = report["scorecard"]
    counts = score["counts"]
    rates = score["rates"]
    violations = set(score["violations"])
    expected_findings = sum(
        result["expected_count"] for result in report["scenario_results"]
    )
    observed_findings = _physical_insight_count(report)
    mismatch_runs = _count_mismatch_runs(report)
    required_field_correctness = min(rates[name] for name in REQUIRED_FIELD_RATES)
    trust_failures = sorted(violations.intersection(TRUST_VIOLATIONS))
    capability_fix_mismatches = int("capability_fix_mismatch" in violations)
    actuals = {
        "expected_findings": expected_findings,
        "observed_findings": observed_findings,
        "count_mismatch_runs": mismatch_runs,
        "high_severity_recall": rates["high_severity_recall"],
        "overall_recall": rates["overall_recall"],
        "precision": rates["precision"],
        "required_field_correctness": required_field_correctness,
        "duplication_rate": rates["duplication_rate"],
        "fragmentation_rate": rates["fragmentation_rate"],
        "umbrella_rate": rates["umbrella_rate"],
        "cross_version_stale_rate": rates["cross_version_stale_rate"],
        "healthy_insights": counts["healthy_insights"],
        "capability_fix_mismatches": capability_fix_mismatches,
        "trust_failures": trust_failures,
    }
    gate_values = (
        (
            "exact_run_counts",
            mismatch_runs == 0,
            f"{mismatch_runs} run/agent count mismatches; {expected_findings} expected and "
            f"{observed_findings} observed final cards.",
        ),
        (
            "high_severity_recall",
            rates["high_severity_recall"] == 1,
            "High-severity recall was "
            f"{rates['high_severity_recall']:.1%}; required 100.0%.",
        ),
        (
            "overall_recall",
            rates["overall_recall"] >= 0.90,
            f"Overall recall was {rates['overall_recall']:.1%}; the minimum is 90.0%.",
        ),
        (
            "precision",
            rates["precision"] >= 0.95,
            f"Precision was {rates['precision']:.1%}; the minimum is 95.0%.",
        ),
        (
            "required_fields",
            required_field_correctness == 1,
            "Lowest required-field correctness among accepted true positives was "
            f"{required_field_correctness:.1%}; every required field must be correct.",
        ),
        (
            "duplicate_relationships",
            rates["duplication_rate"] == 0,
            f"Duplicate relationship rate was {rates['duplication_rate']:.1%}; required 0.0%.",
        ),
        (
            "fragment_relationships",
            rates["fragmentation_rate"] == 0,
            f"Fragment relationship rate was {rates['fragmentation_rate']:.1%}; required 0.0%.",
        ),
        (
            "umbrella_relationships",
            rates["umbrella_rate"] == 0,
            f"Umbrella relationship rate was {rates['umbrella_rate']:.1%}; required 0.0%.",
        ),
        (
            "stale_relationships",
            rates["cross_version_stale_rate"] == 0,
            "Cross-version stale relationship rate was "
            f"{rates['cross_version_stale_rate']:.1%}; required 0.0%.",
        ),
        (
            "healthy_controls",
            counts["healthy_insights"] == 0,
            f"Healthy controls produced {counts['healthy_insights']} cards; required 0.",
        ),
        (
            "capability_compatibility",
            capability_fix_mismatches == 0,
            f"Capability/fix compatibility failures were {capability_fix_mismatches}; required 0.",
        ),
        (
            "trusted_evidence",
            not trust_failures,
            (
                "No structural, provenance, PII, judge-schema, or unresolved trust failures."
                if not trust_failures
                else "Trust failures: " + ", ".join(trust_failures) + "."
            ),
        ),
    )
    return {
        "thresholds": {
            "exact_expected_observed_per_run": True,
            "high_severity_recall_required": 1.0,
            "overall_recall_minimum": 0.90,
            "precision_minimum": 0.95,
            "required_field_correctness": 1.0,
            "duplication_rate_maximum": 0.0,
            "fragmentation_rate_maximum": 0.0,
            "umbrella_rate_maximum": 0.0,
            "cross_version_stale_rate_maximum": 0.0,
            "healthy_insights_maximum": 0,
            "capability_fix_mismatches_maximum": 0,
            "trust_failures_allowed": False,
        },
        "actuals": actuals,
        "gates": [
            {"id": gate_id, "passed": passed, "explanation": explanation}
            for gate_id, passed, explanation in gate_values
        ],
    }


def derive_structured_summary(report: dict[str, Any]) -> str:
    bar = derive_bar_definition(report)
    actuals = bar["actuals"]
    failed = [gate["explanation"] for gate in bar["gates"] if not gate["passed"]]
    conclusion = (
        "All enforced gates passed."
        if not failed
        else "Failed gates: " + " ".join(failed)
    )
    if report["status"] == "INCONCLUSIVE" and report.get("failure") is not None:
        conclusion += " Qualification failure: " + report["failure"]["reason"][:300]
    summary = (
        "The quality bar requires exact per-run expected and observed counts, 100% "
        "high-severity recall, at least 90% overall recall, at least 95% precision, "
        "100% required-field correctness on accepted true positives, zero healthy/"
        "duplicate/fragment/umbrella/stale cards, capability-compatible fixes, and "
        "no trust failures. "
        f"Result: {report['status']}. Actuals: {actuals['expected_findings']} expected, "
        f"{actuals['observed_findings']} observed, "
        f"{actuals['high_severity_recall']:.1%} high-severity recall, "
        f"{actuals['overall_recall']:.1%} overall recall, "
        f"{actuals['precision']:.1%} precision, and "
        f"{actuals['required_field_correctness']:.1%} required-field correctness. "
        + conclusion
    )
    return summary[:2000]


def _phase_expected_count(
    assignment: dict[str, Any],
    sequence_index: int,
) -> int:
    expected_count = assignment["expected"]["finding_count"]
    if assignment["expected"]["category"] == "none":
        return 0
    if sequence_index == len(assignment["version_sequence"]) - 1:
        return expected_count
    phase = assignment["version_sequence"][sequence_index]["phase"]
    return (
        expected_count
        if phase in {"faulted", "faulted-initial", "faulted-repeat", "recurred"}
        else 0
    )


def build_human_validation_checklists(
    report: dict[str, Any],
    plan: dict[str, Any],
    catalog: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    catalog = catalog or load_scenario_catalog()
    scenarios = {item["id"]: item for item in catalog["scenarios"]}
    results = {item["scenario_id"]: item for item in report["scenario_results"]}
    assignments_by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for assignment in plan["assignments"]:
        assignments_by_agent[assignment["agent_id"]].append(assignment)

    checklists = []
    for agent in report["agents"]:
        versions: dict[tuple[str, int, str, str, str], dict[str, Any]] = {}
        for assignment in assignments_by_agent.get(agent["id"], []):
            scenario = scenarios[assignment["scenario_id"]]
            sequence = assignment["version_sequence"]
            for sequence_index, version in enumerate(sequence):
                key = (
                    assignment["run_id"],
                    sequence_index,
                    version["phase"],
                    version["version_key"],
                    version["digest"],
                )
                item = versions.setdefault(
                    key,
                    {
                        "run_id": assignment["run_id"],
                        "sequence_index": sequence_index,
                        "phase": version["phase"],
                        "version_key": version["version_key"],
                        "version_digest": version["digest"],
                        "expected_insight_count": 0,
                        "expected_scenarios": [],
                        "observed_final_cards": [],
                        "planned_prior_evidence": sequence_index > 0,
                        "double_check": "",
                    },
                )
                expected_count = _phase_expected_count(assignment, sequence_index)
                item["expected_insight_count"] += expected_count
                item["expected_scenarios"].append(
                    {
                        "scenario_id": assignment["scenario_id"],
                        "title": scenario["title"],
                        "root_cause": scenario["expected"]["root_cause"],
                        "category": assignment["expected"]["category"],
                        "severity": assignment["expected"]["severity"],
                        "expected_insight_count": expected_count,
                    }
                )
                if sequence_index == len(sequence) - 1:
                    result = results[assignment["scenario_id"]]
                    item["observed_final_cards"].extend(
                        {
                            "scenario_id": assignment["scenario_id"],
                            "insight_reference": reference,
                            "verdict": result["verdict"],
                        }
                        for reference in result["insight_references"]
                    )

        ordered_versions = []
        for item in versions.values():
            expected = item["expected_insight_count"]
            observed = len(item["observed_final_cards"])
            is_final = any(
                result["version_sequence"]["phase"] == item["phase"]
                and result["version_sequence"]["version_digest"]
                == item["version_digest"]
                and result["run_id"] == item["run_id"]
                for result in results.values()
                if result["agent_id"] == agent["id"]
            )
            if not is_final:
                item["double_check"] = (
                    "Planned prior lifecycle version: confirm its evidence is used only as "
                    "planned prior evidence and never linked as current evidence."
                )
            elif expected == 0:
                item["double_check"] = (
                    f"Expected 0 final cards and observed {observed}; verify this healthy or "
                    "corrected version remains card-free."
                )
            elif expected != observed:
                item["double_check"] = (
                    f"Expected {expected} final cards and observed {observed}; double-check "
                    "missing roots or extra noise before promotion."
                )
            else:
                item["double_check"] = (
                    f"Expected and observed {expected} final cards; double-check each root, "
                    "category, severity, field, trace link, and collection relationship."
                )
            ordered_versions.append(item)

        checklists.append(
            {
                "agent_id": agent["id"],
                "review_reason": agent["human_validation"],
                "standard_checks": list(STANDARD_HUMAN_CHECKS),
                "versions": ordered_versions,
                "human_outcome": "not_recorded",
            }
        )
    return checklists


def attach_structured_report_context(
    report: dict[str, Any],
    plan: dict[str, Any],
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    enriched = deepcopy(report)
    enriched["schema_version"] = STRUCTURED_REPORT_VERSION
    enriched["bar_definition"] = derive_bar_definition(enriched)
    enriched["summary"] = derive_structured_summary(enriched)
    enriched["human_validation_checklists"] = build_human_validation_checklists(
        enriched, plan, catalog
    )
    return enriched


def validate_structured_bar(report: dict[str, Any], label: str) -> None:
    if report.get("schema_version") != STRUCTURED_REPORT_VERSION:
        return
    if report["bar_definition"] != derive_bar_definition(report):
        raise ContractError(f"{label}: quality bar actuals or gate explanations contradict scorecard")
    if report["summary"] != derive_structured_summary(report):
        raise ContractError(f"{label}: summary contradicts the structured quality result")
