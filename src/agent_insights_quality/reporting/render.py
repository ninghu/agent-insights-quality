from __future__ import annotations

import html
import os
import re
from collections import defaultdict
from copy import deepcopy
from typing import Any, Mapping
from agent_insights_quality.contracts import (
    ContractError,
    SCHEMAS,
    SCORECARD_SCHEMA,
    load_agent_manifests,
    load_scenario_catalog,
    validate_bug_action_semantics,
    validate_canonical_report_semantics,
    validate_instance,
)
from agent_insights_quality.links import validate_agent_page_url
from agent_insights_quality.links import RuntimeLinkContext
from agent_insights_quality.artifact_io import content_hash, verified_hash
from agent_insights_quality.judging import AUTO_BUG_CONFIDENCE
from agent_insights_quality.reporting.model import (
    STRUCTURED_REPORT_VERSION,
    derive_bar_definition,
    validate_structured_bar,
)


SECTION_TITLES = (
    "Summary",
    "What is working",
    "What needs improvement",
    "Test agents and agent links",
)
MAIL_TRANSPORT_ORDER = (
    "connected_copilot_mail",
    "microsoft_graph",
    "local_outlook_com",
)
_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+)$")
_PUBLIC_REPORT_BASE_URL = (
    "https://github.com/ninghu/agent-insights-quality/blob/main/"
)
_OUTLOOK_TEXT_STYLE = (
    "font-family:Segoe UI,Arial,sans-serif;font-size:14px;line-height:21px;"
)


def _human_validation_reason(items: list[dict[str, Any]]) -> str:
    reasons: set[str] = set()
    for item in items:
        if item["verdict"] == "partially_useful":
            reasons.add("partially useful judgment")
        if (
            item["verifier_verdict"] is not None
            and item["verifier_verdict"] != item["verdict"]
        ):
            reasons.add("primary/verifier disagreement")
        if item["confidence"] < AUTO_BUG_CONFIDENCE or (
            item["verifier_confidence"] is not None
            and item["verifier_confidence"] < AUTO_BUG_CONFIDENCE
        ):
            reasons.add("low-confidence judgment")
        if item["novel"]:
            reasons.add("novel finding")
        if not item["fix_verifiable"]:
            reasons.add("unverifiable fix")
    if not reasons:
        return "N/A"
    return "Required: " + "; ".join(sorted(reasons)) + "."


def validate_report_consistency(
    report: dict[str, Any],
    *,
    validate_catalog: bool = True,
) -> None:
    validate_instance(report, SCHEMAS / "canonical-report.schema.json", "canonical report")
    validate_instance(report["scorecard"], SCORECARD_SCHEMA, "canonical report scorecard")
    validate_structured_bar(report, "canonical report")
    validate_bug_action_semantics(report, "canonical report")
    if validate_catalog:
        agents = load_agent_manifests()
        catalog = load_scenario_catalog({agent["id"] for agent in agents})
        validate_canonical_report_semantics(
            report,
            agents,
            catalog,
            "canonical report",
            expected_scenario_ids={
                result["scenario_id"] for result in report["scenario_results"]
            },
        )
    if report["status"] != report["scorecard"]["verdict"]:
        raise ContractError("Canonical report status contradicts its scorecard")
    if report["status"] == "INCONCLUSIVE" and report["failure"] is None:
        raise ContractError("INCONCLUSIVE report requires explicit failure details")
    if report["status"] != "INCONCLUSIVE" and report["failure"] is not None:
        raise ContractError("Conclusive report cannot contain failure details")
    if report["report_id"] != report["plan_id"]:
        raise ContractError("Canonical report identity contradicts its plan")
    if report["status"] == "INCONCLUSIVE" and report["scorecard"]["complete"]:
        raise ContractError("INCONCLUSIVE report cannot claim completeness")
    if report["status"] != "INCONCLUSIVE" and not report["scorecard"]["complete"]:
        raise ContractError("Conclusive report requires a complete scorecard")
    scenarios_by_agent: dict[str, set[str]] = {}
    for result in report["scenario_results"]:
        scenarios_by_agent.setdefault(result["agent_id"], set()).add(
            result["scenario_id"]
        )
    for agent in report["agents"]:
        relevant = [
            item
            for item in report["field_judgments"]
            if item["scenario_id"] in scenarios_by_agent.get(agent["id"], set())
        ]
        expected_reason = _human_validation_reason(relevant)
        if agent["human_validation"] != expected_reason:
            raise ContractError(
                "Human validation must be derived from semantic judgments"
            )
    if report.get("schema_version") == STRUCTURED_REPORT_VERSION:
        checklists = {
            item["agent_id"]: item for item in report["human_validation_checklists"]
        }
        if len(checklists) != len(report["agents"]) or set(checklists) != {
            agent["id"] for agent in report["agents"]
        }:
            raise ContractError(
                "Structured report requires exactly one human validation checklist per agent"
            )
        for agent in report["agents"]:
            if checklists[agent["id"]]["review_reason"] != agent["human_validation"]:
                raise ContractError(
                    "Human validation checklist contradicts the derived review reason"
                )


def _content_utility_grade(judgment: dict[str, Any]) -> str:
    attributes = judgment["attributes"]
    if all(attributes.values()):
        return "fully_correct"
    if attributes["meaningfulness"]:
        return "partially_useful"
    return "incorrect_noise"


def _finding_grades(report: dict[str, Any]) -> dict[str, int]:
    actuals = (report.get("bar_definition") or derive_bar_definition(report))["actuals"]
    observed = actuals["observed_findings"]
    judgments = report["field_judgments"]
    if len(judgments) == observed:
        grades = [_content_utility_grade(item) for item in judgments]
        correct = grades.count("fully_correct")
        partially_useful = grades.count("partially_useful")
    else:
        correct = report["scorecard"]["counts"]["true_positives"]
        partially_useful = report["scorecard"]["counts"]["partially_useful"]
    return {
        "correct": correct,
        "partially_useful": partially_useful,
        "incorrect": max(0, observed - correct - partially_useful),
    }


def _summary_narrative(report: dict[str, Any]) -> list[str]:
    score = report["scorecard"]
    counts = score["counts"]
    actuals = (report.get("bar_definition") or derive_bar_definition(report))["actuals"]
    grades = _finding_grades(report)
    if report["status"] == "INCONCLUSIVE":
        reason = report["failure"]["reason"] if report["failure"] is not None else ""
        return [
            "No reliable judgment can be made about customer-facing insight quality. "
            f"Only {counts['completed_scenarios']} of {counts['active_scenarios']} "
            f"planned scenarios completed. {reason}".strip(),
            "Treat both generated findings and missing findings as untrusted until a complete, "
            "validated run succeeds.",
        ]

    expected = actuals["expected_findings"]
    observed = actuals["observed_findings"]
    if expected == 0 and observed == 0:
        assessment = (
            "No meaningful product problems were expected or found, and the run produced "
            "no unexpected cards."
        )
    else:
        expected_label = "problem" if expected == 1 else "problems"
        fully_verb = "was" if grades["correct"] == 1 else "were"
        partial_verb = "was" if grades["partially_useful"] == 1 else "were"
        incorrect_verb = "was" if grades["incorrect"] == 1 else "were"
        assessment = (
            f"Among {observed} distinct observed cards, {grades['correct']} {fully_verb} fully "
            f"correct on customer utility and content, {grades['partially_useful']} "
            f"{partial_verb} "
            f"partially useful, and {grades['incorrect']} {incorrect_verb} incorrect or noisy. These "
            "utility grades intentionally exclude lifecycle behavior and collection hygiene. "
            f"Separately, strict quality-bar matching found {counts['true_positives']} of "
            f"{expected} expected {expected_label}; {counts['false_negatives']} did not "
            "receive a strict match."
        )
    trust = (
        f"The assessment covered {counts['completed_scenarios']} of "
        f"{counts['active_scenarios']} planned scenarios. "
    )
    if actuals["trust_failures"] or counts["structural_failures"]:
        trust_issues = list(actuals["trust_failures"])
        if counts["structural_failures"]:
            trust_issues.append(
                f"{counts['structural_failures']} structural failure"
                f"{'s' if counts['structural_failures'] != 1 else ''}"
            )
        trust += "Trust is limited by " + ", ".join(trust_issues) + "."
    else:
        trust += (
            "No structural, provenance, privacy, judge-schema, or unresolved trust "
            "failure was recorded, but any human-validation items below still require review."
        )
    values = [f"{assessment} {_quality_conclusion(report['status'])}", trust]
    partial = [
        item
        for item in report["field_judgments"]
        if _content_utility_grade(item) == "partially_useful"
    ]
    if partial:
        root_match_classification_gaps = sum(
            item["attributes"]["root_cause"]
            and (
                not item["attributes"]["category"]
                or not item["attributes"]["severity"]
            )
            for item in partial
        )
        detail = (
            " Partially useful cards remain visible as customer-useful diagnostic signal "
            "but do not count as strict true positives."
        )
        if root_match_classification_gaps:
            detail += (
                f" {root_match_classification_gaps} matched the expected root cause but "
                f"failed category or severity correctness."
            )
        values.append(detail.strip())
    return values


def _grade_rows(report: dict[str, Any]) -> list[tuple[str, str]]:
    if report["status"] == "INCONCLUSIVE":
        counts = report["scorecard"]["counts"]
        return [
            ("Overall judgment", "INCONCLUSIVE"),
            (
                "Completed scenarios",
                f"{counts['completed_scenarios']} of {counts['active_scenarios']}",
            ),
            ("Expected findings", "N/A"),
            ("Observed findings", "N/A"),
        ]
    grades = _finding_grades(report)
    return [
        ("Fully correct (content utility)", str(grades["correct"])),
        ("Partially useful (content utility)", str(grades["partially_useful"])),
        ("Incorrect/noisy (content utility)", str(grades["incorrect"])),
    ]


def _assessment_scope(report: dict[str, Any]) -> str:
    run_agents = {
        (result["run_id"], result["agent_id"])
        for result in report["scenario_results"]
    }
    return (
        f"Data source: canonical report {report['report_id']}, generated "
        f"{report['generated_at']}; {len(report['scenario_results'])} immutable scenario "
        f"results across {len(run_agents)} run/agent evaluations and "
        f"{len(report['agents'])} synthetic test agents."
    )


def _useful_scenario_examples(report: dict[str, Any]) -> str:
    titles = {
        scenario["scenario_id"]: scenario["title"]
        for checklist in report.get("human_validation_checklists", [])
        for version in checklist["versions"]
        for scenario in version["expected_scenarios"]
    }
    scenario_ids = list(
        dict.fromkeys(
            item["scenario_id"]
            for item in report["field_judgments"]
            if _content_utility_grade(item)
            in {"fully_correct", "partially_useful"}
        )
    )
    if not scenario_ids:
        return ""
    examples = [titles.get(scenario_id, scenario_id) for scenario_id in scenario_ids[:4]]
    suffix = (
        f", and {len(scenario_ids) - len(examples)} more"
        if len(scenario_ids) > len(examples)
        else ""
    )
    return "; ".join(examples) + suffix


def _working_capabilities(report: dict[str, Any]) -> list[tuple[str, str]]:
    score = report["scorecard"]
    counts = score["counts"]
    actuals = (report.get("bar_definition") or derive_bar_definition(report))["actuals"]
    grades = _finding_grades(report)
    if report["status"] == "INCONCLUSIVE":
        return [
            (
                "Assessment unavailable",
                "No customer-facing capability claim is supported because the evidence set "
                "is incomplete.",
            )
        ]

    rows: list[tuple[str, str]] = []
    observed = actuals["observed_findings"]
    useful = grades["correct"] + grades["partially_useful"]
    if useful:
        examples = _useful_scenario_examples(report)
        example_detail = f" Evidence covered {examples}." if examples else ""
        rows.append(
            (
                "Useful diagnostic signal",
                f"{useful} of {observed} observed cards contained useful signal: "
                f"{grades['correct']} met the strict quality bar and "
                f"{grades['partially_useful']} were partially useful.{example_detail}",
            )
        )
    if (
        actuals["expected_findings"] > 0
        and counts["false_negatives"] == 0
        and actuals["overall_recall"] >= 0.90
    ):
        rows.append(
            (
                "Problem detection",
                f"All {actuals['expected_findings']} expected problems were detected; "
                f"overall recall was {actuals['overall_recall']:.1%}.",
            )
        )
    healthy_scenarios = sum(
        item["expected_count"] == 0 for item in report["scenario_results"]
    )
    if healthy_scenarios and counts["healthy_insights"] == 0:
        rows.append(
            (
                "Healthy-agent restraint",
                f"{healthy_scenarios} healthy-control scenarios produced 0 insight cards.",
            )
        )
    if grades["correct"] and actuals["required_field_correctness"] == 1:
        rows.append(
            (
                "Finding content",
                f"All {grades['correct']} fully correct findings passed required title, "
                "description, fix, category, severity, trace, localization, meaningfulness, "
                "and actionability checks.",
            )
        )
    collection = report["collection_analysis"]
    if observed and not any(
        collection[name] for name in ("duplicates", "fragments", "umbrellas", "stale_version")
    ):
        rows.append(
            (
                "Finding separation",
                f"Across {observed} cards, analysis found 0 duplicate, fragment, umbrella, "
                "or stale-version relationships.",
            )
        )
    elif observed and collection["duplicates"] == 0 and collection["stale_version"] == 0:
        rows.append(
            (
                "Duplicate and version control",
                f"Across {observed} cards, analysis found 0 duplicate and 0 stale-version "
                "relationships.",
            )
        )
    return rows or [
        (
            "Confirmed strengths",
            "No customer-facing capability had enough passing evidence to claim as a strength.",
        )
    ]


def _scenario_agent_ids(report: dict[str, Any]) -> dict[str, set[str]]:
    values: dict[str, set[str]] = defaultdict(set)
    for result in report["scenario_results"]:
        values[result["scenario_id"]].add(result["agent_id"])
    return values


def _agent_ids_for_judgments(
    report: dict[str, Any],
    predicate: Any,
) -> set[str]:
    by_scenario = _scenario_agent_ids(report)
    return {
        agent_id
        for judgment in report["field_judgments"]
        if predicate(judgment)
        for agent_id in by_scenario.get(judgment["scenario_id"], set())
    }


def _count_mismatch_agent_ids(report: dict[str, Any]) -> set[str]:
    expected: dict[tuple[str, str], int] = defaultdict(int)
    observed: dict[tuple[str, str], set[str]] = defaultdict(set)
    for result in report["scenario_results"]:
        key = (result["run_id"], result["agent_id"])
        expected[key] += result["expected_count"]
        observed[key].update(result["insight_references"])
    return {
        agent_id
        for (run_id, agent_id), expected_count in expected.items()
        if len(observed[(run_id, agent_id)]) != expected_count
    }


def _failure_agent_ids(report: dict[str, Any]) -> set[str]:
    agents = {agent["id"]: agent["name"] for agent in report["agents"]}
    names = {name: agent_id for agent_id, name in agents.items()}
    affected = set()
    if report["failure"] is not None:
        for value in report["failure"]["affected_agents"]:
            if value in agents:
                affected.add(value)
            elif value in names:
                affected.add(names[value])
    affected.update(
        result["agent_id"]
        for result in report["scenario_results"]
        if not result["completed"] or result["verdict"] == "inconclusive"
    )
    return affected or set(agents)


def _affected_agent_label(
    report: dict[str, Any],
    agent_ids: set[str],
    *,
    aggregate_all: bool = False,
) -> str:
    if aggregate_all:
        return "All test agents"
    all_agent_ids = {agent["id"] for agent in report["agents"]}
    if agent_ids and agent_ids == all_agent_ids:
        return "All test agents"
    names = sorted(
        {
            agent["name"]
            for agent in report["agents"]
            if agent["id"] in agent_ids
        },
        key=lambda value: (value.casefold(), value),
    )
    if names:
        return ", ".join(names)
    return "not retained in this historical report"


def _affected_evidence(
    report: dict[str, Any],
    agent_ids: set[str],
    evidence: str,
    *,
    aggregate_all: bool = False,
) -> str:
    return (
        "Affected test agents: "
        + _affected_agent_label(
            report,
            agent_ids,
            aggregate_all=aggregate_all,
        )
        + ". "
        + evidence
    )


def _root_cause_analysis(report: dict[str, Any]) -> dict[str, int]:
    actuals = (report.get("bar_definition") or derive_bar_definition(report))["actuals"]
    root_correct = [
        item
        for item in report["field_judgments"]
        if item["attributes"]["root_cause"]
    ]
    silent_misses = sum(
        result["expected_count"]
        for result in report["scenario_results"]
        if result["expected_count"] > 0 and result["observed_count"] == 0
    )
    expected_with_output = max(0, actuals["expected_findings"] - silent_misses)
    root_correct_expected = min(
        expected_with_output,
        len({item["scenario_id"] for item in root_correct}),
    )
    return {
        "root_correct_cards": len(root_correct),
        "root_correct_expected_roots": root_correct_expected,
        "silent_misses": silent_misses,
        "expected_with_output": expected_with_output,
        "output_without_root_match": max(
            0,
            expected_with_output - root_correct_expected,
        ),
    }


def _per_agent_assessment_rows(
    report: dict[str, Any],
) -> list[tuple[str, int, int, int, int, int, int, int]]:
    by_scenario = _scenario_agent_ids(report)
    judgments_by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for judgment in report["field_judgments"]:
        for agent_id in by_scenario.get(judgment["scenario_id"], set()):
            judgments_by_agent[agent_id].append(judgment)
    rows = []
    for agent in _ordered_agents(report):
        results = [
            item
            for item in report["scenario_results"]
            if item["agent_id"] == agent["id"]
        ]
        judgments = judgments_by_agent[agent["id"]]
        observed = {
            (item["run_id"], reference)
            for item in results
            for reference in item["insight_references"]
        }
        healthy_noise = {
            (item["run_id"], reference)
            for item in results
            if item["expected_count"] == 0
            for reference in item["insight_references"]
        }
        grades = [_content_utility_grade(item) for item in judgments]
        rows.append(
            (
                agent["name"],
                sum(item["expected_count"] for item in results),
                len(observed),
                sum(
                    item["expected_count"]
                    for item in results
                    if item["expected_count"] > 0 and item["observed_count"] == 0
                ),
                sum(item["attributes"]["root_cause"] for item in judgments),
                grades.count("partially_useful"),
                grades.count("incorrect_noise"),
                len(healthy_noise),
            )
        )
    return rows


def _improvement_rows(report: dict[str, Any]) -> list[tuple[str, str, str]]:
    score = report["scorecard"]
    counts = score["counts"]
    rates = score["rates"]
    actuals = (report.get("bar_definition") or derive_bar_definition(report))["actuals"]
    grades = _finding_grades(report)
    if report["status"] == "INCONCLUSIVE":
        reason = report["failure"]["reason"] if report["failure"] is not None else "Unknown."
        return [
            (
                "Assessment completeness",
                _affected_evidence(
                    report,
                    _failure_agent_ids(report),
                    f"Only {counts['completed_scenarios']} of "
                    f"{counts['active_scenarios']} scenarios completed. {reason}",
                    aggregate_all=not (
                        report["failure"] is not None
                        and report["failure"]["affected_agents"]
                    ),
                ),
                "Complete every planned scenario with validated evidence before drawing or "
                "promoting a quality conclusion.",
            )
        ]
    if report["status"] == "AT BAR":
        return [
            (
                "No product-quality gap observed",
                "Affected test agents: none. "
                f"{actuals['expected_findings']} findings were expected and "
                f"{actuals['observed_findings']} were observed, with "
                f"{counts['false_negatives']} missed and {counts['false_positives']} noisy.",
                "Continue to detect each expected problem once, suppress healthy-agent noise, "
                "and preserve complete trace and version grounding.",
            )
        ]

    rows: list[tuple[str, str, str]] = []
    if counts["false_negatives"] or rates["high_severity_recall"] < 1:
        roots = _root_cause_analysis(report)
        affected = {
            result["agent_id"]
            for result in report["scenario_results"]
            if result["expected_count"] > 0 and result["verdict"] != "correct"
        }
        rows.append(
            (
                "Expected roots lacked a strict match",
                _affected_evidence(
                    report,
                    affected,
                    f"{roots['silent_misses']} of {actuals['expected_findings']} expected "
                    f"roots were true silent misses with no card. "
                    f"{roots['expected_with_output']} expected roots had card output, but "
                    f"{roots['output_without_root_match']} had no root-cause-correct match; "
                    f"{roots['root_correct_expected_roots']} root had a matching card that "
                    "still failed other required content fields. "
                    f"Strict recall was {rates['overall_recall']:.1%}.",
                ),
                "Detect every high-severity problem and at least 90% of all expected problems "
                "with the correct root cause.",
            )
        )
    if grades["incorrect"] or grades["partially_useful"] or counts["healthy_insights"]:
        affected = _agent_ids_for_judgments(
            report,
            lambda item: _content_utility_grade(item) != "fully_correct",
        )
        affected.update(
            result["agent_id"]
            for result in report["scenario_results"]
            if result["observed_count"] > 0 and result["verdict"] != "correct"
        )
        healthy = (
            f" {counts['healthy_insights']} "
            f"{'card came' if counts['healthy_insights'] == 1 else 'cards came'} "
            "from healthy controls."
            if counts["healthy_insights"]
            else ""
        )
        rows.append(
            (
                "Incorrect and ambiguous findings",
                _affected_evidence(
                    report,
                    affected,
                    f"Of {actuals['observed_findings']} observed cards, "
                    f"{grades['incorrect']} were incorrect/noisy and "
                    f"{grades['partially_useful']} were only partially useful; "
                    f"strict quality-bar precision was {rates['precision']:.1%}.{healthy}",
                ),
                "Return no card for healthy behavior and ground each finding in the complete "
                "trace, request, available tools, and current agent version.",
            )
        )
    if actuals["count_mismatch_runs"]:
        rows.append(
            (
                "Finding count did not match root causes",
                _affected_evidence(
                    report,
                    _count_mismatch_agent_ids(report),
                    f"{actuals['count_mismatch_runs']} run/agent results had count "
                    f"mismatches; {actuals['expected_findings']} findings were expected "
                    f"and {actuals['observed_findings']} were observed.",
                ),
                "Produce exactly one clearly scoped finding per independently fixable root "
                "cause in each run.",
            )
        )
    failed_fields = [
        (name.replace("_", " "), rates[name])
        for name in (
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
        if rates[name] < 1
    ]
    if failed_fields:
        field_rate_denominator = grades["correct"] + grades["partially_useful"]
        affected = _agent_ids_for_judgments(
            report,
            lambda item: not all(item["attributes"].values()),
        )
        rows.append(
            (
                "Finding content was incomplete or inaccurate",
                _affected_evidence(
                    report,
                    affected,
                    f"Across {field_rate_denominator} mapped cards with fully or partially "
                    "useful content (the scorecard attribute-rate denominator), "
                    + ", ".join(
                        f"{name} passed {rate:.1%}" for name, rate in failed_fields
                    )
                    + ".",
                ),
                "Make every title, explanation, severity, category, trace link, and proposed "
                "fix specific, correct, localized, meaningful, and actionable.",
            )
        )
    collection = report["collection_analysis"]
    relationship_counts = [
        (label, collection[name])
        for label, name in (
            ("duplicate", "duplicates"),
            ("fragment", "fragments"),
            ("umbrella", "umbrellas"),
            ("stale-version", "stale_version"),
        )
        if collection[name]
    ]
    relationship_rates = [
        (label, rates[name])
        for label, name in (
            ("duplicate", "duplication_rate"),
            ("fragment", "fragmentation_rate"),
            ("umbrella", "umbrella_rate"),
            ("stale-version", "cross_version_stale_rate"),
        )
        if rates[name] > 0
    ]
    if relationship_counts or relationship_rates:
        evidence = (
            "Analysis found "
            + " and ".join(
                f"{count} {label} relationship"
                f"{'s' if count != 1 else ''}"
                for label, count in relationship_counts
            )
            + "."
            if relationship_counts
            else "Relationship analysis measured "
            + ", ".join(f"{label} {rate:.1%}" for label, rate in relationship_rates)
            + "."
        )
        rows.append(
            (
                "Related findings were not cleanly separated",
                _affected_evidence(
                    report,
                    _agent_ids_for_judgments(
                        report,
                        lambda item: any(item["relationships"].values())
                        or item["stale_version"],
                    ),
                    evidence,
                ),
                "Group evidence by root cause, avoid duplicate or fragmented cards, and scope "
                "each finding to the immutable agent version where it reproduces.",
            )
        )
    if "capability_fix_mismatch" in score["violations"]:
        rows.append(
            (
                "Proposed fixes assumed unavailable capabilities",
                _affected_evidence(
                    report,
                    _agent_ids_for_judgments(
                        report,
                        lambda item: not item["fix_verifiable"]
                        or not item["attributes"]["proposed_fix"],
                    ),
                    "One or more proposed fixes referenced a capability outside the "
                    "deployed agent contract.",
                ),
                "Generate remediation only from the tools, models, and integrations configured "
                "for the evaluated agent.",
            )
        )
    if actuals["trust_failures"] or counts["structural_failures"]:
        rows.append(
            (
                "Assessment trust was reduced",
                _affected_evidence(
                    report,
                    set(),
                    f"{counts['structural_failures']} structural failures and "
                    f"{len(actuals['trust_failures'])} recorded trust failures affected "
                    "the run.",
                    aggregate_all=True,
                ),
                "Fail closed until evidence structure, provenance, privacy, judging, and "
                "classification are complete and trustworthy.",
            )
        )
    return rows[:6]


def _bug_action_status(report: dict[str, Any]) -> str | None:
    mutation_count = sum(
        action["action"] in {"created", "updated", "reopened", "commented"}
        for action in report["bug_actions"]
    )
    candidate_count = sum(
        action["action"] == "candidate" for action in report["bug_actions"]
    )
    if candidate_count and mutation_count:
        return (
            f"{candidate_count} bug candidate"
            f"{'s' if candidate_count != 1 else ''} prepared; "
            f"{mutation_count} private bug action"
            f"{'s were' if mutation_count != 1 else ' was'} confirmed by apply receipts."
        )
    if candidate_count:
        return (
            f"{candidate_count} bug candidate"
            f"{'s' if candidate_count != 1 else ''} prepared; no work-item mutation was claimed."
        )
    if mutation_count:
        return (
            f"{mutation_count} private bug action"
            f"{'s were' if mutation_count != 1 else ' was'} confirmed by apply receipts."
        )
    return None


def _markdown_cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def _ordered_agents(report: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        report["agents"],
        key=lambda agent: (agent["name"].casefold(), agent["name"]),
    )


def _recommend_human_validation(
    report: dict[str, Any],
    agent: dict[str, Any],
) -> bool:
    scenario_ids = {
        result["scenario_id"]
        for result in report["scenario_results"]
        if result["agent_id"] == agent["id"]
    }
    results = [
        result
        for result in report["scenario_results"]
        if result["agent_id"] == agent["id"]
    ]
    judgments = [
        item
        for item in report["field_judgments"]
        if item["scenario_id"] in scenario_ids
    ]
    return (
        agent["human_validation"] != "N/A"
        or any(
            not result["completed"] or result["verdict"] != "correct"
            for result in results
        )
        or any(
            item["verdict"] != "correct"
            or not all(item["attributes"].values())
            or any(item["relationships"].values())
            or item["stale_version"]
            or not item["fix_verifiable"]
            or (
                item["verifier_verdict"] is not None
                and item["verifier_verdict"] != item["verdict"]
            )
            for item in judgments
        )
    )


def _validated_insight_evaluations(
    report: dict[str, Any],
    insight_evaluations: dict[str, Any] | None,
) -> list[dict[str, str]]:
    if insight_evaluations is None:
        return []
    if set(insight_evaluations) != {"schema_version", "report_id", "cards"}:
        raise ContractError("Insight evaluation sidecar has unexpected fields")
    if (
        insight_evaluations["schema_version"] != "1.0.0"
        or insight_evaluations["report_id"] != report["report_id"]
        or not isinstance(insight_evaluations["cards"], list)
    ):
        raise ContractError("Insight evaluation sidecar does not match the report")
    agent_ids = {agent["id"] for agent in report["agents"]}
    cards = []
    required = {
        "agent_id",
        "agent_name",
        "category",
        "title",
        "description",
        "evaluation",
        "evaluation_result",
    }
    for card in insight_evaluations["cards"]:
        if not isinstance(card, dict) or set(card) != required:
            raise ContractError("Insight evaluation card has unexpected fields")
        if (
            card["agent_id"] not in agent_ids
            or not isinstance(card["agent_name"], str)
            or not card["agent_name"].startswith(card["agent_id"] + "-")
            or card["evaluation"]
            not in {"fully_correct", "partially_useful", "incorrect_noise"}
        ):
            raise ContractError("Insight evaluation card identity or grade is invalid")
        for name in (
            "agent_name",
            "category",
            "title",
            "description",
            "evaluation_result",
        ):
            if not isinstance(card[name], str) or not card[name].strip():
                raise ContractError(f"Insight evaluation card {name} is required")
        if (
            len(card["agent_name"]) > 200
            or len(card["category"]) > 100
            or len(card["title"]) > 300
            or len(card["description"]) > 3000
            or len(card["evaluation_result"]) > 2000
        ):
            raise ContractError("Insight evaluation card text exceeds the public report bound")
        cards.append(card)
    actuals = (report.get("bar_definition") or derive_bar_definition(report))["actuals"]
    grades = _finding_grades(report)
    if len(cards) != actuals["observed_findings"] or {
        grade: sum(card["evaluation"] == grade for card in cards)
        for grade in ("fully_correct", "partially_useful", "incorrect_noise")
    } != {
        "fully_correct": grades["correct"],
        "partially_useful": grades["partially_useful"],
        "incorrect_noise": grades["incorrect"],
    }:
        raise ContractError(
            "Insight evaluation sidecar counts contradict the canonical report"
        )
    return sorted(
        cards,
        key=lambda card: (
            card["agent_name"].casefold(),
            card["agent_name"],
            card["category"].casefold(),
            card["title"].casefold(),
            card["title"],
        ),
    )


def render_report_markdown(
    report: dict[str, Any],
    insight_evaluations: dict[str, Any] | None = None,
) -> str:
    validate_report_consistency(report)
    score = report["scorecard"]
    counts = score["counts"]
    rates = score["rates"]
    bar = report.get("bar_definition") or derive_bar_definition(report)
    actuals = bar["actuals"]
    lines = [
        f"# Agent Insights Quality Report - {report['report_date']}",
        "",
        f"- Report: `{report['report_id']}`",
        f"- Status: **{report['status']}**",
        f"- Engine: `{report['engine']['build']}` / `{report['engine']['generator_model']}`",
        f"- Complete: `{str(score['complete']).lower()}`",
        "",
        "## Summary",
        "",
    ]
    lines.extend(_summary_narrative(report))
    lines.extend(["", _assessment_scope(report)])
    lines.extend(
        [
        "",
        "| Grade | Findings |",
        "| --- | ---: |",
        ]
    )
    lines.extend(f"| {grade} | {finding} |" for grade, finding in _grade_rows(report))
    lines.extend(
        [
        "",
        "## What is working",
        "",
        "| Capability | Evidence |",
        "| --- | --- |",
        ]
    )
    lines.extend(
        f"| {_markdown_cell(capability)} | {_markdown_cell(evidence)} |"
        for capability, evidence in _working_capabilities(report)
    )
    lines.extend(
        [
        "",
        "## What needs improvement",
        "",
        "| Product gap | What happened | Needed behavior |",
        "| --- | --- | --- |",
        ]
    )
    lines.extend(
        f"| {_markdown_cell(gap)} | {_markdown_cell(happened)} | "
        f"{_markdown_cell(needed)} |"
        for gap, happened, needed in _improvement_rows(report)
    )
    action_status = _bug_action_status(report)
    if action_status:
        lines.extend(["", f"**Follow-up:** {action_status}"])
    lines.extend(
        [
        "",
        "## Detailed assessment analysis",
        "",
        ]
    )
    root_analysis = _root_cause_analysis(report)
    lines.extend(
        [
        f"- Expected roots: {actuals['expected_findings']}; observed physical cards: "
        f"{actuals['observed_findings']}; strict true positives: "
        f"{counts['true_positives']}.",
        f"- Root-cause-correct cards: {root_analysis['root_correct_cards']} of "
        f"{actuals['observed_findings']}; expected roots with a root-cause-correct "
        f"match: {root_analysis['root_correct_expected_roots']} of "
        f"{actuals['expected_findings']}.",
        f"- True silent misses: {root_analysis['silent_misses']} expected roots produced "
        "no card. Of the remaining "
        f"{root_analysis['expected_with_output']} expected roots with card output, "
        f"{root_analysis['output_without_root_match']} had no root-cause-correct match.",
        "- Observed-card utility grades exclude lifecycle and collection hygiene. "
        "Lifecycle continuity/staleness and exact duplicate, fragment, and umbrella "
        "relationships remain separate gates.",
        "",
        ]
    )
    utility_judgments = [
        item
        for item in report["field_judgments"]
        if _content_utility_grade(item) == "partially_useful"
    ]
    if utility_judgments:
        lines.extend(
            [
                "### Partially useful content attribute passes",
                "",
                "| Attribute | Passed | Evaluated |",
                "| --- | ---: | ---: |",
            ]
        )
        for attribute in (
            "root_cause",
            "title",
            "description",
            "category",
            "severity",
            "proposed_fix",
            "linked_traces",
            "evidence_localization",
            "meaningfulness",
            "actionability",
        ):
            lines.append(
                f"| {attribute.replace('_', ' ').title()} | "
                f"{sum(item['attributes'][attribute] for item in utility_judgments)} | "
                f"{len(utility_judgments)} |"
            )
        lines.append("")
    lines.extend(
        [
            "### Per-agent assessment",
            "",
            "| Test agent | Expected roots | Observed cards | Silent misses | "
            "Root-correct cards | Partially useful | Incorrect/noisy | Healthy-control noise |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in _per_agent_assessment_rows(report):
        lines.append(
            f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} | "
            f"{row[5]} | {row[6]} | {row[7]} |"
        )
    evaluation_cards = _validated_insight_evaluations(report, insight_evaluations)
    if evaluation_cards:
        lines.extend(
            [
                "",
                "## Per-agent generated insight evaluation",
                "",
                "This sanitized presentation lists actual generated category, title, and "
                "description content. Utility grading is lifecycle-neutral; lifecycle and "
                "collection hygiene remain separate.",
                "",
            ]
        )
        current_agent = None
        for card in evaluation_cards:
            if card["agent_name"] != current_agent:
                if current_agent is not None:
                    lines.append("")
                current_agent = card["agent_name"]
                lines.extend(
                    [
                        f"### {card['agent_name']}",
                        "",
                        "| Category | Title | Description | Evaluation result |",
                        "| --- | --- | --- | --- |",
                    ]
                )
            lines.append(
                f"| {_markdown_cell(card['category'])} | "
                f"{_markdown_cell(card['title'])} | "
                f"{_markdown_cell(card['description'])} | "
                f"{_markdown_cell(card['evaluation_result'])} |"
            )
    lines.extend(
        [
            "",
            "## Quality bar and result",
            "",
            "AT BAR requires exact expected-versus-observed cards for every run and agent; "
            "at least 90% recall and 95% precision; 100% required-field correctness on "
            "accepted true positives; zero duplicate, fragment, umbrella, stale-version, and "
            "healthy-control cards; and no structural, provenance, PII, judge-schema, or "
            "unresolved trust failure.",
            "",
            f"**Result: {report['status']}.** Expected {actuals['expected_findings']} findings; "
            f"observed {actuals['observed_findings']}. Recall was "
            f"{actuals['overall_recall']:.1%}, precision was {actuals['precision']:.1%}, and "
            f"required-field correctness was {actuals['required_field_correctness']:.1%}.",
        ]
    )
    lines.extend(
        [
        "",
            "## Numeric scorecard",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
        ]
    )
    for name, value in counts.items():
        lines.append(f"| {name.replace('_', ' ').title()} | {value} |")
    lines.append(
        f"| Expected Findings | {counts['true_positives'] + counts['false_negatives']} |"
    )
    lines.append(
        f"| Observed Findings | {counts['true_positives'] + counts['false_positives']} |"
    )
    for name, value in rates.items():
        lines.append(f"| {name.replace('_', ' ').title()} | {value:.3f} |")
    lines.extend(
        [
            "",
            "## Gate violations",
            "",
            ", ".join(f"`{item}`" for item in score["violations"]) or "None.",
            "",
            "## Scenario results",
            "",
            "| Scenario | Agent | Completed | Expected | Observed | Canonical verdict | Insights |",
            "| --- | --- | --- | ---: | ---: | --- | ---: |",
        ]
    )
    for result in report["scenario_results"]:
        lines.append(
            f"| `{result['scenario_id']}` | `{result['agent_id']}` | "
            f"{result['completed']} | {result['expected_count']} | "
            f"{result['observed_count']} | {result['verdict']} | "
            f"{len(result['insight_references'])} |"
        )
    lines.extend(
        [
            "",
            "## Source field judgments",
            "",
            "Canonical verdicts remain unchanged for audit. The leadership observed-card utility "
            "grade is recomputed from content attributes and excludes lifecycle and collection "
            "relationships.",
            "",
            "| Scenario | Insight | Attribute results |",
            "| --- | --- | --- |",
        ]
    )
    for judgment in report["field_judgments"]:
        attributes = ", ".join(
            f"{name}={'pass' if passed else 'fail'}"
            for name, passed in sorted(judgment["attributes"].items())
        )
        lines.append(
            f"| `{judgment['scenario_id']}` | `{judgment['insight_reference']}` | "
            f"{attributes} |"
        )
    collection = report["collection_analysis"]
    diagnostics = report["diagnostics"]
    lines.extend(
        [
            "",
            "## Lifecycle and collection hygiene",
            "",
            "Lifecycle continuity/staleness and duplicate, fragment, or umbrella relationships "
            "are evaluated as separate quality gates and never change the observed-card "
            "content-utility grade.",
            "",
            "| Distinct | Duplicates | Fragments | Umbrellas | Stale version |",
            "| ---: | ---: | ---: | ---: | ---: |",
            f"| {collection['distinct']} | {collection['duplicates']} | "
            f"{collection['fragments']} | {collection['umbrellas']} | "
            f"{collection['stale_version']} |",
            "",
            "## Efficiency diagnostics",
            "",
            "| Engine latency ms | Model calls | Tokens |",
            "| ---: | ---: | ---: |",
            f"| {diagnostics['engine_latency_ms'] if diagnostics['engine_latency_ms'] is not None else 'N/A'} | "
            f"{diagnostics['model_calls'] if diagnostics['model_calls'] is not None else 'N/A'} | "
            f"{diagnostics['tokens'] if diagnostics['tokens'] is not None else 'N/A'} |",
        ]
    )
    if report.get("schema_version") == STRUCTURED_REPORT_VERSION:
        lines.extend(["", "## Human validation one-pager", ""])
        agents = {agent["id"]: agent for agent in report["agents"]}
        checklists = {
            item["agent_id"]: item
            for item in report["human_validation_checklists"]
        }
        for agent in _ordered_agents(report):
            checklist = checklists[agent["id"]]
            agent = agents[checklist["agent_id"]]
            scenario_count = len(
                {
                    scenario["scenario_id"]
                    for version in checklist["versions"]
                    for scenario in version["expected_scenarios"]
                }
            )
            lines.extend(
                [
                    f"### {agent['name']} (`{agent['id']}`)",
                    "",
                    "**Test-agent description:** "
                    f"Synthetic `{agent['type']}` test agent evaluated across "
                    f"{len(checklist['versions'])} immutable versions and "
                    f"{scenario_count} reviewed injected scenarios.",
                    "",
                    "**Recommend human validation:** "
                    + (
                        "Yes"
                        if _recommend_human_validation(report, agent)
                        else "No"
                    ),
                    "",
                    f"**Review reason:** {checklist['review_reason']}",
                    "",
                    "| Run / immutable version | Injected issue(s) | Expected insight(s) | "
                    "Observed final cards | Human-validation guidance |",
                    "| --- | --- | --- | --- | --- |",
                ]
            )
            for version in checklist["versions"]:
                injected = "<br>".join(
                    f"`{item['scenario_id']}` {item['title']}: {item['root_cause']}"
                    for item in version["expected_scenarios"]
                )
                expected = "<br>".join(
                    f"{item['expected_insight_count']} expected; "
                    f"{item['category']} / {item['severity']}"
                    for item in version["expected_scenarios"]
                )
                observed = (
                    "<br>".join(
                        f"`{card['insight_reference']}` "
                        f"({card['verdict']} canonical verdict)"
                        for card in version["observed_final_cards"]
                    )
                    or "None"
                )
                lines.append(
                    f"| `{version['run_id']}` / {version['phase']} "
                    f"`{version['version_digest']}` | {injected} | "
                    f"Total {version['expected_insight_count']} expected<br>{expected} | "
                    f"{observed} | {version['double_check']} |"
                )
            lines.extend(["", "**Standard checklist**", ""])
            lines.extend(f"- [ ] {item}" for item in checklist["standard_checks"])
            lines.extend(
                [
                    f"- Human outcome: `{checklist['human_outcome']}`",
                    "",
                ]
            )
    lines.extend(
        [
            "",
            "## Test agents",
            "",
            "| Agent | Type | Insights reference | Human validation |",
            "| --- | --- | --- | --- |",
        ]
    )
    for agent in _ordered_agents(report):
        lines.append(
            f"| `{agent['id']}` | `{agent['type']}` | "
            f"`{agent['insights_reference']}` | {agent['human_validation']} |"
        )
    lines.extend(
        [
            "",
            "## Memory changes",
            "",
            "| Fingerprint | From | To |",
            "| --- | --- | --- |",
        ]
    )
    for change in report["memory_changes"]:
        lines.append(
            f"| `{change['fingerprint']}` | {change['from'] or 'N/A'} | {change['to']} |"
        )
    lines.extend(
        [
            "",
            "## Bug actions",
            "",
            "| Fingerprint | Action | Work item reference |",
            "| --- | --- | --- |",
        ]
    )
    for action in report["bug_actions"]:
        lines.append(
            f"| `{action['fingerprint']}` | {action['action']} | "
            f"{action['work_item_reference'] or 'N/A'} |"
        )
    return "\n".join(lines + [""])


def render_trend(reports: list[dict[str, Any]], *, limit: int = 14) -> dict[str, Any]:
    if limit < 1 or limit > 90:
        raise ContractError("Trend limit must be between 1 and 90")
    unique: dict[str, dict[str, Any]] = {}
    for report in reports:
        validate_report_consistency(report, validate_catalog=False)
        unique[report["report_date"]] = report
    selected = [unique[key] for key in sorted(unique)[-limit:]]
    trend = {
        "schema_version": "1.0.0",
        "days": [
            {
                "report_date": report["report_date"],
                "status": report["status"],
                "trusted_insight_rate": (
                    None
                    if report["status"] == "INCONCLUSIVE"
                    else report["scorecard"]["rates"]["precision"]
                ),
                "report_path": (
                    "reports/daily/"
                    + report["report_date"].replace("-", "/")
                    + (
                        f"/{report['report_id']}"
                        if report["report_id"]
                        != f"aiq-{report['report_date'].replace('-', '')}"
                        else ""
                    )
                    + "/report.md"
                ),
            }
            for report in selected
        ],
    }
    validate_instance(trend, SCHEMAS / "trend.schema.json", "trend")
    return trend


def resolve_recipient(
    config: dict[str, Any],
    environ: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    """Resolve Actions addresses or a test-mode authenticated-user mailbox handoff."""
    environ = environ if environ is not None else os.environ
    if config.get("mode") not in {"test", "production"}:
        raise ContractError("Reporting mode must be test or production")
    variables = config.get("recipient_variables")
    if variables is not None and config.get("recipient_variable") != variables.get(config["mode"]):
        raise ContractError("Reporting recipient variable does not match the selected mode")
    variable = config["recipient_variable"]
    address = environ.get(variable)
    if not address and config["mode"] == "test":
        return {
            "mode": "authenticated_user",
            "address": None,
            "source": "connected_microsoft_mailbox",
        }
    if not address:
        raise ContractError(f"Protected recipient variable is not available: {variable}")
    match = _EMAIL.fullmatch(address)
    if not match or match.group(1).casefold() != config["allowed_domain"].casefold():
        raise ContractError("Reporting recipient is outside the configured allowed domain")
    return {"mode": "address", "address": address, "source": variable}


_STATUS_STYLES = {
    "AT BAR": {
        "background": "#e6f4ea",
        "foreground": "#0b6a0b",
        "accent": "#107c10",
    },
    "NOT AT BAR": {
        "background": "#fde7e9",
        "foreground": "#a4262c",
        "accent": "#c50f1f",
    },
    "INCONCLUSIVE": {
        "background": "#fff4ce",
        "foreground": "#8a5700",
        "accent": "#d29200",
    },
}


def _section_heading(title: str) -> str:
    return (
        '<h2 style="margin:0 0 14px 0;color:#12304a;font-family:Segoe UI,Arial,'
        f'sans-serif;font-size:20px;line-height:26px;">{html.escape(title)}</h2>'
    )


def _data_table(
    headers: tuple[str, ...],
    rows: list[tuple[str, ...]],
    widths: tuple[int, ...],
) -> str:
    header = "".join(
        f'<th align="left" width="{width}%" style="padding:10px 12px;'
        f'border:1px solid #d6deea;color:#12304a;vertical-align:top;'
        f'{_OUTLOOK_TEXT_STYLE}font-weight:700;">'
        f"{html.escape(label)}</th>"
        for label, width in zip(headers, widths, strict=True)
    )
    body = "".join(
        "<tr>"
        + "".join(
            '<td style="padding:11px 12px;border:1px solid #d6deea;'
            f'color:#334155;vertical-align:top;{_OUTLOOK_TEXT_STYLE}">'
            f"{html.escape(value)}</td>"
            for value in row
        )
        + "</tr>"
        for row in rows
    )
    return (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="width:100%;border-collapse:collapse;{_OUTLOOK_TEXT_STYLE}">'
        f'<tr bgcolor="#e8eef7">{header}</tr>{body}</table>'
    )


def _private_data_source_table(
    report: dict[str, Any],
    context: RuntimeLinkContext,
) -> str:
    project_url = context.resource_route()
    return (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="width:100%;border-collapse:collapse;margin-top:18px;'
        f'{_OUTLOOK_TEXT_STYLE}">'
        '<tr bgcolor="#e8eef7">'
        '<th align="left" width="28%" style="padding:10px 12px;'
        f'border:1px solid #d6deea;color:#12304a;{_OUTLOOK_TEXT_STYLE}'
        'font-weight:700;">Data source</th>'
        '<th align="left" width="72%" style="padding:10px 12px;'
        f'border:1px solid #d6deea;color:#12304a;{_OUTLOOK_TEXT_STYLE}'
        'font-weight:700;">Resolved value</th></tr>'
        "<tr>"
        '<td style="padding:11px 12px;border:1px solid #d6deea;color:#334155;'
        f'vertical-align:top;{_OUTLOOK_TEXT_STYLE}">Foundry project</td>'
        '<td style="padding:11px 12px;border:1px solid #d6deea;color:#334155;'
        f'vertical-align:top;{_OUTLOOK_TEXT_STYLE}">'
        f"{html.escape(context.account)} / {html.escape(context.project)} &middot; "
        f'<a style="color:#0067b8;text-decoration:underline;font-weight:600;" '
        f'href="{html.escape(project_url, quote=True)}">Open authenticated project</a>'
        "</td></tr>"
        "<tr>"
        '<td style="padding:11px 12px;border:1px solid #d6deea;color:#334155;'
        f'vertical-align:top;{_OUTLOOK_TEXT_STYLE}">Canonical assessment</td>'
        '<td style="padding:11px 12px;border:1px solid #d6deea;color:#334155;'
        f'vertical-align:top;{_OUTLOOK_TEXT_STYLE}">'
        f"{html.escape(report['report_id'])} &middot; "
        f"{html.escape(report['report_date'])} &middot; generated "
        f"{html.escape(report['generated_at'])}</td></tr></table>"
    )


def _quality_conclusion(status: str) -> str:
    return {
        "AT BAR": (
            "Agent Insights met the strict daily quality bar with complete, "
            "validated evidence."
        ),
        "NOT AT BAR": (
            "Agent Insights did not meet the strict daily quality bar; the gaps "
            "below require attention."
        ),
        "INCONCLUSIVE": (
            "No quality conclusion can be made because the validated evidence "
            "set is incomplete."
        ),
    }[status]


def render_email_html(
    report: dict[str, Any],
    trend: dict[str, Any],
    agent_links: Mapping[str, str],
    expected_link_context: RuntimeLinkContext,
) -> tuple[str, str]:
    validate_report_consistency(report)
    validate_instance(trend, SCHEMAS / "trend.schema.json", "trend")
    current_rows = [
        day for day in trend["days"] if day["report_date"] == report["report_date"]
    ]
    expected_rate = (
        None
        if report["status"] == "INCONCLUSIVE"
        else report["scorecard"]["rates"]["precision"]
    )
    expected_path = (
        "reports/daily/"
        + report["report_date"].replace("-", "/")
        + (
            f"/{report['report_id']}"
            if report["report_id"] != f"aiq-{report['report_date'].replace('-', '')}"
            else ""
        )
        + "/report.md"
    )
    if (
        len(current_rows) != 1
        or current_rows[0]["status"] != report["status"]
        or current_rows[0]["trusted_insight_rate"] != expected_rate
        or current_rows[0]["report_path"] != expected_path
    ):
        raise ContractError("Current trend entry contradicts the canonical report")
    expected_agents = {agent["id"] for agent in report["agents"]}
    if set(agent_links) != expected_agents:
        raise ContractError("Direct email must contain a runtime agent page link for every agent")
    if expected_link_context.project != report["plan_id"]:
        raise ContractError("Runtime Agent Insights context does not match the report plan")
    for agent in report["agents"]:
        validate_agent_page_url(
            agent_links[agent["id"]], expected_link_context, agent["name"]
        )
    score = report["scorecard"]
    counts = score["counts"]
    signal = (
        f"{counts['new_issues']} new, {counts['regressed_issues']} regressed"
        if counts["new_issues"] or counts["regressed_issues"]
        else f"{counts['completed_scenarios']}/{counts['active_scenarios']} scenarios"
    )
    subject = (
        f"[Agent Insights Quality] {report['status']} - {report['report_date']} - {signal}"
    )
    summary = _summary_narrative(report)
    grade_rows = _grade_rows(report)
    working = _working_capabilities(report)
    improvements = _improvement_rows(report)
    action_status = _bug_action_status(report)
    status_style = _STATUS_STYLES[report["status"]]
    report_url = _PUBLIC_REPORT_BASE_URL + expected_path
    rows = []
    for agent in _ordered_agents(report):
        recommend = "Yes" if _recommend_human_validation(report, agent) else "No"
        rows.append(
            "<tr>"
            '<td style="padding:11px 12px;border:1px solid #d6deea;'
            'color:#1f2937;line-height:18px;">'
            f"<strong>{html.escape(agent['name'])}</strong><br>"
            '<span style="color:#64748b;font-size:12px;">'
            f"{html.escape(agent['id'])}</span></td>"
            '<td style="padding:11px 12px;border:1px solid #d6deea;'
            f'color:#334155;">{html.escape(agent["type"])}</td>'
            '<td style="padding:11px 12px;border:1px solid #d6deea;">'
            f'<a style="color:#0067b8;text-decoration:underline;font-weight:600;" '
            f'href="{html.escape(agent_links[agent["id"]], quote=True)}">'
            "Open agent</a></td>"
            '<td style="padding:11px 12px;border:1px solid #d6deea;'
            f'color:#334155;line-height:18px;font-weight:700;">{recommend}</td>'
            "</tr>"
        )
    body = (
        '<!doctype html><html><body bgcolor="#f3f6fa" '
        'style="margin:0;padding:0;background-color:#f3f6fa;font-family:Segoe UI,'
        'Arial,sans-serif;color:#1f2937;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="100%" bgcolor="#f3f6fa" style="width:100%;background-color:#f3f6fa;">'
        '<tr><td align="center" style="padding:24px 12px;">'
        "<!--[if mso]><table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" "
        "border=\"0\" width=\"1160\"><tr><td><![endif]-->"
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="100%" bgcolor="#ffffff" style="width:100%;max-width:1160px;'
        'background-color:#ffffff;border:1px solid #dfe6ef;border-collapse:collapse;">'
        '<tr><td bgcolor="#12304a" style="padding:34px 32px 30px 32px;'
        'background-color:#12304a;">'
        '<h1 style="margin:0 0 8px 0;color:#ffffff;font-family:Segoe UI,Arial,'
        'sans-serif;font-size:32px;line-height:39px;font-weight:700;">'
        "Agent Insights quality</h1>"
        '<p style="margin:0 0 14px 0;color:#dbeafe;font-size:17px;line-height:24px;">'
        f"Daily qualification report &middot; {html.escape(report['report_date'])}</p>"
        f'<span style="display:inline-block;padding:5px 10px;background-color:'
        f'{status_style["background"]};color:{status_style["foreground"]};'
        'font-size:12px;line-height:16px;font-weight:700;">'
        f"{html.escape(report['status'])}</span>"
        '<p style="margin:15px 0 0 0;color:#aebfd0;font-size:12px;line-height:18px;">'
        f"Report {html.escape(report['report_id'])} &middot; Build "
        f"{html.escape(report['engine']['build'])}</p>"
        "</td></tr>"
        '<tr><td style="padding:24px 32px 0 32px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="100%" bgcolor="#eaf4ff" style="width:100%;background-color:#eaf4ff;'
        'border-left:5px solid #0078d4;border-collapse:collapse;">'
        '<tr><td style="padding:18px 20px;color:#12304a;font-size:16px;line-height:24px;">'
        f"<strong>{html.escape(_quality_conclusion(report['status']))}</strong>"
        "</td></tr></table></td></tr>"
        '<tr><td style="padding:28px 32px 0 32px;">'
        + _section_heading(SECTION_TITLES[0])
        + "".join(
            f'<p style="margin:0 0 12px 0;color:#334155;{_OUTLOOK_TEXT_STYLE}">'
            f"{html.escape(paragraph)}</p>"
            for paragraph in summary
        )
        + f'<p style="margin:0 0 18px 0;color:#64748b;{_OUTLOOK_TEXT_STYLE}">'
        + html.escape(_assessment_scope(report))
        + "</p>"
        + _data_table(("Grade", "Findings"), grade_rows, (38, 62))
        + _private_data_source_table(report, expected_link_context)
        + "</td></tr>"
        '<tr><td style="padding:30px 32px 0 32px;">'
        + _section_heading(SECTION_TITLES[1])
        + _data_table(("Capability", "Evidence"), working, (28, 72))
        + "</td></tr>"
        '<tr><td style="padding:24px 32px 0 32px;">'
        + _section_heading(SECTION_TITLES[2])
        + _data_table(
            ("Product gap", "What happened", "Needed behavior"),
            improvements,
            (24, 43, 33),
        )
        + (
            '<p style="margin:14px 0 0 0;color:#475569;font-size:13px;line-height:20px;">'
            f"<strong>Follow-up:</strong> {html.escape(action_status)}</p>"
            if action_status
            else ""
        )
        + "</td></tr>"
        '<tr><td style="padding:24px 32px 38px 32px;">'
        + _section_heading(SECTION_TITLES[3])
        + '<p style="margin:0 0 14px 0;color:#475569;font-size:14px;line-height:21px;">'
        "For injected issues, expected insights, immutable versions, and validation guidance, "
        f'<a style="color:#0067b8;text-decoration:underline;font-weight:600;" '
        f'href="{html.escape(report_url, quote=True)}">open the full Markdown report</a>.'
        "</p>"
        + '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="width:100%;border-collapse:collapse;font-size:13px;">'
        '<tr bgcolor="#e8eef7">'
        '<th align="left" width="36%" style="padding:10px 12px;border:1px solid #d6deea;'
        'color:#12304a;">Test agent</th>'
        '<th align="left" width="14%" style="padding:10px 12px;border:1px solid #d6deea;'
        'color:#12304a;">Type</th>'
        '<th align="left" width="24%" style="padding:10px 12px;border:1px solid #d6deea;'
        'color:#12304a;">Agent</th>'
        '<th align="left" width="26%" style="padding:10px 12px;border:1px solid #d6deea;'
        'color:#12304a;">Recommend human validation</th></tr>'
        + "".join(rows)
        + "</table></td></tr></table>"
        "<!--[if mso]></td></tr></table><![endif]-->"
        "</td></tr></table></body></html>"
    )
    if tuple(re.findall(r"<h2[^>]*>(.*?)</h2>", body)) != SECTION_TITLES:
        raise ContractError("Email must contain exactly the four approved sections")
    return subject, body


def build_email_send_request(
    subject: str,
    body: str,
    recipient: dict[str, str | None],
) -> dict[str, Any]:
    content_digest = content_hash(
        {"recipient": recipient, "subject": subject, "html": body}
    )
    request = {
        "schema_version": "1.0.0",
        "channel": "connected_microsoft_mail",
        "recipient": deepcopy(recipient),
        "subject": subject,
        "html": body,
        "state": "unsent",
        "retry_delays_seconds": [60, 300, 900],
        "attempt_count": 0,
        "content_digest": content_digest,
        "transport_strategy": {
            "attempt_order": list(MAIL_TRANSPORT_ORDER),
            "graph_requires_authorization": True,
            "local_outlook_host_id": "local",
            "local_outlook_requires_authenticated_user": True,
            "local_outlook_requires_sent_items_verification": True,
            "stop_after_first_confirmed_success": True,
            "logic_app_forbidden": True,
        },
    }
    request["request_hash"] = content_hash(request)
    validate_instance(
        request,
        SCHEMAS / "email-send-request.schema.json",
        "email send request",
    )
    return request


def create_email_send_request(
    report: dict[str, Any],
    trend: dict[str, Any],
    agent_links: Mapping[str, str],
    expected_link_context: RuntimeLinkContext,
    recipient: dict[str, str | None],
) -> dict[str, Any]:
    subject, body = render_email_html(
        report, trend, agent_links, expected_link_context
    )
    return build_email_send_request(subject, body, recipient)


def import_email_receipt(
    request: dict[str, Any],
    receipt: dict[str, Any],
) -> dict[str, Any]:
    validate_instance(
        request,
        SCHEMAS / "email-send-request.schema.json",
        "email send request",
    )
    verified_hash(request, "request_hash", "email send request")
    expected_content_digest = content_hash(
        {
            "recipient": request["recipient"],
            "subject": request["subject"],
            "html": request["html"],
        }
    )
    if request["content_digest"] != expected_content_digest:
        raise ContractError("Email handoff content digest is invalid")
    validate_instance(
        receipt,
        SCHEMAS / "email-receipt.schema.json",
        "email receipt",
    )
    if receipt.get("request_hash") != request.get("request_hash"):
        raise ContractError("Email receipt does not match its send request")
    if receipt["content_digest"] != request["content_digest"]:
        raise ContractError("Email receipt content digest does not match the handoff")

    attempts = receipt["attempts"]
    transports = [attempt["transport"] for attempt in attempts]
    if transports != list(MAIL_TRANSPORT_ORDER[: len(transports)]):
        raise ContractError("Email transports must follow the no-duplicate fallback order")
    if any(
        attempt["content_digest"] != request["content_digest"]
        for attempt in attempts
    ):
        raise ContractError("Every email attempt must preserve the same content digest")
    sent_attempts = [
        (index, attempt)
        for index, attempt in enumerate(attempts)
        if attempt["state"] == "sent"
    ]
    if len(sent_attempts) > 1 or (
        sent_attempts and sent_attempts[0][0] != len(attempts) - 1
    ):
        raise ContractError("Email transport attempts must stop after first confirmed success")

    for attempt in attempts:
        transport = attempt["transport"]
        if attempt["state"] == "sent":
            if attempt["error"] is not None or not attempt["provider_reference"]:
                raise ContractError(
                    "Successful email attempt requires opaque confirmation and no error"
                )
        elif attempt["provider_reference"] is not None or not attempt["error"]:
            raise ContractError(
                "Unsuccessful email attempt requires an error and no confirmation"
            )
        if transport == "microsoft_graph" and attempt["state"] != "unauthorized":
            if not attempt["authorization_confirmed"]:
                raise ContractError("Microsoft Graph mail requires confirmed authorization")
        if transport == "local_outlook_com":
            if (
                attempt["host_id"] != "local"
                or request["recipient"]["mode"] != "authenticated_user"
                or not attempt["mailbox_match_verified"]
            ):
                raise ContractError(
                    "Local Outlook requires hostId=local and the authenticated-user mailbox"
                )
            if attempt["state"] == "sent" and not attempt["sent_items_verified"]:
                raise ContractError(
                    "Local Outlook delivery requires Sent Items verification"
                )

    if receipt["state"] == "sent":
        if len(sent_attempts) != 1:
            raise ContractError("Sent email receipt requires one confirmed transport")
        successful = sent_attempts[0][1]
        if (
            receipt["successful_transport"] != successful["transport"]
            or receipt["provider_reference"] != successful["provider_reference"]
            or not receipt["provider_reference"]
        ):
            raise ContractError("Sent email receipt requires matching opaque confirmation")
    elif sent_attempts or receipt["successful_transport"] is not None:
        raise ContractError("Failed email receipt cannot claim a successful transport")
    if receipt["state"] == "failed" and receipt["provider_reference"] is not None:
        raise ContractError("Failed email receipt cannot contain provider confirmation")
    return deepcopy(receipt)
