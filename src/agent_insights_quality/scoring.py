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
    judgment_target_insight_ids,
    validate_evidence_bundle,
    validate_judgment_for_bundle,
)
from agent_insights_quality.privacy import sensitive_findings
from agent_insights_quality.artifact_io import content_hash


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
TRUST_FAILURES = {
    "structural_failure",
    "provenance_failure",
    "judge_schema_failure",
    "unresolved_judgment",
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


def _run_count_mismatches(
    plan: dict[str, Any],
    bundles: list[dict[str, Any]],
) -> dict[tuple[str, str, str], tuple[int, int]]:
    expected: Counter[tuple[str, str, str]] = Counter()
    actual: dict[tuple[str, str, str], int] = {}
    assignments = {item["scenario_id"]: item for item in plan["assignments"]}
    for assignment in plan["assignments"]:
        key = (plan["report_date"], assignment["run_id"], assignment["agent_id"])
        expected[key] += assignment["expected"]["finding_count"]
    for bundle in bundles:
        scenario_id = bundle["scenario"]["id"]
        assignment = assignments.get(scenario_id)
        if assignment is None:
            continue
        key = (plan["report_date"], bundle["run"]["run_id"], bundle["agent"]["id"])
        count = bundle["run_finding_count"]["actual"]
        prior = actual.setdefault(key, count)
        if prior != count:
            raise ContractError("Evidence bundles disagree on the exact run insight total")
    return {
        key: (count, actual.get(key, 0))
        for key, count in expected.items()
        if count != actual.get(key, 0)
    }


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
    seen_run_insights: dict[tuple[str, str, str], tuple[str, str]] = {}
    bundle_scenarios: set[str] = set()
    valid_bundles: list[dict[str, Any]] = []

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
        valid_bundles.append(bundle)
        expected = assignment["expected"]
        catalog_scenario = scenario_by_id.get(scenario_id)
        registered_agent = agent_by_id.get(assignment["agent_id"])
        current_version = assignment["version_sequence"][-1]
        version_context = bundle["version_sequence"]
        if (
            catalog_scenario is None
            or registered_agent is None
            or bundle["plan_id"] != plan["plan_id"]
            or bundle["scenario"]["version"] != assignment["scenario_version"]
            or bundle["agent"]["id"] != assignment["agent_id"]
            or bundle["agent"]["name"] != assignment["agent_name"]
            or version_context["phase"] != current_version["phase"]
            or version_context["version_digest"] != current_version["digest"]
            or version_context["run_id"] != assignment["run_id"]
            or bundle["agent"]["version_digest"] != version_context["version_digest"]
            or bundle["agent"]["type"] != registered_agent["agent_type"]
            or not set(bundle["agent"]["available_tools"]).issubset(
                registered_agent["implementation"]["representative_tools"]
            )
            or bundle["run"]["engine_build"] != plan["engine"]["build"]
            or bundle["run"]["generator_model"] != plan["engine"]["generator_model"]
            or bundle["run"]["run_id"] != version_context["run_id"]
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
        previous = bundle["previous_insight"]
        if len(assignment["version_sequence"]) > 1:
            prior_versions = assignment["version_sequence"][:-1]
            if previous is None or not any(
                previous["phase"] == version["phase"]
                and previous["run_id"] == assignment["run_id"]
                and previous["version_digest"] == version["digest"]
                for version in prior_versions
            ):
                violations.update({"provenance_failure", "cross_version_stale"})
        elif previous is not None:
            violations.update({"provenance_failure", "cross_version_stale"})
        trace_ids = {trace["trace_id"] for trace in bundle["trace_evidence"]}
        prior_trace_ids = set(bundle["prior_trace_ids"])
        if trace_ids & prior_trace_ids:
            violations.add("provenance_failure")
        if len(assignment["version_sequence"]) == 1 and prior_trace_ids:
            violations.add("provenance_failure")
        window_start = _timestamp(bundle["run"]["window_start"])
        window_end = _timestamp(bundle["run"]["window_end"])
        for trace in bundle["trace_evidence"]:
            observed = _timestamp(trace["observed_at"])
            if (
                trace["project_reference"] != plan["project"]["resource_reference"]
                or trace["agent_id"] != assignment["agent_id"]
                or trace["version_digest"] != version_context["version_digest"]
                or observed < window_start
                or observed >= window_end
            ):
                violations.add("provenance_failure")
                if trace["version_digest"] != version_context["version_digest"]:
                    violations.add("cross_version_stale")

        bundle_insight_ids: set[str] = set()
        for insight in bundle["insights"]:
            linked_trace_ids = set(insight["trace_ids"])
            if not linked_trace_ids.issubset(trace_ids | prior_trace_ids):
                violations.add("provenance_failure")
            elif linked_trace_ids & prior_trace_ids:
                violations.add("cross_version_stale")
        for insight in bundle["insights"]:
            if insight["id"] in bundle_insight_ids:
                violations.add("duplication")
            bundle_insight_ids.add(insight["id"])
            if insight["trace_count"] != len(insight["trace_ids"]):
                violations.add("structural_failure")
                structural_failures += 1
            if not _fix_is_compatible(bundle, insight):
                violations.add("capability_fix_mismatch")
            run_identity = (
                bundle["run"]["run_id"],
                bundle["agent"]["id"],
                insight["id"],
            )
            prior_identity = seen_run_insights.get(run_identity)
            current_identity = (
                insight["signature"],
                insight["evidence_fingerprint"],
            )
            if prior_identity is not None:
                if prior_identity != current_identity:
                    violations.update({"structural_failure", "provenance_failure"})
                    structural_failures += 1
                continue
            seen_run_insights[run_identity] = current_identity
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
    for expected_count, observed_count in _run_count_mismatches(plan, valid_bundles).values():
        violations.add("finding_count_mismatch")
        violations.add("extra_noise" if observed_count > expected_count else "missing_findings")
    return violations, structural_failures


def _validate_judgments(
    bundles: list[dict[str, Any]],
    judgments: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str | None], dict[str, Any]], set[str], bool]:
    bundle_by_id = {bundle.get("bundle_id"): bundle for bundle in bundles}
    by_mapping: dict[tuple[str, str | None], dict[str, Any]] = {}
    physical_primary: set[tuple[str, str, str]] = set()
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
            valid_mapping = insight_id in set(
                judgment_target_insight_ids(bundle)
            )
            if scenario_id != bundle["scenario"]["id"] or not valid_mapping:
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
            if insight_id is not None:
                physical_key = (
                    str(bundle["run"]["run_id"]),
                    str(bundle["agent"]["id"]),
                    str(insight_id),
                )
                if physical_key in physical_primary:
                    raise ContractError(
                        "multiple primary judgments for one physical run insight"
                    )
                physical_primary.add(physical_key)
            by_mapping[key] = judgment
        except (ContractError, KeyError, TypeError):
            violations.add("judge_schema_failure")
            trustworthy = False
    return by_mapping, violations, trustworthy


def _allocate_run_insights(
    plan: dict[str, Any],
    bundles_by_scenario: dict[str, dict[str, Any]],
    primary: dict[tuple[str, str | None], dict[str, Any]],
) -> tuple[
    dict[tuple[str, str, str], str],
    set[tuple[str, str, str]],
    dict[tuple[str, str, str], dict[str, Any]],
]:
    """Assign each physical run insight to at most one scenario.

    Agent Insights returns one run-level collection. Multiple scenario bundles for that run may
    therefore contain the same cards. The allocation first computes a maximum matching between
    trustworthy cards and expected scenario slots, then deterministically assigns any remaining
    noise/partial cards to one scenario for diagnostics.
    """

    assignment_order = {
        assignment["scenario_id"]: index
        for index, assignment in enumerate(plan["assignments"])
    }
    candidates: dict[
        tuple[str, str, str],
        dict[str, dict[str, Any]],
    ] = defaultdict(dict)
    for scenario_id, bundle in bundles_by_scenario.items():
        for insight in bundle["insights"]:
            judgment = primary.get((scenario_id, insight["id"]))
            if judgment is None:
                continue
            key = (bundle["run"]["run_id"], bundle["agent"]["id"], insight["id"])
            candidates[key][scenario_id] = judgment

    def is_true_positive(judgment: dict[str, Any]) -> bool:
        return judgment["verdict"] == "correct" and all(
            judgment["attributes"][name]["passes"] for name in PASS_ATTRIBUTES
        )

    slots: list[tuple[str, int]] = []
    slot_candidates: dict[tuple[str, int], tuple[tuple[str, str, str], ...]] = {}
    for assignment in plan["assignments"]:
        scenario_id = assignment["scenario_id"]
        expected_count = assignment["expected"]["finding_count"]
        for slot_index in range(expected_count):
            slot = (scenario_id, slot_index)
            slots.append(slot)
            slot_candidates[slot] = tuple(
                sorted(
                    key
                    for key, judgments in candidates.items()
                    if scenario_id in judgments
                    and is_true_positive(judgments[scenario_id])
                )
            )
    slots.sort(
        key=lambda slot: (
            len(slot_candidates[slot]),
            assignment_order[slot[0]],
            slot[1],
        )
    )

    insight_to_slot: dict[tuple[str, str, str], tuple[str, int]] = {}

    def match(slot: tuple[str, int], seen: set[tuple[str, str, str]]) -> bool:
        for key in slot_candidates[slot]:
            if key in seen:
                continue
            seen.add(key)
            prior = insight_to_slot.get(key)
            if prior is None or match(prior, seen):
                insight_to_slot[key] = slot
                return True
        return False

    for slot in slots:
        match(slot, set())

    owners = {key: slot[0] for key, slot in insight_to_slot.items()}
    matched = set(owners)
    owner_counts = Counter(owners.values())
    verdict_rank = {
        "partially_useful": 0,
        "incorrect_noise": 1,
        "correct": 2,
    }
    for key, judgments in sorted(candidates.items()):
        if key in owners:
            continue
        owner = min(
            judgments,
            key=lambda scenario_id: (
                verdict_rank.get(judgments[scenario_id]["verdict"], 3),
                owner_counts[scenario_id],
                assignment_order[scenario_id],
            ),
        )
        owners[key] = owner
        owner_counts[owner] += 1

    representatives = {
        key: candidates[key][owner]
        for key, owner in owners.items()
    }
    return owners, matched, representatives


def _has_complete_physical_judgment_ownership(
    bundles: list[dict[str, Any]],
) -> bool:
    physical: set[tuple[str, str, str]] = set()
    owners: Counter[tuple[str, str, str]] = Counter()
    for bundle in bundles:
        run_id = str(bundle["run"]["run_id"])
        agent_id = str(bundle["agent"]["id"])
        for insight in [*bundle["insights"], *bundle["run_noise_insights"]]:
            physical.add((run_id, agent_id, str(insight["id"])))
        for insight in bundle["insights"]:
            owners[(run_id, agent_id, str(insight["id"]))] += 1
    return set(owners) == physical and all(count == 1 for count in owners.values())


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
    valid_bundles: list[dict[str, Any]] = []
    for bundle in bundles:
        try:
            validate_evidence_bundle(bundle)
        except (ContractError, KeyError, TypeError):
            continue
        valid_bundles.append(bundle)
    primary, judgment_violations, trustworthy = _validate_judgments(
        valid_bundles, judgments
    )
    violations.update(judgment_violations)
    assignments = {item["scenario_id"]: item for item in plan["assignments"]}
    bundles_by_scenario = {
        item["scenario"]["id"]: item
        for item in valid_bundles
        if item["scenario"]["id"] in assignments
    }

    unresolved = not _has_complete_physical_judgment_ownership(valid_bundles)
    for scenario_id, bundle in bundles_by_scenario.items():
        if scenario_id not in assignments or "insights" not in bundle:
            continue
        for insight_id in judgment_target_insight_ids(bundle):
            if primary.get((scenario_id, insight_id)) is None:
                unresolved = True
    if unresolved:
        violations.add("unresolved_judgment")
        trustworthy = False

    expected_faults = [
        scenario_id
        for scenario_id, assignment in assignments.items()
        if assignment["expected"]["finding_count"] > 0
    ]
    healthy = [
        scenario_id
        for scenario_id, assignment in assignments.items()
        if assignment["expected"]["finding_count"] == 0
    ]

    owners, matched_physical, representative_by_physical = _allocate_run_insights(
        plan,
        bundles_by_scenario,
        primary,
    )
    all_primary = list(representative_by_physical.values())
    true_positive_counts = Counter(
        owners[key]
        for key in matched_physical
        if owners[key] in expected_faults
    )
    expected_fault_count = sum(
        assignments[scenario_id]["expected"]["finding_count"]
        for scenario_id in expected_faults
    )
    true_positives = len(
        [
            key
            for key in matched_physical
            if owners[key] in expected_faults
        ]
    )
    run_totals: dict[tuple[str, str], int] = {}
    for bundle in valid_bundles:
        key = (bundle["run"]["run_id"], bundle["agent"]["id"])
        count = bundle["run_finding_count"]["actual"]
        prior = run_totals.setdefault(key, count)
        if prior != count:
            raise ContractError("Evidence bundles disagree on the exact run insight total")
    produced = sum(run_totals.values())
    false_positives = max(0, produced - true_positives)
    false_negatives = expected_fault_count - true_positives
    partially_useful = sum(item["verdict"] == "partially_useful" for item in all_primary)
    healthy_insights = sum(
        bundles_by_scenario[scenario_id]["finding_count"]["actual"]
        for scenario_id in healthy
        if scenario_id in bundles_by_scenario
    )
    healthy_noisy_cases = sum(
        bundles_by_scenario.get(scenario_id, {})
        .get("finding_count", {})
        .get("actual", 0)
        > 0
        for scenario_id in healthy
    )
    if healthy_insights:
        violations.add("healthy_false_positive")

    precision = _ratio(true_positives, produced)
    recall = _ratio(true_positives, expected_fault_count)
    f1 = _ratio(2 * precision * recall, precision + recall)

    def severity_recall(severity: str) -> float:
        relevant = [
            scenario_id
            for scenario_id in expected_faults
            if assignments[scenario_id]["expected"]["severity"] == severity
        ]
        return _ratio(
            sum(true_positive_counts.get(scenario_id, 0) for scenario_id in relevant),
            sum(
                assignments[scenario_id]["expected"]["finding_count"]
                for scenario_id in relevant
            ),
        )

    mapped_expected = [
        judgment
        for key, judgment in representative_by_physical.items()
        if owners[key] in expected_faults
        and judgment["verdict"] != "incorrect_noise"
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
        "cross_version_stale_rate": _ratio(stale_count, len(valid_bundles)),
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

    trust_failures = TRUST_FAILURES & violations
    complete = (
        set(bundles_by_scenario) == set(assignments)
        and not trust_failures
        and trustworthy
    )
    if not complete:
        violations.add("incomplete_catalog")
    inconclusive = not trustworthy or bool(
        (TRUST_FAILURES | {"incomplete_catalog"}) & violations
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
    if not _has_complete_physical_judgment_ownership(bundles):
        raise ContractError(
            "Cannot emit mappings without one primary owner for every physical insight"
        )
    missing_judgments = [
        (bundle["scenario"]["id"], insight_id)
        for bundle in bundles
        for insight_id in judgment_target_insight_ids(bundle)
        if primary.get((bundle["scenario"]["id"], insight_id)) is None
    ]
    if missing_judgments:
        raise ContractError(
            "Cannot emit mappings without a trustworthy primary judgment for every produced insight"
        )
    bundle_by_scenario = {
        bundle["scenario"]["id"]: bundle
        for bundle in bundles
        if "scenario" in bundle
    }
    owners, _matched, _representatives = _allocate_run_insights(
        plan,
        bundle_by_scenario,
        primary,
    )
    mappings = []
    for assignment in plan["assignments"]:
        scenario_id = assignment["scenario_id"]
        bundle = bundle_by_scenario.get(scenario_id)
        items = []
        trusted_count = 0
        for insight in bundle["insights"] if bundle else []:
            physical_key = (
                bundle["run"]["run_id"],
                bundle["agent"]["id"],
                insight["id"],
            )
            if owners.get(physical_key) != scenario_id:
                continue
            judgment = primary.get((scenario_id, insight["id"]))
            if (
                judgment is not None
                and judgment["verdict"] == "correct"
                and all(
                    judgment["attributes"][name]["passes"]
                    for name in PASS_ATTRIBUTES
                )
            ):
                trusted_count += 1
            items.append(
                {
                    "insight_reference": content_hash(
                        {
                            "report_date": plan["report_date"],
                            "run_id": assignment["run_id"],
                            "agent_id": assignment["agent_id"],
                            "insight_id": insight["id"],
                        }
                    ),
                    "verdict": judgment["verdict"] if judgment else "inconclusive",
                }
            )
        expected_count = assignment["expected"]["finding_count"]
        observed_count = (
            bundle["finding_count"]["actual"] if bundle is not None else 0
        )
        verdicts = {item["verdict"] for item in items}
        if bundle is None or "inconclusive" in verdicts:
            verdict = "inconclusive"
        elif expected_count == 0:
            verdict = "correct" if observed_count == 0 else "incorrect_noise"
        elif observed_count == 0:
            verdict = "missed"
        elif (
            trusted_count == expected_count
            and observed_count == expected_count
            and verdicts == {"correct"}
        ):
            verdict = "correct"
        elif trusted_count == 0 and verdicts == {"incorrect_noise"}:
            verdict = "incorrect_noise"
        elif trusted_count == 0 and verdicts == {"partially_useful"}:
            verdict = "partially_useful"
        else:
            verdict = "mixed"
        mappings.append(
            {
                "scenario_id": scenario_id,
                "agent_id": assignment["agent_id"],
                "run_id": assignment["run_id"],
                "version_sequence": {
                    "phase": (
                        bundle["version_sequence"]["phase"]
                        if bundle
                        else assignment["version_sequence"][-1]["phase"]
                    ),
                    "version_digest": (
                        bundle["version_sequence"]["version_digest"]
                        if bundle
                        else assignment["version_sequence"][-1]["digest"]
                    ),
                },
                "agent_version_digest": (
                    bundle["version_sequence"]["version_digest"]
                    if bundle
                    else assignment["version_sequence"][-1]["digest"]
                ),
                "expected_count": expected_count,
                "observed_count": observed_count,
                "verdict": verdict,
                "insights": items,
            }
        )
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
