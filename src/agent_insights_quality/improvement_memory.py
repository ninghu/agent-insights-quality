"""Deterministic Insight Engine improvement memory.

This module implements the plan's "Daily Insight Engine improvement memory"
surface. It is intentionally pure and deterministic: deterministic code supplies
current-run coverage, counts, ownership, and stable references; only a strict,
schema-validated pattern/recommendation object from GPT-5.6 Sol (``analysis``)
supplies prose. Rendering is a pure function of that object plus the persisted
living state. No network/model call, CLI wiring, or catalog/traffic access
happens here.

Findings only ever *support* a cross-Agent pattern when their ``ownership`` is
``insight_engine``; every other finding (including incomplete evidence) is an
exclusion and cannot be reassigned by this module. All rendered text reuses
already public-safe ``reasoning``/``ownership_reason`` strings that reporting.py
already renders in each per-Agent report; no additional raw prompts, traces,
provider IDs, or private identifiers are introduced.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from agent_insights_quality.reporting import COVERAGE_MISSING_TYPES
from agent_insights_quality.util import (
    ROOT,
    ContractError,
    atomic_json,
    atomic_text,
    immutable_json,
    immutable_text,
    file_hash,
    content_hash,
    read_json,
)

_ANALYSIS_SCHEMA = json.loads(
    (ROOT / "schemas" / "insight-engine-improvement-analysis.schema.json").read_text(
        encoding="utf-8"
    )
)
_ANALYSIS_VALIDATOR = Draft202012Validator(_ANALYSIS_SCHEMA)

LIVING_DOCUMENT_JSON = "reports/insight-engine-improvement.json"
LIVING_DOCUMENT_MARKDOWN = "reports/insight-engine-improvement.md"


def assessment_policy_digest() -> str:
    paths = (
        ROOT / "src" / "agent_insights_quality" / "prompts" / "assessment.md",
        ROOT / "schemas" / "assessment.schema.json",
        ROOT / "schemas" / "baseline-assessment.schema.json",
        ROOT / "schemas" / "report.schema.json",
    )
    return content_hash(
        {
            path.relative_to(ROOT).as_posix(): file_hash(path)
            for path in paths
        }
    )


def current_run_signal(report: Mapping[str, Any]) -> dict[str, int]:
    """Deterministic current-run counts for the improvement memory.

    ``missing_expected_issues`` uses the corrected coverage semantics: an
    expected issue with only Noise and/or Duplicate cards is still Missing.
    """
    details = Counter(item["detail"] for item in report["issues"])
    generated_issue_cards = sum(
        len(item["assessment"].get("card_evaluations", [])) for item in report["issues"]
    )
    return {
        "generated_issue_cards": generated_issue_cards,
        "partially_correct": details.get("PARTIAL", 0),
        "incorrect": details.get("MISMATCHED", 0),
        "noise": details.get("NOISE", 0),
        "duplicate": details.get("DUPLICATE", 0),
        "missing_expected_issues": sum(
            details.get(kind, 0) for kind in COVERAGE_MISSING_TYPES
        ),
    }


def report_coverage(report: Mapping[str, Any]) -> dict[str, Any]:
    """Public-safe coverage the report/run actually exercised.

    Never implies full-catalog coverage; it reflects only the current Daily's
    selected agents/issues and whether their evidence is complete.
    """
    return {
        "agents": len(report["baseline"]),
        "issues": len(report["issues"]),
        "runtime_evidence_complete": not bool(report["summary"].get("incomplete", False)),
    }


def exercised_capabilities(report: Mapping[str, Any]) -> set[str]:
    capabilities: set[str] = set()
    for item in report["issues"]:
        if item.get("runtime_evidence_complete") is False:
            continue
        fields = item.get("assessment", {}).get("fields", {})
        if isinstance(fields, Mapping):
            capabilities.update(str(key) for key in fields)
    return capabilities


def _finding_entry(
    *,
    agent: str,
    issue_id: str | None,
    title: str,
    reasoning: str,
    fields: Mapping[str, Any] | None = None,
    reference: str | None = None,
) -> dict[str, Any]:
    return {
        "agent": agent,
        "issue_id": issue_id,
        "title": title,
        "reasoning": reasoning,
        "failed_fields": sorted(
            key for key, passed in (fields or {}).items() if passed is False
        ),
        "reference": reference,
        "report_link": _agent_report_link(agent),
    }


def build_normalized_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """Public-safe normalized summary that would be sent to GPT-5.6 Sol.

    Splits findings into ``insight_engine_findings`` (ownership=="insight_engine"
    only, eligible to support a pattern) and ``exclusions`` (every other
    ownership, plus incomplete evidence, which can never be reassigned by the
    synthesis step). Reuses existing public-safe ``reasoning``/
    ``ownership_reason`` text; no raw prompts, traces, or private identifiers.
    """
    insight_engine_findings: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []

    def add(entry: dict[str, Any], ownership: str, finding_type: str) -> None:
        record = {**entry, "ownership": ownership, "finding_type": finding_type}
        if ownership == "insight_engine" and finding_type != "INCOMPLETE":
            insight_engine_findings.append(record)
        else:
            exclusions.append(record)

    for baseline in report["baseline"]:
        assessment = baseline["assessment"]
        if assessment["verdict"] == "clean":
            continue
        add(
            _finding_entry(
                agent=baseline["agent"],
                issue_id=None,
                title="Baseline outcome",
                reasoning=assessment.get("ownership_reason", ""),
                fields={},
            ),
            assessment["ownership"],
            assessment["verdict"].upper(),
        )
        for card in assessment.get("card_evaluations", []):
            if card.get("evaluation") == "valid_agent_finding":
                continue
            add(
                _finding_entry(
                    agent=baseline["agent"],
                    issue_id=None,
                    title=card.get("title", "Untitled Insight"),
                    reasoning=card.get("reasoning") or card.get("ownership_reason", ""),
                    fields=card.get("fields", {}),
                    reference=card.get("reference"),
                ),
                card.get("ownership", assessment["ownership"]),
                (card.get("evaluation") or "incomplete").upper(),
            )

    for item in report["issues"]:
        assessment = item["assessment"]
        primary = next(
            (
                card
                for card in assessment.get("card_evaluations", [])
                if card.get("finding_type")
                in {"MATCHED", "PARTIAL", "MISMATCHED"}
            ),
            None,
        )
        add(
            _finding_entry(
                agent=item["agent"],
                issue_id=item["issue_id"],
                title=item["title"],
                reasoning=assessment.get("reasoning") or "",
                fields=assessment.get("fields", {}),
                reference=(
                    primary.get("reference")
                    if isinstance(primary, Mapping)
                    else item.get("evidence_reference")
                ),
            ),
            assessment["ownership"],
            item["detail"],
        )
        for card in assessment.get("card_evaluations", []):
            if card.get("finding_type") not in {"NOISE", "DUPLICATE"}:
                continue
            add(
                _finding_entry(
                    agent=item["agent"],
                    issue_id=None,
                    title=card.get("title", "Untitled Insight"),
                    reasoning=card.get("reasoning") or card.get("ownership_reason", ""),
                    fields=card.get("fields", {}),
                    reference=card.get("reference"),
                ),
                card.get("ownership", assessment["ownership"]),
                card["finding_type"],
            )

    return {
        "coverage": report_coverage(report),
        "current_run_signal": current_run_signal(report),
        "insight_engine_findings": insight_engine_findings,
        "exclusions": exclusions,
    }


def validate_analysis(analysis: Mapping[str, Any]) -> None:
    """Validate GPT-5.6 Sol's returned pattern/recommendation object.

    Missing or invalid citations fail the improvement report rather than
    producing an uncited pattern; the schema enforces at least two supporting
    Agents and at least two evidence citations per pattern.
    """
    errors = sorted(
        _ANALYSIS_VALIDATOR.iter_errors(analysis), key=lambda error: list(error.path)
    )
    if errors:
        raise ContractError(
            "Insight Engine improvement analysis failed schema validation: "
            + "; ".join(error.message for error in errors)
        )
    keys = [pattern["pattern_key"] for pattern in analysis["patterns"]]
    if len(keys) != len(set(keys)):
        raise ContractError(
            "Insight Engine improvement analysis has duplicate pattern_key values"
        )
    priority_keys = {item["pattern_key"] for item in analysis["improvement_priorities"]}
    if not priority_keys.issubset(set(keys)):
        raise ContractError(
            "Insight Engine improvement priorities reference an unknown pattern_key"
        )
    serialized = json.dumps(analysis, sort_keys=True).casefold()
    for forbidden in (
        "http://",
        "https://",
        "/subscriptions/",
        "provider_id",
        "raw_trace",
        "response payload",
        "prompt payload",
        "customer data",
    ):
        if forbidden in serialized:
            raise ContractError(
                "Insight Engine improvement analysis contains forbidden "
                "private or raw evidence"
            )


def validate_analysis_against_summary(
    analysis: Mapping[str, Any],
    normalized_summary: Mapping[str, Any],
) -> None:
    validate_analysis(analysis)
    eligible = {
        (item["agent"], item.get("issue_id"))
        for item in normalized_summary["insight_engine_findings"]
    }
    for pattern in analysis["patterns"]:
        citations = {
            (item["agent"], item.get("issue_id"))
            for item in pattern["evidence"]
        }
        evidence_agents = {item[0] for item in citations}
        if (
            not citations
            or not citations.issubset(eligible)
            or evidence_agents != set(pattern["supporting_agents"])
        ):
            raise ContractError(
                "Insight Engine improvement pattern cites an ineligible or "
                "unresolved per-Agent finding"
            )


def reconcile_patterns(
    previous_patterns: Mapping[str, Mapping[str, Any]],
    analysis_patterns: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    run_date: str,
    comparable: bool,
    exercised_agents: Sequence[str],
    assessment_policy: str | None = None,
    exercised_capability_names: Sequence[str] | None = None,
    pattern_capabilities: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Pure lifecycle reconciliation of the living pattern state.

    Deterministic code, not the synthesis model, calculates status
    transitions and absence counts:

    - A pattern seen this run is ``new`` the first time, ``reopened`` if it
      was previously ``resolved``, else ``active``.
    - A pattern from prior state not seen this run keeps a ``resolved``
      status unconditionally (archived). Otherwise, if this run is not
      comparable for that pattern (either the whole run is not comparable, or
      a previously supporting Agent was not exercised this run), it becomes
      ``not_evaluated`` and its comparable-absence count is unchanged. Else
      its comparable-absence count advances by one, becoming ``resolved`` at
      two and ``watching`` otherwise.
    """
    exercised = set(exercised_agents)
    policy = assessment_policy or assessment_policy_digest()
    capabilities = (
        set(exercised_capability_names)
        if exercised_capability_names is not None
        else None
    )
    capability_map = pattern_capabilities or {}
    seen_keys = {pattern["pattern_key"] for pattern in analysis_patterns}
    reconciled: dict[str, dict[str, Any]] = {}

    for pattern in analysis_patterns:
        key = pattern["pattern_key"]
        prior = previous_patterns.get(key)
        if prior is None:
            status = "new"
            first_seen_run, first_seen_date = run_id, run_date
            observed_run_count = 1
        else:
            status = "reopened" if prior["status"] == "resolved" else "active"
            first_seen_run = prior["first_seen_run"]
            first_seen_date = prior["first_seen_date"]
            observed_run_count = int(prior.get("observed_run_count", 0)) + 1
        reconciled[key] = {
            "status": status,
            "title": pattern["title"],
            "why_it_is_a_pattern": pattern["why_it_is_a_pattern"],
            "affected_agents": sorted(set(pattern["supporting_agents"])),
            "evidence": list(pattern["evidence"]),
            "improvement": pattern["improvement"],
            "measurable_signal": pattern["measurable_signal"],
            "confidence": pattern["confidence"],
            "first_seen_run": first_seen_run,
            "first_seen_date": first_seen_date,
            "last_seen_run": run_id,
            "last_seen_date": run_date,
            "observed_run_count": observed_run_count,
            "comparable_absence_count": 0,
            "assessment_policy_digest": policy,
            "supporting_capabilities": sorted(
                set(capability_map.get(key, ()))
            ),
        }

    for key, prior in previous_patterns.items():
        if key in seen_keys:
            continue
        prior = dict(prior)
        if prior["status"] == "resolved":
            reconciled[key] = prior
            continue
        previously_affected = set(prior.get("affected_agents", []))
        required_capabilities = set(
            prior.get("supporting_capabilities", [])
        )
        run_is_comparable = (
            comparable
            and prior.get("assessment_policy_digest") == policy
            and previously_affected.issubset(exercised)
            and (
                capabilities is None
                or required_capabilities.issubset(capabilities)
            )
        )
        if not run_is_comparable:
            prior["status"] = "not_evaluated"
            reconciled[key] = prior
            continue
        absence_count = int(prior.get("comparable_absence_count", 0)) + 1
        prior["comparable_absence_count"] = absence_count
        prior["status"] = "resolved" if absence_count >= 2 else "watching"
        reconciled[key] = prior

    return reconciled


def build_run_snapshot(
    report: Mapping[str, Any],
    analysis: Mapping[str, Any],
    reconciled_patterns: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Assemble the immutable per-run snapshot object for this Official Daily."""
    seen_keys = {pattern["pattern_key"] for pattern in analysis["patterns"]}
    return {
        "schema_version": "1.0.0",
        "run_id": report["run_id"],
        "report_date": report["report_date"],
        "coverage": report_coverage(report),
        "assessment_policy": "unchanged",
        "assessment_policy_digest": assessment_policy_digest(),
        "executive_summary": analysis["executive_summary"],
        "current_run_signal": current_run_signal(report),
        "patterns": [
            {
                "pattern_key": pattern["pattern_key"],
                "title": pattern["title"],
                "supporting_agents": sorted(set(pattern["supporting_agents"])),
                "measurable_signal": pattern["measurable_signal"],
            }
            for pattern in analysis["patterns"]
        ],
        "reconciliation": [
            {
                "pattern_key": key,
                "seen_this_run": key in seen_keys,
                "status": entry["status"],
                "comparable_absence_count": entry.get("comparable_absence_count", 0),
            }
            for key, entry in sorted(reconciled_patterns.items())
        ],
        "isolated_observations": list(analysis["isolated_observations"]),
        "exclusions": list(analysis["exclusions"]),
    }


_AGENT_DISPLAY_NAMES = {
    "weather-agent": "Weather",
    "healthcare-agent": "Healthcare",
    "finance-agent": "Finance",
    "travel-agent": "Travel",
    "support-ticket-agent": "Support",
}


def _agent_display(agent: str) -> str:
    return _AGENT_DISPLAY_NAMES.get(agent, agent)


def _agent_report_link(agent: str) -> str:
    return f"agents/{agent}.md"


def render_snapshot_markdown(snapshot: Mapping[str, Any]) -> str:
    """Render the immutable per-run snapshot markdown from ``snapshot``."""
    coverage = snapshot["coverage"]
    run_id = snapshot["run_id"]
    lines = [
        "# Insight Engine Improvement Snapshot",
        "",
        f"> **Immutable snapshot for `{run_id}`.** This snapshot records only this "
        "run. The stable [Insight Engine Improvement Memory]"
        "(../../../../insight-engine-improvement.md) reconciles it with prior "
        "Official Daily snapshots.",
        "",
        f"- Report date: `{snapshot['report_date']}`",
        f"- Run: `{run_id}`",
        f"- Coverage: `{coverage['agents']} agents / {coverage['issues']} expected issues`",
        "- Runtime evidence: "
        f"`{'Complete' if coverage['runtime_evidence_complete'] else 'Incomplete'}`",
        f"- Assessment policy: `{snapshot['assessment_policy']}`",
        "",
        "## Executive summary",
        "",
        snapshot["executive_summary"],
        "",
        "## Current-run signal",
        "",
        "| Signal | Count |",
        "| --- | ---: |",
    ]
    signal = snapshot["current_run_signal"]
    for label, key in (
        ("Generated issue cards", "generated_issue_cards"),
        ("Partially Correct", "partially_correct"),
        ("Incorrect", "incorrect"),
        ("Noise", "noise"),
        ("Duplicate", "duplicate"),
        ("Missing expected issues", "missing_expected_issues"),
    ):
        lines.append(f"| {label} | {signal[key]} |")
    lines.extend(
        [
            "",
            "## Current-run patterns",
            "",
            "| Pattern | Supporting Agents | Measurable signal |",
            "| --- | --- | --- |",
        ]
    )
    if snapshot["patterns"]:
        for pattern in snapshot["patterns"]:
            agents = ", ".join(_agent_display(a) for a in pattern["supporting_agents"])
            lines.append(
                f"| {pattern['title']} | {agents} | {pattern['measurable_signal']} |"
            )
    else:
        lines.append("| None | - | No cross-Agent pattern was identified this run. |")
    lines.extend(
        [
            "",
            "## Memory reconciliation input",
            "",
            "| Pattern | Seen in this run | Status | Comparable absence |",
            "| --- | --- | --- | ---: |",
        ]
    )
    for row in snapshot["reconciliation"]:
        lines.append(
            f"| `{row['pattern_key']}` | {'Yes' if row['seen_this_run'] else 'No'} | "
            f"{row['status']} | {row['comparable_absence_count']} |"
        )
    if snapshot["isolated_observations"]:
        lines.extend(["", "## Isolated observations", ""])
        lines.extend(f"- {item}" for item in snapshot["isolated_observations"])
    if snapshot["exclusions"]:
        lines.extend(["", "## Exclusions", ""])
        lines.extend(f"- {item}" for item in snapshot["exclusions"])
    agent_names = sorted(
        {
            agent
            for pattern in snapshot["patterns"]
            for agent in pattern["supporting_agents"]
        }
    ) or sorted(_AGENT_DISPLAY_NAMES)
    lines.extend(["", "## Evidence links", ""])
    lines.extend(
        f"- [{_agent_display(agent)} evaluation]({_agent_report_link(agent)})"
        for agent in agent_names
    )
    lines.extend(
        [
            "",
            "This snapshot cannot be edited by a later Daily run. Later status "
            "changes appear in the stable living document and a new immutable "
            "snapshot.",
            "",
        ]
    )
    return "\n".join(lines)


def render_living_markdown(state: Mapping[str, Any]) -> str:
    """Render the stable living document from the persisted ``state`` object."""
    patterns = state["patterns"]
    active = {k: v for k, v in patterns.items() if v["status"] in {"new", "active", "reopened"}}
    watching = {k: v for k, v in patterns.items() if v["status"] == "watching"}
    resolved = {k: v for k, v in patterns.items() if v["status"] == "resolved"}
    coverage = state["latest_coverage"]
    lines = [
        "# Insight Engine Improvement Memory",
        "",
        "> This is the stable living document. It remembers recurring patterns "
        "across Official Daily runs while immutable snapshots preserve each "
        "run's original analysis. It is advisory and does not change score, "
        "ownership, promotion, or per-card assessment.",
        "",
        f"- Last updated: `{state['last_updated']}`",
        f"- Latest Official Daily: `{state['latest_run_id']}`",
        f"- Latest coverage: `{coverage['agents']} agents / {coverage['issues']} expected issues`",
        "- Latest runtime evidence: "
        f"`{'Complete' if coverage['runtime_evidence_complete'] else 'Incomplete'}`",
        (
            f"- Latest immutable snapshot: [Open snapshot]"
            f"({state['latest_snapshot_link']})"
            if state.get("latest_snapshot_link")
            else "- Latest immutable snapshot: `None`"
        ),
        "",
        "## Executive summary",
        "",
        state.get("executive_summary", "No executive summary is available yet."),
        "",
        "## Improvement priorities",
        "",
        "| Rank | Improvement area | Affected Agents | Success signal in a later run |",
        "| ---: | --- | --- | --- |",
    ]
    ranked = sorted(
        active.items(),
        key=lambda kv: (-kv[1]["confidence"], kv[0]),
    )
    if ranked:
        for rank, (_key, pattern) in enumerate(ranked, start=1):
            agents = ", ".join(_agent_display(a) for a in pattern["affected_agents"])
            lines.append(
                f"| {rank} | {pattern['improvement']} | {agents} | "
                f"{pattern['measurable_signal']} |"
            )
    else:
        lines.append("| - | No active cross-Agent pattern is currently open. | - | - |")
    lines.extend(["", "## Cross-Agent patterns", ""])
    if active:
        for index, (key, pattern) in enumerate(
            sorted(active.items(), key=lambda kv: kv[0]), start=1
        ):
            lines.extend(
                [
                    f"### Pattern {index} - {pattern['title']}",
                    "",
                    f"**Status:** `{pattern['status'].title()}`  ",
                    f"**First seen:** `{pattern['first_seen_run']}`  ",
                    f"**Last seen:** `{pattern['last_seen_run']}`  ",
                    f"**Comparable absence count:** `{pattern['comparable_absence_count']}/2`",
                    "",
                    f"**Why this is a pattern:** {pattern['why_it_is_a_pattern']}",
                    "",
                    f"**Insight Engine improvement:** {pattern['improvement']}",
                    "",
                ]
            )
    else:
        lines.extend(["No active cross-Agent pattern is currently open.", ""])
    lines.extend(["## Watching patterns", ""])
    if watching:
        lines.extend(
            [
                "| Pattern | Last seen | Comparable absence |",
                "| --- | --- | ---: |",
            ]
        )
        for key, pattern in sorted(watching.items()):
            lines.append(
                f"| {pattern['title']} | `{pattern['last_seen_run']}` | "
                f"`{pattern['comparable_absence_count']}/2` |"
            )
    else:
        lines.append("No patterns are currently Watching.")
    lines.extend(["", "## Resolved archive", ""])
    if resolved:
        lines.extend(["| Pattern | Last seen |", "| --- | --- |"])
        for key, pattern in sorted(resolved.items()):
            lines.append(f"| {pattern['title']} | `{pattern['last_seen_run']}` |")
    else:
        lines.append("No patterns have reached two consecutive comparable complete absences.")
    lines.extend(["", "## Isolated observations", ""])
    isolated = state.get("isolated_observations") or []
    if isolated:
        lines.extend(f"- {item}" for item in isolated)
    else:
        lines.append("No isolated single-Agent observations from the latest run.")
    lines.extend(["", "## Exclusions/limitations", ""])
    exclusions = state.get("exclusions") or []
    if exclusions:
        lines.extend(f"- {item}" for item in exclusions)
    else:
        lines.append("No findings were excluded from analysis in the latest run.")
    lines.extend(["", "## Agent evidence map", ""])
    lines.extend(
        [
            "Each Per-Agent report contains its own coding-agent context.",
            "",
            "| Agent | Evaluation |",
            "| --- | --- |",
        ]
    )
    latest_snapshot = str(state.get("latest_snapshot_link") or "")
    if latest_snapshot:
        latest_root = latest_snapshot.rsplit("/", 1)[0]
        for agent in sorted(_AGENT_DISPLAY_NAMES):
            lines.append(
                f"| {_agent_display(agent)} | "
                f"[Open]({latest_root}/agents/{agent}.md) |"
            )
    else:
        lines.append("| - | No Official Daily per-Agent reports are available yet. |")
    lines.extend(["", "## Snapshot history", ""])
    history = state.get("snapshot_history") or []
    if history:
        lines.extend(
            [
                "| Official Daily | Coverage | Memory update | Immutable snapshot |",
                "| --- | --- | --- | --- |",
            ]
        )
        for row in history:
            lines.append(
                f"| `{row['run_id']}` | {row['coverage_text']} | "
                f"{row['memory_update_text']} | [Open]({row['snapshot_link']}) |"
            )
    else:
        lines.append("No prior Official Daily has updated this living document.")
    lines.extend(
        [
            "",
            "## Analysis guardrails",
            "",
            "- A cross-Agent pattern requires supporting findings from at least "
            "two distinct Agents.",
            "- One comparable absence moves a pattern to Watching; two "
            "consecutive comparable absences move it to Resolved. Missing "
            "coverage and INCOMPLETE runs do not advance the count.",
            "- Every pattern and recommendation must link to the underlying "
            "per-Agent evaluation.",
            "- Incomplete evidence is reported as a limitation and cannot "
            "support a systemic conclusion.",
            "- Recommendations must generalize from observed evidence; they "
            "must not encode Test Agent issue IDs, expected defects, fixed "
            "prompts, or known answers into production Insight Engine "
            "behavior.",
            "- Only public-safe assessment summaries are analyzed. Raw "
            "prompts, responses, traces, provider IDs, private resource "
            "identifiers, and customer data are forbidden.",
            "- This report is advisory. Per-card assessments remain the "
            "source of scoring and ownership.",
            "",
        ]
    )
    return "\n".join(lines)


def _memory_update_text(reconciled_patterns: Mapping[str, Mapping[str, Any]]) -> str:
    counts = Counter(entry["status"] for entry in reconciled_patterns.values())
    parts = [
        f"{counts[status]} {status.title()}"
        for status in ("new", "active", "reopened", "watching", "resolved")
        if counts.get(status)
    ]
    return ", ".join(parts) if parts else "No pattern change"


def build_living_state(
    previous_state: Mapping[str, Any] | None,
    report: Mapping[str, Any],
    analysis: Mapping[str, Any],
    reconciled_patterns: Mapping[str, Mapping[str, Any]],
    *,
    snapshot_link: str,
) -> dict[str, Any]:
    """Merge ``reconciled_patterns`` into the persisted living-state object."""
    history = list((previous_state or {}).get("snapshot_history") or [])
    coverage = report_coverage(report)
    history.insert(
        0,
        {
            "run_id": report["run_id"],
            "coverage_text": (
                f"{coverage['agents']} Agents / {coverage['issues']} issues / "
                f"{'complete' if coverage['runtime_evidence_complete'] else 'incomplete'}"
            ),
            "memory_update_text": _memory_update_text(reconciled_patterns),
            "snapshot_link": snapshot_link,
        },
    )
    return {
        "schema_version": "1.0.0",
        "last_updated": report["report_date"],
        "latest_run_id": report["run_id"],
        "latest_coverage": coverage,
        "latest_snapshot_link": snapshot_link,
        "executive_summary": analysis["executive_summary"],
        "isolated_observations": list(analysis["isolated_observations"]),
        "exclusions": list(analysis["exclusions"]),
        "patterns": {key: dict(value) for key, value in reconciled_patterns.items()},
        "snapshot_history": history,
    }


def write_improvement_memory(
    *,
    report: Mapping[str, Any],
    analysis: Mapping[str, Any],
    reports_root: Path,
    living_state_path: Path,
) -> dict[str, Any]:
    """Reconcile, persist, and render the improvement memory for one run.

    Only an Official Daily (``profile == "daily"``) may mutate the living
    document; every run may still receive an immutable snapshot/history row.
    An INCOMPLETE run reconciles with ``comparable=False`` and an empty
    analysis pattern set, so it can never create, resolve, reopen,
    reprioritize, or otherwise mutate pattern memory - it only records a
    snapshot/history row.
    """
    if report.get("profile") != "daily":
        raise ContractError(
            "Insight Engine improvement memory can only be written for an "
            "Official Daily report"
        )
    normalized_summary = build_normalized_summary(report)
    validate_analysis_against_summary(analysis, normalized_summary)
    coverage = report_coverage(report)
    previous_state = read_json(living_state_path) if living_state_path.exists() else None
    if previous_state is not None and previous_state.get("latest_run_id") == report["run_id"]:
        raise ContractError(
            f"Insight Engine improvement memory already recorded run "
            f"{report['run_id']!r}; each Official Daily run may reconcile "
            "pattern memory exactly once"
        )
    previous_patterns = (previous_state or {}).get("patterns", {})
    exercised_agents = [item["agent"] for item in report["baseline"]]
    is_comparable = coverage["runtime_evidence_complete"]
    analysis_patterns = list(analysis["patterns"]) if is_comparable else []
    eligible_by_finding = {
        (item["agent"], item.get("issue_id")): item
        for item in normalized_summary["insight_engine_findings"]
    }
    pattern_capabilities = {
        pattern["pattern_key"]: sorted(
            {
                capability
                for evidence in pattern["evidence"]
                for capability in eligible_by_finding[
                    (evidence["agent"], evidence.get("issue_id"))
                ]["failed_fields"]
            }
        )
        for pattern in analysis_patterns
    }
    reconciled = reconcile_patterns(
        previous_patterns,
        analysis_patterns,
        run_id=report["run_id"],
        run_date=report["report_date"],
        comparable=is_comparable,
        exercised_agents=exercised_agents,
        assessment_policy=assessment_policy_digest(),
        exercised_capability_names=sorted(
            exercised_capabilities(report)
        ),
        pattern_capabilities=pattern_capabilities,
    )
    snapshot = build_run_snapshot(report, analysis, reconciled)
    snapshot_dir = (
        reports_root
        / "daily"
        / report["report_date"].replace("-", "/")
    )
    snapshot_json_path = snapshot_dir / "insight-engine-improvement.json"
    snapshot_markdown_path = snapshot_dir / "insight-engine-improvement.md"
    immutable_json(snapshot_json_path, snapshot)
    immutable_text(snapshot_markdown_path, render_snapshot_markdown(snapshot))

    relative_snapshot_link = "/".join(
        ["daily", *report["report_date"].split("-"), "insight-engine-improvement.md"]
    )
    living_state = build_living_state(
        previous_state,
        report,
        analysis,
        reconciled,
        snapshot_link=relative_snapshot_link,
    )
    atomic_json(living_state_path, living_state)
    living_markdown_path = reports_root / "insight-engine-improvement.md"
    atomic_text(living_markdown_path, render_living_markdown(living_state))
    return living_state


def write_improvement_preview(
    *,
    report: Mapping[str, Any],
    analysis: Mapping[str, Any],
    output: Path,
) -> dict[str, Any]:
    if report.get("profile") != "daily":
        raise ContractError(
            "Insight Engine improvement preview requires a Daily report"
        )
    normalized_summary = build_normalized_summary(report)
    validate_analysis_against_summary(analysis, normalized_summary)
    coverage = report_coverage(report)
    patterns = list(analysis["patterns"]) if coverage["runtime_evidence_complete"] else []
    reconciled = reconcile_patterns(
        {},
        patterns,
        run_id=report["run_id"],
        run_date=report["report_date"],
        comparable=coverage["runtime_evidence_complete"],
        exercised_agents=[item["agent"] for item in report["baseline"]],
        assessment_policy=assessment_policy_digest(),
        exercised_capability_names=sorted(
            exercised_capabilities(report)
        ),
        pattern_capabilities={
            pattern["pattern_key"]: sorted(
                {
                    capability
                    for evidence in pattern["evidence"]
                    for finding in normalized_summary[
                        "insight_engine_findings"
                    ]
                    if (
                        finding["agent"],
                        finding.get("issue_id"),
                    )
                    == (
                        evidence["agent"],
                        evidence.get("issue_id"),
                    )
                    for capability in finding["failed_fields"]
                }
            )
            for pattern in patterns
        },
    )
    snapshot = build_run_snapshot(report, analysis, reconciled)
    immutable_json(
        output / "insight-engine-improvement-preview.json",
        snapshot,
    )
    immutable_text(
        output / "insight-engine-improvement-preview.md",
        render_snapshot_markdown(snapshot),
    )
    return snapshot


def validate_published_improvement(
    *,
    report: Mapping[str, Any],
    living_state: Mapping[str, Any],
    living_markdown: str,
    snapshot: Mapping[str, Any],
    snapshot_markdown: str,
) -> None:
    expected_snapshot_link = "/".join(
        ["daily", *report["report_date"].split("-"), "insight-engine-improvement.md"]
    )
    if (
        report.get("profile") != "daily"
        or snapshot.get("run_id") != report["run_id"]
        or snapshot.get("report_date") != report["report_date"]
        or snapshot.get("coverage") != report_coverage(report)
        or snapshot.get("current_run_signal") != current_run_signal(report)
        or snapshot.get("assessment_policy_digest")
        != assessment_policy_digest()
        or living_state.get("latest_run_id") != report["run_id"]
        or living_state.get("last_updated") != report["report_date"]
        or living_state.get("latest_coverage") != report_coverage(report)
        or living_state.get("latest_snapshot_link") != expected_snapshot_link
        or not living_state.get("snapshot_history")
        or living_state["snapshot_history"][0].get("run_id") != report["run_id"]
    ):
        raise ContractError(
            "Published Insight Engine improvement memory is not bound to the Daily report"
        )
    if living_markdown != render_living_markdown(living_state):
        raise ContractError(
            "Published Insight Engine improvement living Markdown is inconsistent"
        )
    if snapshot_markdown != render_snapshot_markdown(snapshot):
        raise ContractError(
            "Published Insight Engine improvement snapshot Markdown is inconsistent"
        )
