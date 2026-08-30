from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

from agent_insights_quality import improvement_memory
from agent_insights_quality.improvement_memory import (
    build_living_state,
    assessment_policy_digest,
    build_normalized_summary,
    build_run_snapshot,
    current_run_signal,
    assign_stable_pattern_ids,
    reconcile_patterns,
    render_living_markdown,
    render_snapshot_markdown,
    report_coverage,
    validate_analysis,
    validate_analysis_against_summary,
    validate_published_improvement,
    write_improvement_memory,
    write_improvement_preview,
)
from agent_insights_quality.catalogs import catalog_hashes, load_catalogs
from agent_insights_quality.reporting import build_operational_failure_report
from agent_insights_quality.util import ROOT, ContractError, content_hash


def _issue(
    issue_id: str,
    agent: str,
    detail: str,
    *,
    ownership: str = "insight_engine",
    reasoning: str = "Public-safe reasoning.",
    card_evaluations: list[dict] | None = None,
) -> dict:
    fields = {
        "root_cause": detail not in {"PARTIAL", "MISMATCHED"},
        "title": True,
    }
    if card_evaluations is None:
        reference = content_hash({"agent": agent, "issue_id": issue_id, "detail": detail})
        if detail in {"MATCHED", "PARTIAL", "MISMATCHED"}:
            card_evaluations = [
                {
                    "reference": reference,
                    "title": f"Card for {issue_id}",
                    "finding_type": detail,
                    "fields": fields,
                    "field_reasons": (
                        {"root_cause": "The root cause is incorrect."}
                        if detail in {"PARTIAL", "MISMATCHED"}
                        else {}
                    ),
                    "ownership": ownership,
                    "confidence": 0.8,
                    "reasoning": reasoning,
                }
            ]
        elif detail == "NOISE":
            card_evaluations = [
                {
                    "reference": reference,
                    "title": f"Noise for {issue_id}",
                    "finding_type": "NOISE",
                    "fields": fields,
                    "ownership": ownership,
                    "confidence": 0.8,
                    "reasoning": reasoning,
                }
            ]
        elif detail == "DUPLICATE":
            primary = content_hash({"agent": agent, "issue_id": issue_id, "primary": True})
            card_evaluations = [
                {
                    "reference": primary,
                    "title": f"Primary for {issue_id}",
                    "finding_type": "MATCHED",
                    "fields": fields,
                    "ownership": "none",
                    "confidence": 0.9,
                    "reasoning": "Primary finding.",
                },
                {
                    "reference": reference,
                    "title": f"Duplicate for {issue_id}",
                    "finding_type": "DUPLICATE",
                    "duplicate_of": primary,
                    "fields": fields,
                    "ownership": ownership,
                    "confidence": 0.8,
                    "reasoning": reasoning,
                },
            ]
        else:
            card_evaluations = []
    return {
        "issue_id": issue_id,
        "agent": agent,
        "title": f"Title for {issue_id}",
        "detail": detail,
        "assessment": {
            "ownership": ownership,
            "reasoning": reasoning,
            "fields": fields,
            "card_evaluations": card_evaluations,
        },
    }


def _baseline(agent: str, *, verdict: str = "clean") -> dict:
    return {
        "agent": agent,
        "assessment": {
            "verdict": verdict,
            "ownership": "none" if verdict == "clean" else "insight_engine",
            "ownership_reason": "No baseline Insight was observed.",
            "card_evaluations": [],
        },
    }


def _report(
    *,
    profile: str = "daily",
    run_id: str = "aiq-20260824",
    report_date: str = "2026-08-24",
    incomplete: bool = False,
    issues: list[dict] | None = None,
    baseline: list[dict] | None = None,
) -> dict:
    agents = ["weather-agent", "healthcare-agent", "finance-agent", "travel-agent"]
    return {
        "run_id": run_id,
        "report_date": report_date,
        "profile": profile,
        "summary": {"incomplete": incomplete},
        "baseline": baseline
        if baseline is not None
        else [_baseline(agent) for agent in agents],
        "issues": issues
        if issues is not None
        else [
            _issue("issue-001", "weather-agent", "MATCHED", ownership="none"),
            _issue("issue-002", "healthcare-agent", "PARTIAL"),
            _issue("issue-003", "finance-agent", "MISMATCHED"),
            _issue("issue-004", "travel-agent", "NOISE"),
        ],
    }


def _analysis(
    *,
    pattern_key: str = "root-cause-drift",
    supporting_agents: tuple[str, ...] = ("healthcare-agent", "finance-agent"),
) -> dict:
    return {
        "schema_version": "1.0.0",
        "model": "gpt-5.6-sol",
        "executive_summary": "A cross-Agent root-cause drift pattern is the strongest signal.",
        "patterns": [
            {
                "pattern_key": pattern_key,
                "title": "Root cause replaced by downstream symptom",
                "why_it_is_a_pattern": (
                    "The same diagnosis error appears in two independent Agent families."
                ),
                "supporting_agents": list(supporting_agents),
                "evidence": [
                    {
                        "finding_id": f"{supporting_agents[0]}/issue-002/expected",
                        "agent": supporting_agents[0],
                        "issue_id": "issue-002",
                        "detail": "root_cause=false",
                    },
                    {
                        "finding_id": f"{supporting_agents[1]}/issue-003/expected",
                        "agent": supporting_agents[1],
                        "issue_id": "issue-003",
                        "detail": "root_cause=false",
                    },
                ],
                "improvement": "Separate root identification from card writing.",
                "measurable_signal": "Fewer root_cause=false judgments across two Agents.",
                "confidence": 0.8,
            }
        ],
        "isolated_observations": ["A single-Agent observation remains isolated."],
        "improvement_priorities": [
            {
                "pattern_key": pattern_key,
                "why_it_matters": "Root-cause drift compounds every downstream field.",
            }
        ],
        "exclusions": ["One agent-owned finding could not support a pattern."],
    }


def test_current_run_signal_counts_noise_and_duplicate_as_missing() -> None:
    report = _report(
        issues=[
            _issue("issue-001", "weather-agent", "MATCHED"),
            _issue("issue-002", "healthcare-agent", "PARTIAL"),
            _issue("issue-003", "finance-agent", "MISMATCHED"),
            _issue("issue-004", "travel-agent", "NOISE"),
            _issue("issue-005", "travel-agent", "DUPLICATE"),
            _issue("issue-006", "weather-agent", "MISSING"),
        ]
    )
    signal = current_run_signal(report)
    assert signal["partially_correct"] == 1
    assert signal["incorrect"] == 1
    assert signal["noise"] == 1
    assert signal["duplicate"] == 1
    # Noise-only and explicitly Missing issues lack a primary; the Duplicate
    # remains attached to its independently selected primary.
    assert signal["missing_expected_issues"] == 2


def test_stable_living_memory_seed_files_exist_without_dead_links() -> None:
    state = json.loads(
        (ROOT / "reports" / "insight-engine-improvement.json").read_text(
            encoding="utf-8"
        )
    )
    markdown = (
        ROOT / "reports" / "insight-engine-improvement.md"
    ).read_text(encoding="utf-8")
    assert state["latest_run_id"] is None
    assert state["patterns"] == {}
    assert "No Official Daily per-Agent reports are available yet." in markdown
    assert "](agents/" not in markdown


def test_report_coverage_reflects_selected_scope_and_completeness() -> None:
    report = _report()
    coverage = report_coverage(report)
    assert coverage == {
        "agents": 4,
        "issues": 4,
        "runtime_evidence_complete": True,
    }
    incomplete_report = _report(incomplete=True)
    assert report_coverage(incomplete_report)["runtime_evidence_complete"] is False


def test_build_normalized_summary_splits_by_ownership_and_excludes_incomplete() -> None:
    report = _report(
        issues=[
            _issue("issue-001", "weather-agent", "MATCHED", ownership="none"),
            _issue("issue-002", "healthcare-agent", "PARTIAL", ownership="insight_engine"),
            _issue("issue-003", "finance-agent", "MISMATCHED", ownership="agent"),
            _issue("issue-004", "travel-agent", "INCOMPLETE", ownership="insight_engine"),
        ]
    )
    normalized = build_normalized_summary(report)
    eligible_issue_ids = {f["issue_id"] for f in normalized["insight_engine_findings"]}
    excluded_issue_ids = {f["issue_id"] for f in normalized["exclusions"]}
    assert eligible_issue_ids == {"issue-002"}
    assert "issue-001" in excluded_issue_ids
    assert "issue-003" in excluded_issue_ids
    # Ownership=insight_engine but INCOMPLETE evidence can never support a pattern.
    assert "issue-004" in excluded_issue_ids
    assert normalized["coverage"] == report_coverage(report)
    assert normalized["current_run_signal"] == current_run_signal(report)
    eligible = normalized["insight_engine_findings"][0]
    assert eligible["report_link"] == "agents/healthcare-agent.md"
    assert eligible["failed_fields"]
    assert eligible["finding_id"] == "healthcare-agent/issue-002/expected"
    assert eligible["reference"].startswith("sha256:")


def test_normalized_summary_keeps_duplicate_issue_and_noise_without_one() -> None:
    report = _report(
        issues=[
            _issue("issue-001", "weather-agent", "DUPLICATE"),
            _issue("issue-002", "healthcare-agent", "NOISE"),
        ]
    )
    normalized = build_normalized_summary(report)
    duplicate = next(
        item
        for item in normalized["insight_engine_findings"]
        if item["finding_type"] == "DUPLICATE"
    )
    noise = next(
        item
        for item in normalized["insight_engine_findings"]
        if item["finding_type"] == "NOISE"
    )
    assert duplicate["issue_id"] == "issue-001"
    assert noise["issue_id"] is None
    assert duplicate["finding_id"] != noise["finding_id"]


def test_validate_analysis_rejects_invalid_and_uncited_patterns() -> None:
    validate_analysis(_analysis())  # does not raise

    single_agent = deepcopy(_analysis())
    single_agent["patterns"][0]["supporting_agents"] = ["weather-agent"]
    with pytest.raises(ContractError):
        validate_analysis(single_agent)

    missing_field = deepcopy(_analysis())
    del missing_field["patterns"][0]["measurable_signal"]
    with pytest.raises(ContractError):
        validate_analysis(missing_field)

    duplicate_keys = deepcopy(_analysis())
    duplicate_keys["patterns"].append(deepcopy(duplicate_keys["patterns"][0]))
    with pytest.raises(ContractError, match="duplicate pattern_key"):
        validate_analysis(duplicate_keys)

    unknown_priority = deepcopy(_analysis())
    unknown_priority["improvement_priorities"][0]["pattern_key"] = "unknown-pattern"
    with pytest.raises(ContractError, match="unknown pattern_key"):
        validate_analysis(unknown_priority)
    private = deepcopy(_analysis())
    private["executive_summary"] = "See https://internal.invalid/raw_trace."
    with pytest.raises(ContractError, match="private or raw"):
        validate_analysis(private)


def test_analysis_citations_must_resolve_to_insight_engine_findings() -> None:
    report = _report()
    analysis = _analysis()
    validate_analysis_against_summary(
        analysis,
        build_normalized_summary(report),
    )
    invalid = deepcopy(analysis)
    invalid["patterns"][0]["evidence"][0]["issue_id"] = "issue-999"
    with pytest.raises(ContractError, match="ineligible"):
        validate_analysis_against_summary(
            invalid,
            build_normalized_summary(report),
        )
    invalid_reference = deepcopy(analysis)
    invalid_reference["patterns"][0]["evidence"][0]["finding_id"] = (
        "healthcare-agent/issue-999/expected"
    )
    with pytest.raises(ContractError, match="ineligible"):
        validate_analysis_against_summary(
            invalid_reference,
            build_normalized_summary(report),
        )


def test_pattern_ids_use_root_evidence_not_generated_title() -> None:
    analysis = _analysis(pattern_key="model-local-key")
    summary = build_normalized_summary(_report())
    first = assign_stable_pattern_ids(analysis, {}, summary)
    stable_id = first["patterns"][0]["pattern_key"]
    assert stable_id != "model-local-key"
    assert first["improvement_priorities"][0]["pattern_key"] == stable_id

    reworded = _analysis(pattern_key="another-model-local-key")
    reworded["patterns"][0]["title"] = "A harmlessly reworded pattern title"
    second = assign_stable_pattern_ids(reworded, {}, summary)
    assert second["patterns"][0]["pattern_key"] == stable_id

    changed_summary = deepcopy(summary)
    changed_summary["insight_engine_findings"][0][
        "failed_field_reasons"
    ]["root_cause"] = "Different root evidence."
    changed_local_key = _analysis(pattern_key="another-model-local-key")
    changed = assign_stable_pattern_ids(
        changed_local_key,
        {},
        changed_summary,
    )
    assert changed["patterns"][0]["pattern_key"] != stable_id

    prior = {
        stable_id: {
            "title": first["patterns"][0]["title"],
            "status": "active",
        }
    }
    normalized = build_normalized_summary(
        _report(),
        {"patterns": prior},
    )
    assert normalized["prior_patterns"] == [
        {
            "pattern_id": stable_id,
            "title": first["patterns"][0]["title"],
            "status": "active",
            "affected_agents": [],
            "supporting_capabilities": [],
        }
    ]


def test_living_memory_reuses_evidence_identity_after_title_rewording(
    tmp_path: Path,
) -> None:
    reports_root = tmp_path / "reports"
    living = reports_root / "insight-engine-improvement.json"
    first = write_improvement_memory(
        report=_report(),
        analysis=_analysis(pattern_key="first-model-key"),
        reports_root=reports_root,
        living_state_path=living,
    )
    first_id = next(iter(first["patterns"]))
    next_report = _report(
        run_id="aiq-20260825",
        report_date="2026-08-25",
    )
    reworded = _analysis(pattern_key="second-model-key")
    reworded["patterns"][0]["title"] = "Reworded without changing evidence"
    second = write_improvement_memory(
        report=next_report,
        analysis=reworded,
        reports_root=reports_root,
        living_state_path=living,
    )
    assert list(second["patterns"]) == [first_id]
    assert second["patterns"][first_id]["title"] == (
        "Reworded without changing evidence"
    )


def test_reconcile_patterns_lifecycle_transitions() -> None:
    analysis = _analysis()
    validate_analysis(analysis)

    # First observation: new.
    first = reconcile_patterns(
        {},
        analysis["patterns"],
        run_id="aiq-r01",
        run_date="2026-08-20",
        comparable=True,
        exercised_agents=["weather-agent", "healthcare-agent", "finance-agent", "travel-agent"],
    )
    assert first["root-cause-drift"]["status"] == "new"
    assert first["root-cause-drift"]["observed_run_count"] == 1

    # Seen again: active.
    second = reconcile_patterns(
        first,
        analysis["patterns"],
        run_id="aiq-r02",
        run_date="2026-08-21",
        comparable=True,
        exercised_agents=["weather-agent", "healthcare-agent", "finance-agent", "travel-agent"],
    )
    assert second["root-cause-drift"]["status"] == "active"
    assert second["root-cause-drift"]["observed_run_count"] == 2

    # Absent, but comparable: watching.
    third = reconcile_patterns(second, [], run_id="aiq-r03", run_date="2026-08-22", comparable=True, exercised_agents=["weather-agent", "healthcare-agent", "finance-agent", "travel-agent"])
    assert third["root-cause-drift"]["status"] == "watching"
    assert third["root-cause-drift"]["comparable_absence_count"] == 1

    # Absent again, comparable: resolved.
    fourth = reconcile_patterns(third, [], run_id="aiq-r04", run_date="2026-08-23", comparable=True, exercised_agents=["weather-agent", "healthcare-agent", "finance-agent", "travel-agent"])
    assert fourth["root-cause-drift"]["status"] == "resolved"
    assert fourth["root-cause-drift"]["comparable_absence_count"] == 2

    # Once resolved, a further absence stays resolved (archived) unconditionally.
    fifth = reconcile_patterns(fourth, [], run_id="aiq-r05", run_date="2026-08-24", comparable=True, exercised_agents=["weather-agent", "healthcare-agent", "finance-agent", "travel-agent"])
    assert fifth["root-cause-drift"]["status"] == "resolved"

    # Seen again after being resolved: reopened.
    sixth = reconcile_patterns(fifth, analysis["patterns"], run_id="aiq-r06", run_date="2026-08-25", comparable=True, exercised_agents=["weather-agent", "healthcare-agent", "finance-agent", "travel-agent"])
    assert sixth["root-cause-drift"]["status"] == "reopened"
    assert len(sixth["root-cause-drift"]["history"]) == 6


def test_pattern_absence_requires_same_policy_and_capability_coverage() -> None:
    analysis = _analysis()
    first = reconcile_patterns(
        {},
        analysis["patterns"],
        run_id="aiq-r01",
        run_date="2026-08-20",
        comparable=True,
        exercised_agents=["healthcare-agent", "finance-agent"],
        assessment_policy=assessment_policy_digest(),
        exercised_capability_names=["root_cause"],
        pattern_capabilities={"root-cause-drift": ["root_cause"]},
    )
    changed_policy = reconcile_patterns(
        first,
        [],
        run_id="aiq-r02",
        run_date="2026-08-21",
        comparable=True,
        exercised_agents=["healthcare-agent", "finance-agent"],
        assessment_policy="sha256:" + ("f" * 64),
        exercised_capability_names=["root_cause"],
    )
    assert changed_policy["root-cause-drift"]["status"] == "new"
    assert (
        changed_policy["root-cause-drift"]["last_evaluation"]
        == "not_evaluated"
    )
    assert changed_policy["root-cause-drift"]["comparable_absence_count"] == 0
    missing_capability = reconcile_patterns(
        first,
        [],
        run_id="aiq-r02",
        run_date="2026-08-21",
        comparable=True,
        exercised_agents=["healthcare-agent", "finance-agent"],
        assessment_policy=assessment_policy_digest(),
        exercised_capability_names=["severity"],
    )
    assert missing_capability["root-cause-drift"]["status"] == "new"
    assert (
        missing_capability["root-cause-drift"]["last_evaluation"]
        == "not_evaluated"
    )
    assert missing_capability["root-cause-drift"]["comparable_absence_count"] == 0


def test_reconcile_patterns_not_evaluated_when_not_comparable() -> None:
    analysis = _analysis()
    baseline_state = reconcile_patterns(
        {},
        analysis["patterns"],
        run_id="aiq-r01",
        run_date="2026-08-20",
        comparable=True,
        exercised_agents=["weather-agent", "healthcare-agent", "finance-agent", "travel-agent"],
    )

    # Whole run is not comparable (e.g. INCOMPLETE evidence): absence count unchanged.
    incomparable_run = reconcile_patterns(
        baseline_state,
        [],
        run_id="aiq-r02",
        run_date="2026-08-21",
        comparable=False,
        exercised_agents=["weather-agent", "healthcare-agent", "finance-agent", "travel-agent"],
    )
    assert incomparable_run["root-cause-drift"]["status"] == "new"
    assert (
        incomparable_run["root-cause-drift"]["last_evaluation"]
        == "not_evaluated"
    )
    assert incomparable_run["root-cause-drift"]["comparable_absence_count"] == 0

    # A previously supporting Agent was not exercised this run: also not_evaluated.
    reduced_matrix_run = reconcile_patterns(
        baseline_state,
        [],
        run_id="aiq-r03",
        run_date="2026-08-22",
        comparable=True,
        exercised_agents=["finance-agent", "travel-agent"],
    )
    assert reduced_matrix_run["root-cause-drift"]["status"] == "new"
    assert (
        reduced_matrix_run["root-cause-drift"]["last_evaluation"]
        == "not_evaluated"
    )
    assert reduced_matrix_run["root-cause-drift"]["comparable_absence_count"] == 0


def test_snapshot_and_living_markdown_render_without_leaking_private_content() -> None:
    report = _report()
    analysis = _analysis()
    validate_analysis(analysis)
    reconciled = reconcile_patterns(
        {},
        analysis["patterns"],
        run_id=report["run_id"],
        run_date=report["report_date"],
        comparable=True,
        exercised_agents=[item["agent"] for item in report["baseline"]],
    )
    snapshot = build_run_snapshot(report, analysis, reconciled)
    assert snapshot["analysis"]["patterns"][0]["why_it_is_a_pattern"]
    assert snapshot["analysis"]["improvement_priorities"] == (
        analysis["improvement_priorities"]
    )
    snapshot_markdown = render_snapshot_markdown(snapshot)
    assert "# Insight Engine Improvement Snapshot" in snapshot_markdown
    assert "Root cause replaced by downstream symptom" in snapshot_markdown
    assert "Separate root identification from card writing." in snapshot_markdown
    assert "healthcare-agent/issue-002/expected" in snapshot_markdown
    assert "## Evidence links" in snapshot_markdown

    living_state = build_living_state(
        None,
        report,
        analysis,
        reconciled,
        snapshot_link="daily/2026/08/24/insight-engine-improvement.md",
    )
    living_markdown = render_living_markdown(living_state)
    assert "# Insight Engine Improvement Memory" in living_markdown
    assert "## Improvement priorities" in living_markdown
    assert "## Cross-Agent patterns" in living_markdown
    assert "## Snapshot history" in living_markdown
    assert "Root cause replaced by downstream symptom" in living_markdown
    for forbidden in ("sha256:", "http://", "provider_id", "raw_trace"):
        assert forbidden not in living_markdown
        assert forbidden not in snapshot_markdown


def test_write_improvement_memory_is_immutable_per_run_and_daily_only(
    tmp_path: Path,
) -> None:
    reports_root = tmp_path / "reports"
    living_state_path = reports_root / "insight-engine-improvement.json"
    report = _report()
    analysis = _analysis()

    state = write_improvement_memory(
        report=report,
        analysis=analysis,
        reports_root=reports_root,
        living_state_path=living_state_path,
    )
    assert state["latest_run_id"] == report["run_id"]
    pattern = next(iter(state["patterns"].values()))
    assert pattern["priority"] == 1
    assert pattern["history"][0]["evaluation"] == "observed"
    assert all("finding_id" in item for item in pattern["evidence"])
    assert (reports_root / "insight-engine-improvement.md").exists()
    assert living_state_path.exists()
    snapshot_dir = reports_root / "daily" / "2026" / "08" / "24"
    assert (snapshot_dir / "insight-engine-improvement.json").exists()
    assert (snapshot_dir / "insight-engine-improvement.md").exists()
    living_markdown = (
        reports_root / "insight-engine-improvement.md"
    ).read_text(encoding="utf-8")
    assert (
        "daily/2026/08/24/agents/weather-agent.md"
        in living_markdown
    )
    snapshot = json.loads(
        (snapshot_dir / "insight-engine-improvement.json").read_text(
            encoding="utf-8"
        )
    )
    validate_published_improvement(
        report=report,
        living_state=state,
        living_markdown=living_markdown,
        snapshot=snapshot,
        snapshot_markdown=(
            snapshot_dir / "insight-engine-improvement.md"
        ).read_text(encoding="utf-8"),
    )

    # Re-running the exact same run resumes idempotently without reconciling
    # pattern history a second time.
    resumed = write_improvement_memory(
        report=report,
        analysis=analysis,
        reports_root=reports_root,
        living_state_path=living_state_path,
    )
    assert resumed == state
    (reports_root / "insight-engine-improvement.md").unlink()
    resumed_after_partial_write = write_improvement_memory(
        report=report,
        analysis=analysis,
        reports_root=reports_root,
        living_state_path=living_state_path,
    )
    assert resumed_after_partial_write == state
    assert (reports_root / "insight-engine-improvement.md").is_file()

    # A different run for the same report_date with different analysis
    # content must fail closed rather than silently overwrite the immutable
    # snapshot for that date.
    changed_analysis = deepcopy(analysis)
    changed_analysis["executive_summary"] = "A different executive summary."
    same_date_new_run = deepcopy(report)
    same_date_new_run["run_id"] = "aiq-20260824-rerun"
    with pytest.raises(ContractError, match="immutable"):
        write_improvement_memory(
            report=same_date_new_run,
            analysis=changed_analysis,
            reports_root=reports_root,
            living_state_path=living_state_path,
        )

    staging_report = deepcopy(report)
    staging_report["profile"] = "staging"
    with pytest.raises(ContractError, match="Official Daily"):
        write_improvement_memory(
            report=staging_report,
            analysis=analysis,
            reports_root=reports_root,
            living_state_path=living_state_path,
        )


def test_daily_report_and_memory_are_staged_and_published_together(
    tmp_path: Path,
) -> None:
    agents, issues = load_catalogs()
    selected = {
        agent["name"]: list(agent["issue_ids"][:4])
        for agent in agents["agents"]
    }
    report = build_operational_failure_report(
        report_date=date(2026, 8, 24),
        run_id="aiq-20260824",
        profile="daily",
        selected=selected,
        issues=issues,
        failure_code="synthetic_incomplete",
        catalog_hashes=catalog_hashes(agents, issues),
    )
    analysis = _analysis()
    analysis["patterns"] = []
    analysis["improvement_priorities"] = []
    reports_root = tmp_path / "reports"
    output = reports_root / "daily" / "2026" / "08" / "24"
    write_improvement_memory(
        report=report,
        analysis=analysis,
        reports_root=reports_root,
        living_state_path=reports_root / "insight-engine-improvement.json",
        report_output=output,
    )
    assert (output / "report.json").is_file()
    assert (output / "report.md").is_file()
    assert {
        path.stem for path in (output / "agents").glob("*.md")
    } == {agent["name"] for agent in agents["agents"]}
    assert (reports_root / "insight-engine-improvement.json").is_file()
    assert (reports_root / "insight-engine-improvement.md").is_file()


def test_failed_staging_writes_no_publication_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    agents, issues = load_catalogs()
    report = build_operational_failure_report(
        report_date=date(2026, 8, 24),
        run_id="aiq-20260824",
        profile="daily",
        selected={
            agent["name"]: list(agent["issue_ids"][:4])
            for agent in agents["agents"]
        },
        issues=issues,
        failure_code="synthetic_incomplete",
        catalog_hashes=catalog_hashes(agents, issues),
    )
    analysis = _analysis()
    analysis["patterns"] = []
    analysis["improvement_priorities"] = []
    monkeypatch.setattr(
        improvement_memory,
        "write_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ContractError("synthetic staged report failure")
        ),
    )
    reports_root = tmp_path / "reports"
    with pytest.raises(ContractError, match="staged report failure"):
        write_improvement_memory(
            report=report,
            analysis=analysis,
            reports_root=reports_root,
            living_state_path=reports_root / "insight-engine-improvement.json",
            report_output=reports_root / "daily" / "2026" / "08" / "24",
        )
    assert not reports_root.exists()


def test_email_only_improvement_preview_never_writes_living_state(
    tmp_path: Path,
) -> None:
    output = tmp_path / ".aiq-runtime" / "improvement-preview"
    snapshot = write_improvement_preview(
        report=_report(),
        analysis=_analysis(),
        output=output,
    )
    assert snapshot["run_id"] == "aiq-20260824"
    assert (
        output / "insight-engine-improvement-preview.json"
    ).is_file()
    assert (
        output / "insight-engine-improvement-preview.md"
    ).is_file()
    assert not (tmp_path / "reports").exists()


def test_incomplete_run_records_snapshot_but_cannot_mutate_pattern_memory(
    tmp_path: Path,
) -> None:
    reports_root = tmp_path / "reports"
    living_state_path = reports_root / "insight-engine-improvement.json"
    report = _report()
    analysis = _analysis()
    write_improvement_memory(
        report=report,
        analysis=analysis,
        reports_root=reports_root,
        living_state_path=living_state_path,
    )

    incomplete_report = _report(
        run_id="aiq-20260825",
        report_date="2026-08-25",
        incomplete=True,
    )
    incomplete_analysis = _analysis()
    state = write_improvement_memory(
        report=incomplete_report,
        analysis=incomplete_analysis,
        reports_root=reports_root,
        living_state_path=living_state_path,
    )
    # The pattern is preserved (not resolved/reopened/reprioritized) but its
    # status reflects that this run's evidence was not comparable.
    pattern = next(iter(state["patterns"].values()))
    assert pattern["status"] == "new"
    assert pattern["last_evaluation"] == "not_evaluated"
    assert pattern["comparable_absence_count"] == 0
    assert len(state["snapshot_history"]) == 2
    snapshot_dir = reports_root / "daily" / "2026" / "08" / "25"
    assert (snapshot_dir / "insight-engine-improvement.json").exists()
