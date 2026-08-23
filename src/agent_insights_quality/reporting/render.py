from __future__ import annotations

import html
import os
import re
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
from agent_insights_quality.links import validate_agent_insights_url
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
    "Test agents and Agent Insights links",
)
MAIL_TRANSPORT_ORDER = (
    "connected_copilot_mail",
    "microsoft_graph",
    "local_outlook_com",
)
_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+)$")


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


def _finding_grades(report: dict[str, Any]) -> dict[str, int]:
    actuals = (report.get("bar_definition") or derive_bar_definition(report))["actuals"]
    observed = actuals["observed_findings"]
    judgments = report["field_judgments"]
    if len(judgments) == observed:
        correct = sum(item["verdict"] == "correct" for item in judgments)
        partially_useful = sum(
            item["verdict"] == "partially_useful" for item in judgments
        )
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
        correct_verb = "was" if grades["correct"] == 1 else "were"
        missed_verb = "was" if counts["false_negatives"] == 1 else "were"
        assessment = (
            f"Of {expected} expected {expected_label}, {grades['correct']} {correct_verb} "
            f"captured as fully correct findings and {counts['false_negatives']} "
            f"{missed_verb} missed. The run generated "
            f"{observed} distinct cards: {grades['correct']} correct, "
            f"{grades['partially_useful']} partially useful, and "
            f"{grades['incorrect']} incorrect or noisy."
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
    return [f"{assessment} {_quality_conclusion(report['status'])}", trust]


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
        ("Correct", str(grades["correct"])),
        ("Partially useful", str(grades["partially_useful"])),
        ("Incorrect/noisy", str(grades["incorrect"])),
        (
            "Missed expected problems",
            str(report["scorecard"]["counts"]["false_negatives"]),
        ),
    ]


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
            if item["verdict"] in {"correct", "partially_useful"}
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
                f"{grades['correct']} were fully correct and "
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
                f"Only {counts['completed_scenarios']} of {counts['active_scenarios']} "
                f"scenarios completed. {reason}",
                "Complete every planned scenario with validated evidence before drawing or "
                "promoting a quality conclusion.",
            )
        ]
    if report["status"] == "AT BAR":
        return [
            (
                "No product-quality gap observed",
                f"{actuals['expected_findings']} findings were expected and "
                f"{actuals['observed_findings']} were observed, with "
                f"{counts['false_negatives']} missed and {counts['false_positives']} noisy.",
                "Continue to detect each expected problem once, suppress healthy-agent noise, "
                "and preserve complete trace and version grounding.",
            )
        ]

    rows: list[tuple[str, str, str]] = []
    if counts["false_negatives"] or rates["high_severity_recall"] < 1:
        missed_label = (
            "problem was" if counts["false_negatives"] == 1 else "problems were"
        )
        rows.append(
            (
                "Expected problems were missed",
                f"{counts['false_negatives']} expected {missed_label} missed; "
                f"high-severity recall was {rates['high_severity_recall']:.1%} and overall "
                f"recall was {rates['overall_recall']:.1%}.",
                "Detect every high-severity problem and at least 90% of all expected problems "
                "with the correct root cause.",
            )
        )
    if grades["incorrect"] or grades["partially_useful"] or counts["healthy_insights"]:
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
                f"Of {actuals['observed_findings']} observed cards, {grades['incorrect']} "
                f"were incorrect/noisy and {grades['partially_useful']} were only partially "
                f"useful; precision was {rates['precision']:.1%}.{healthy}",
                "Return no card for healthy behavior and ground each finding in the complete "
                "trace, request, available tools, and current agent version.",
            )
        )
    if actuals["count_mismatch_runs"]:
        rows.append(
            (
                "Finding count did not match root causes",
                f"{actuals['count_mismatch_runs']} run/agent results had count mismatches; "
                f"{actuals['expected_findings']} findings were expected and "
                f"{actuals['observed_findings']} were observed.",
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
        rows.append(
            (
                "Finding content was incomplete or inaccurate",
                f"Across {len(report['field_judgments'])} judged cards, "
                + ", ".join(f"{name} passed {rate:.1%}" for name, rate in failed_fields)
                + ".",
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
                evidence,
                "Group evidence by root cause, avoid duplicate or fragmented cards, and scope "
                "each finding to the immutable agent version where it reproduces.",
            )
        )
    if "capability_fix_mismatch" in score["violations"]:
        rows.append(
            (
                "Proposed fixes assumed unavailable capabilities",
                "One or more proposed fixes referenced a capability outside the deployed "
                "agent contract.",
                "Generate remediation only from the tools, models, and integrations configured "
                "for the evaluated agent.",
            )
        )
    if actuals["trust_failures"] or counts["structural_failures"]:
        rows.append(
            (
                "Assessment trust was reduced",
                f"{counts['structural_failures']} structural failures and "
                f"{len(actuals['trust_failures'])} recorded trust failures affected the run.",
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


def render_report_markdown(report: dict[str, Any]) -> str:
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
            "| Scenario | Agent | Completed | Expected | Observed | Verdict | Insights |",
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
            "## Field judgments",
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
            "## Collection analysis",
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
        for checklist in report["human_validation_checklists"]:
            agent = agents[checklist["agent_id"]]
            lines.extend(
                [
                    f"### {agent['name']} (`{agent['id']}`)",
                    "",
                    f"**Review reason:** {checklist['review_reason']}",
                    "",
                    "| Run / immutable version | Expected insights and ground truth | "
                    "Observed final cards | Double-check |",
                    "| --- | --- | --- | --- |",
                ]
            )
            for version in checklist["versions"]:
                expected = "<br>".join(
                    f"`{item['scenario_id']}` {item['title']}: "
                    f"{item['expected_insight_count']} x {item['category']} / "
                    f"{item['severity']} - {item['root_cause']}"
                    for item in version["expected_scenarios"]
                )
                observed = (
                    "<br>".join(
                        f"`{card['insight_reference']}` ({card['verdict']})"
                        for card in version["observed_final_cards"]
                    )
                    or "None"
                )
                lines.append(
                    f"| `{version['run_id']}` / {version['phase']} "
                    f"`{version['version_digest']}` | Expected "
                    f"{version['expected_insight_count']}<br>{expected} | {observed} | "
                    f"{version['double_check']} |"
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
    for agent in report["agents"]:
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


def _trend_table(trend: dict[str, Any]) -> str:
    validate_instance(trend, SCHEMAS / "trend.schema.json", "trend")
    rows = []
    for day in trend["days"][-14:]:
        style = _STATUS_STYLES[day["status"]]
        rate_value = day["trusted_insight_rate"]
        rate = "N/A" if rate_value is None else f"{rate_value * 100:.0f}%"
        bar = "&mdash;"
        if rate_value is not None:
            bar_width = round(rate_value * 100)
            bar = (
                '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
                'width="100%" style="width:100%;border-collapse:collapse;">'
                "<tr>"
                f'<td width="{bar_width}%" bgcolor="{style["accent"]}" '
                'style="height:8px;line-height:8px;font-size:1px;">&nbsp;</td>'
                f'<td width="{100 - bar_width}%" bgcolor="#e8eef7" '
                'style="height:8px;line-height:8px;font-size:1px;">&nbsp;</td>'
                "</tr></table>"
            )
        rows.append(
            "<tr>"
            '<td style="padding:9px 10px;border:1px solid #d6deea;'
            'color:#334155;white-space:nowrap;">'
            f"{html.escape(day['report_date'])}</td>"
            f'<td bgcolor="{style["background"]}" style="padding:9px 10px;'
            f'border:1px solid #d6deea;color:{style["foreground"]};'
            'font-weight:700;white-space:nowrap;">'
            f"{html.escape(day['status'])}</td>"
            '<td style="padding:9px 10px;border:1px solid #d6deea;">'
            f"{bar}</td>"
            '<td style="padding:9px 10px;border:1px solid #d6deea;'
            'text-align:right;color:#334155;font-weight:600;">'
            f"{rate}</td>"
            "</tr>"
        )
    return (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="width:100%;border-collapse:collapse;font-size:13px;">'
        '<tr bgcolor="#e8eef7">'
        '<th align="left" style="padding:9px 10px;border:1px solid #d6deea;'
        'color:#12304a;">Date</th>'
        '<th align="left" style="padding:9px 10px;border:1px solid #d6deea;'
        'color:#12304a;">Result</th>'
        '<th align="left" style="padding:9px 10px;border:1px solid #d6deea;'
        'color:#12304a;">Trusted insight trend</th>'
        '<th align="right" style="padding:9px 10px;border:1px solid #d6deea;'
        'color:#12304a;">Rate</th></tr>'
        + "".join(rows)
        + "</table>"
    )


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
        'border:1px solid #d6deea;color:#12304a;vertical-align:top;">'
        f"{html.escape(label)}</th>"
        for label, width in zip(headers, widths, strict=True)
    )
    body = "".join(
        "<tr>"
        + "".join(
            '<td style="padding:11px 12px;border:1px solid #d6deea;'
            f'color:#334155;line-height:19px;vertical-align:top;">{html.escape(value)}</td>'
            for value in row
        )
        + "</tr>"
        for row in rows
    )
    return (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="width:100%;border-collapse:collapse;font-size:13px;">'
        f'<tr bgcolor="#e8eef7">{header}</tr>{body}</table>'
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


def _agent_expectation_html(checklist: dict[str, Any]) -> str:
    versions = []
    for version in checklist["versions"]:
        scenarios = "; ".join(
            f"{item['title']} ({item['expected_insight_count']} x "
            f"{item['category']}/{item['severity']}): {item['root_cause']}"
            for item in version["expected_scenarios"]
        )
        versions.append(
            f"<strong>{html.escape(version['phase'])}</strong>: "
            f"{version['expected_insight_count']} expected - {html.escape(scenarios)}"
        )
    return "<br>".join(versions)


def _agent_double_check_html(checklist: dict[str, Any]) -> str:
    focus = " ".join(version["double_check"] for version in checklist["versions"])
    return (
        f"{html.escape(checklist['review_reason'])}<br>"
        f"{html.escape(focus)}<br>"
        "Then verify card title/description/fix, current-version trace provenance, "
        "lifecycle prior evidence, and duplicate/fragment/umbrella relationships; "
        "record the human outcome."
    )


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
        raise ContractError("Direct email must contain a runtime Agent Insights link for every agent")
    if expected_link_context.project != report["plan_id"]:
        raise ContractError("Runtime Agent Insights context does not match the report plan")
    for agent in report["agents"]:
        validate_agent_insights_url(
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
    checklists = {
        item["agent_id"]: item
        for item in report.get("human_validation_checklists", [])
    }
    rows = []
    for agent in report["agents"]:
        checklist = checklists.get(agent["id"])
        expectations = (
            _agent_expectation_html(checklist)
            if checklist
            else html.escape(agent["human_validation"])
        )
        double_check = (
            _agent_double_check_html(checklist)
            if checklist
            else html.escape(agent["human_validation"])
        )
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
            "Open Agent Insights</a></td>"
            '<td style="padding:11px 12px;border:1px solid #d6deea;'
            f'color:#334155;line-height:18px;">{expectations}</td>'
            '<td style="padding:11px 12px;border:1px solid #d6deea;'
            f'color:#334155;line-height:18px;">{double_check}</td>'
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
        + '<p style="margin:0 0 12px 0;color:#334155;font-size:15px;line-height:23px;">'
        + html.escape(summary[0])
        + "</p>"
        '<p style="margin:0 0 18px 0;color:#475569;font-size:14px;line-height:21px;">'
        + html.escape(summary[1])
        + "</p>"
        + _data_table(("Grade", "Findings"), grade_rows, (38, 62))
        + '<p style="margin:20px 0 10px 0;color:#12304a;font-size:14px;'
        'line-height:20px;font-weight:700;">14-day quality trend</p>'
        + _trend_table(trend)
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
        + '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="width:100%;border-collapse:collapse;font-size:13px;">'
        '<tr bgcolor="#e8eef7">'
        '<th align="left" width="19%" style="padding:10px 12px;border:1px solid #d6deea;'
        'color:#12304a;">Test agent</th>'
        '<th align="left" width="10%" style="padding:10px 12px;border:1px solid #d6deea;'
        'color:#12304a;">Type</th>'
        '<th align="left" width="13%" style="padding:10px 12px;border:1px solid #d6deea;'
        'color:#12304a;">Agent Insights</th>'
        '<th align="left" width="28%" style="padding:10px 12px;border:1px solid #d6deea;'
        'color:#12304a;">Version expectations</th>'
        '<th align="left" width="30%" style="padding:10px 12px;border:1px solid #d6deea;'
        'color:#12304a;">What to double-check</th></tr>'
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
