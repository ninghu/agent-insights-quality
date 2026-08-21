from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from agent_insights_quality.contracts import (
    ContractError,
    SCHEMAS,
    load_agent_manifests,
    load_scenario_catalog,
    validate_daily_plan_semantics,
    validate_instance,
)
from agent_insights_quality.judging import (
    validate_evidence_bundle,
    validate_judgment_for_bundle,
)
from agent_insights_quality.privacy import sensitive_findings
from agent_insights_quality.runtime import content_hash, verified_hash


PRIMARY_CLASSIFICATION_MIN_CONFIDENCE = 0.80
PASS_ATTRIBUTES = (
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
ATTRIBUTE_RATES = {
    "category": "category_accuracy",
    "severity": "severity_accuracy",
    "title": "title_pass_rate",
    "description": "description_pass_rate",
    "proposed_fix": "proposed_fix_pass_rate",
    "linked_traces": "linked_trace_pass_rate",
    "evidence_localization": "evidence_localization_rate",
    "meaningfulness": "meaningfulness_rate",
    "actionability": "actionability_rate",
}
def _ratio(numerator: int | float, denominator: int | float) -> float:
    return numerator / denominator if denominator else 1.0


def _timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ContractError(f"Invalid evidence timestamp: {value}") from error


def _fix_is_compatible(bundle: dict[str, Any], insight: dict[str, Any]) -> bool:
    allowed = {
        "prompt": {"prompt_patch", "prose", "no_fix"},
        "hosted_code": {"code_change", "prose", "no_fix"},
        "hosted_custom_container": {"container_change", "code_change", "prose", "no_fix"},
    }
    return insight["fix_kind"] in allowed[bundle["agent"]["type"]] and set(
        insight["tool_references"]
    ).issubset(bundle["agent"]["available_tools"])


def deterministic_violations(
    plan: dict[str, Any],
    bundles: list[dict[str, Any]],
    catalog: dict[str, Any] | None = None,
    agents: list[dict[str, Any]] | None = None,
) -> tuple[set[str], int]:
    """Recompute structural, provenance, trace, capability, PII, count, and dedupe gates."""
    violations: set[str] = set()
    structural_failures = 0
    assignments = {item["scenario_id"]: item for item in plan["assignments"]}
    catalog = catalog or load_scenario_catalog()
    scenario_by_id = {item["id"]: item for item in catalog["scenarios"]}
    agents = agents or load_agent_manifests()
    agent_by_id = {item["id"]: item for item in agents}
    seen_ids: set[str] = set()
    seen_signatures: set[str] = set()
    seen_evidence: set[str] = set()
    bundle_scenarios: set[str] = set()

    for bundle in bundles:
        try:
            validate_evidence_bundle(bundle)
        except (ContractError, KeyError, TypeError):
            violations.add("structural_failure")
            structural_failures += 1
            continue
        scenario_id = bundle["scenario"]["id"]
        assignment = assignments.get(scenario_id)
        if assignment is None or scenario_id in bundle_scenarios:
            violations.add("provenance_failure")
            structural_failures += 1
            continue
        bundle_scenarios.add(scenario_id)
        expected = assignment["expected"]
        catalog_scenario = scenario_by_id.get(scenario_id)
        registered_agent = agent_by_id.get(assignment["agent_id"])
        if (
            catalog_scenario is None
            or registered_agent is None
            or bundle["plan_id"] != plan["plan_id"]
            or bundle["scenario"]["version"] != assignment["scenario_version"]
            or bundle["agent"]["id"] != assignment["agent_id"]
            or bundle["agent"]["name"] != assignment["agent_name"]
            or bundle["agent"]["version_digest"] != assignment["agent_version_digest"]
            or bundle["agent"]["type"] != registered_agent["agent_type"]
            or not set(bundle["agent"]["available_tools"]).issubset(
                registered_agent["implementation"]["representative_tools"]
            )
            or bundle["run"]["engine_build"] != plan["engine"]["build"]
            or bundle["run"]["generator_model"] != plan["engine"]["generator_model"]
            or bundle["run"]["window_start"] != assignment["window"]["start"]
            or bundle["run"]["window_end"] != assignment["window"]["end"]
            or bundle["ground_truth"]["category"] != expected["category"]
            or bundle["ground_truth"]["severity"] != expected["severity"]
            or bundle["ground_truth"]["root_cause"]
            != catalog_scenario["expected"]["root_cause"]
            or bundle["ground_truth"]["fix_boundary"]
            != catalog_scenario["expected"]["fix"]["boundary"]
            or bundle["ground_truth"]["category"]
            != catalog_scenario["expected"]["category"]
            or bundle["ground_truth"]["severity"]
            != catalog_scenario["expected"]["severity"]
        ):
            violations.add("provenance_failure")
        if len(bundle["insights"]) > 5:
            violations.add("over_five_insights")

        trace_ids = {trace["trace_id"] for trace in bundle["trace_evidence"]}
        window_start = _timestamp(bundle["run"]["window_start"])
        window_end = _timestamp(bundle["run"]["window_end"])
        for trace in bundle["trace_evidence"]:
            observed = _timestamp(trace["observed_at"])
            if (
                trace["project_reference"] != plan["project"]["resource_reference"]
                or trace["agent_id"] != assignment["agent_id"]
                or trace["version_digest"] != assignment["agent_version_digest"]
                or observed < window_start
                or observed >= window_end
            ):
                violations.add("provenance_failure")
                if trace["version_digest"] != assignment["agent_version_digest"]:
                    violations.add("cross_version_stale")

        for insight in bundle["insights"]:
            if insight["trace_count"] != len(insight["trace_ids"]):
                violations.add("structural_failure")
                structural_failures += 1
            if not set(insight["trace_ids"]).issubset(trace_ids):
                violations.add("provenance_failure")
            if not _fix_is_compatible(bundle, insight):
                violations.add("structural_failure")
                structural_failures += 1
            for value, violation in (
                (insight["id"], "duplication"),
                (insight["signature"], "duplication"),
                (insight["evidence_fingerprint"], "duplication"),
            ):
                target = (
                    seen_ids
                    if value == insight["id"]
                    else seen_signatures
                    if value == insight["signature"]
                    else seen_evidence
                )
                if value in target:
                    violations.add(violation)
                target.add(value)
        if sensitive_findings(bundle):
            violations.add("secret_or_pii")

    if bundle_scenarios != set(assignments):
        violations.add("incomplete_catalog")
    return violations, structural_failures


def _validate_judgments(
    bundles: list[dict[str, Any]],
    judgments: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str], dict[str, Any]], set[str], bool]:
    bundle_by_id = {bundle.get("bundle_id"): bundle for bundle in bundles}
    by_mapping: dict[tuple[str, str], dict[str, Any]] = {}
    violations: set[str] = set()
    trustworthy = True
    for judgment in judgments:
        try:
            bundle = bundle_by_id[judgment["bundle_id"]]
            validate_judgment_for_bundle(judgment, bundle)
            if judgment["model"] != "gpt-5.6-sol":
                raise ContractError("judgment model is not pinned")
            expected_prompt_version = (
                "primary-v1"
                if judgment["judge_role"] == "primary"
                else "blinded-verifier-v1"
            )
            if judgment["prompt_version"] != expected_prompt_version:
                raise ContractError("judgment prompt version does not match its role")
            if judgment["evidence_schema_version"] != bundle["schema_version"]:
                raise ContractError("judgment evidence version mismatch")
            scenario_id = judgment["mapping"]["scenario_id"]
            insight_id = judgment["mapping"]["insight_id"]
            if scenario_id != bundle["scenario"]["id"] or insight_id not in {
                insight["id"] for insight in bundle["insights"]
            }:
                raise ContractError("judgment mapping does not exist")
            if judgment["judge_role"] != "primary":
                continue
            if judgment["confidence"] < PRIMARY_CLASSIFICATION_MIN_CONFIDENCE:
                violations.add("unresolved_judgment")
                trustworthy = False
                continue
            key = (scenario_id, insight_id)
            if key in by_mapping:
                raise ContractError("multiple primary judgments for one insight")
            by_mapping[key] = judgment
        except (ContractError, KeyError, TypeError):
            violations.add("judge_schema_failure")
            trustworthy = False
    return by_mapping, violations, trustworthy


def score_run(
    plan: dict[str, Any],
    bundles: list[dict[str, Any]],
    judgments: list[dict[str, Any]],
    *,
    issue_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Recompute every aggregate from plan, evidence, and strict primary judgments."""
    agents = load_agent_manifests()
    catalog = load_scenario_catalog({agent["id"] for agent in agents})
    validate_instance(plan, SCHEMAS / "daily-plan.schema.json", "daily plan")
    validate_daily_plan_semantics(plan, agents, catalog, "daily plan")

    violations, structural_failures = deterministic_violations(
        plan, bundles, catalog, agents
    )
    primary, judgment_violations, trustworthy = _validate_judgments(bundles, judgments)
    violations.update(judgment_violations)
    assignments = {item["scenario_id"]: item for item in plan["assignments"]}
    bundles_by_scenario = {item.get("scenario", {}).get("id"): item for item in bundles}

    classifications: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_primary: list[dict[str, Any]] = []
    unresolved = False
    for scenario_id, bundle in bundles_by_scenario.items():
        if scenario_id not in assignments or "insights" not in bundle:
            continue
        for insight in bundle["insights"]:
            judgment = primary.get((scenario_id, insight["id"]))
            if judgment is None:
                unresolved = True
                continue
            classifications[scenario_id].append(judgment)
            all_primary.append(judgment)
    if unresolved:
        violations.add("unresolved_judgment")
        trustworthy = False

    expected_faults = [
        scenario_id
        for scenario_id, assignment in assignments.items()
        if assignment["expected"]["finding_count"] == 1
    ]
    healthy = [
        scenario_id
        for scenario_id, assignment in assignments.items()
        if assignment["expected"]["finding_count"] == 0
    ]

    def is_true_positive(judgment: dict[str, Any]) -> bool:
        return judgment["verdict"] == "correct" and all(
            judgment["attributes"][name]["passes"] for name in PASS_ATTRIBUTES
        )

    true_positive_by_scenario = {
        scenario_id: [item for item in classifications[scenario_id] if is_true_positive(item)]
        for scenario_id in expected_faults
    }
    if any(len(items) > 1 for items in true_positive_by_scenario.values()):
        violations.add("duplication")
    if any(len(items) > 1 for items in classifications.values()):
        violations.add("fragmentation")
    true_positives = sum(bool(items) for items in true_positive_by_scenario.values())
    produced = sum(len(bundle.get("insights", [])) for bundle in bundles)
    false_positives = produced - true_positives
    false_negatives = len(expected_faults) - true_positives
    partially_useful = sum(item["verdict"] == "partially_useful" for item in all_primary)
    healthy_insights = sum(
        len(bundles_by_scenario.get(scenario_id, {}).get("insights", []))
        for scenario_id in healthy
    )
    healthy_noisy_cases = sum(
        bool(bundles_by_scenario.get(scenario_id, {}).get("insights", []))
        for scenario_id in healthy
    )
    if healthy_insights:
        violations.add("healthy_false_positive")

    precision = _ratio(true_positives, produced)
    recall = _ratio(true_positives, len(expected_faults))
    f1 = _ratio(2 * precision * recall, precision + recall)

    def severity_recall(severity: str) -> float:
        relevant = [
            scenario_id
            for scenario_id in expected_faults
            if assignments[scenario_id]["expected"]["severity"] == severity
        ]
        return _ratio(
            sum(bool(true_positive_by_scenario[scenario_id]) for scenario_id in relevant),
            len(relevant),
        )

    mapped_expected = [
        item
        for scenario_id in expected_faults
        for item in classifications[scenario_id]
        if item["verdict"] != "incorrect_noise"
    ]

    def attribute_rate(name: str) -> float:
        return _ratio(
            sum(item["attributes"][name]["passes"] for item in mapped_expected),
            len(mapped_expected),
        )

    duplicate_count = sum(bool(item["relationships"]["duplicate_of"]) for item in all_primary)
    fragmented_count = sum(bool(item["relationships"]["fragmented_with"]) for item in all_primary)
    umbrella_count = sum(bool(item["relationships"]["umbrella_for"]) for item in all_primary)
    distinct_count = sum(
        not (
            item["relationships"]["duplicate_of"]
            or item["relationships"]["fragmented_with"]
            or item["relationships"]["umbrella_for"]
        )
        for item in all_primary
    )
    if duplicate_count:
        violations.add("duplication")
    if fragmented_count:
        violations.add("fragmentation")
    if umbrella_count:
        violations.add("umbrella")

    stale_count = int("cross_version_stale" in violations)
    relationship_denominator = len(all_primary)
    rates = {
        "high_severity_recall": severity_recall("high"),
        "medium_severity_recall": severity_recall("medium"),
        "low_severity_recall": severity_recall("low"),
        "overall_recall": recall,
        "precision": precision,
        "f1": f1,
        "healthy_noise_rate": _ratio(healthy_noisy_cases, len(healthy)),
        **{rate: attribute_rate(attribute) for attribute, rate in ATTRIBUTE_RATES.items()},
        "distinctness_rate": _ratio(distinct_count, relationship_denominator),
        "duplication_rate": _ratio(duplicate_count, relationship_denominator),
        "fragmentation_rate": _ratio(fragmented_count, relationship_denominator),
        "umbrella_rate": _ratio(umbrella_count, relationship_denominator),
        "cross_version_stale_rate": _ratio(stale_count, len(bundles)),
    }

    if rates["high_severity_recall"] != 1:
        violations.add("high_severity_recall")
    if rates["overall_recall"] < 0.90:
        violations.add("overall_recall")
    if rates["precision"] < 0.95:
        violations.add("precision")
    if any(rates[name] != 1 for name in ATTRIBUTE_RATES.values()) or rates[
        "distinctness_rate"
    ] != 1:
        violations.add("attribute_correctness")
    if rates["cross_version_stale_rate"]:
        violations.add("cross_version_stale")

    complete = set(bundles_by_scenario) == set(assignments) and trustworthy
    if not complete:
        violations.add("incomplete_catalog")
    inconclusive = not trustworthy or bool(
        {"provenance_failure", "judge_schema_failure", "unresolved_judgment"} & violations
    )
    verdict = "INCONCLUSIVE" if inconclusive or not complete else (
        "AT BAR" if not violations else "NOT AT BAR"
    )
    memory = Counter(issue_counts or {})
    scorecard = {
        "schema_version": "1.0.0",
        "verdict": verdict,
        "complete": complete and verdict != "INCONCLUSIVE",
        "counts": {
            "active_scenarios": len(assignments),
            "completed_scenarios": len(set(bundles_by_scenario) & set(assignments)),
            "true_positives": true_positives,
            "partially_useful": partially_useful,
            "false_positives": false_positives,
            "false_negatives": false_negatives,
            "healthy_insights": healthy_insights,
            "structural_failures": structural_failures,
            "new_issues": memory["new"],
            "known_issues": memory["known"],
            "resolved_issues": memory["resolved"],
            "regressed_issues": memory["regressed"],
        },
        "rates": rates,
        "violations": sorted(violations),
    }
    validate_instance(scorecard, SCHEMAS / "scorecard.schema.json", "scorecard")
    return scorecard


def case_to_insight_mappings(
    plan: dict[str, Any],
    bundles: list[dict[str, Any]],
    judgments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return public-safe case mappings derived from evidence, never supplied aggregates."""
    primary, violations, _ = _validate_judgments(bundles, judgments)
    if violations:
        raise ContractError("Cannot emit mappings from invalid judgments")
    bundle_by_scenario = {
        bundle["scenario"]["id"]: bundle
        for bundle in bundles
        if "scenario" in bundle
    }
    mappings = []
    for assignment in plan["assignments"]:
        scenario_id = assignment["scenario_id"]
        bundle = bundle_by_scenario.get(scenario_id)
        items = []
        for insight in bundle["insights"] if bundle else []:
            judgment = primary.get((scenario_id, insight["id"]))
            items.append(
                {
                    "insight_reference": content_hash(
                        {
                            "bundle_id": bundle["bundle_id"],
                            "insight_id": insight["id"],
                        }
                    ),
                    "verdict": judgment["verdict"] if judgment else "inconclusive",
                }
            )
        mappings.append({"scenario_id": scenario_id, "insights": items})
    return mappings


def scorecards_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare supplied aggregates without accepting floating-point formatting differences."""
    if left.keys() != right.keys():
        return False
    return all(
        math.isclose(left["rates"][key], right["rates"][key], abs_tol=1e-12)
        for key in left["rates"]
    ) and {key: value for key, value in left.items() if key != "rates"} == {
        key: value for key, value in right.items() if key != "rates"
    }
