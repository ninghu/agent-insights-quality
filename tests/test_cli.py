from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality import cli
from agent_insights_quality.util import ContractError, atomic_json, content_hash


def test_daily_commands_replace_monolithic_run_daily() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run-daily"])
    assert parser.parse_args(["daily-status"]).command == "daily-status"
    assert parser.parse_args(["daily-guide"]).command == "daily-guide"


def test_unresolved_insight_state_blocks_immutable_manifest() -> None:
    store = SimpleNamespace(has_unresolved_insight_state=lambda: True)

    with pytest.raises(ContractError, match="resume before creating"):
        cli._assert_insight_state_resolved(store)


def test_daily_agent_parser_keeps_each_lane_whole() -> None:
    args = cli.build_parser().parse_args(
        ["daily-run-agent", "--agent", "finance-agent"]
    )
    assert args.agent == "finance-agent"


def test_test_finalization_stays_private_and_skips_adx(
    monkeypatch,
    tmp_path: Path,
) -> None:
    private_root = tmp_path / ".aiq-runtime" / "runtime"
    state = private_root / "daily" / "aiq-20260828-r01"
    output_root = tmp_path / "reports"
    state.mkdir(parents=True)
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", str(private_root))
    manifest = {
        "schema_version": "5.0.0",
        "run_id": "aiq-20260828-r01",
        "profile": "daily",
        "delivery_mode": "test_email_only",
        "report_date": "2026-08-28",
        "test_region": "WestUS2",
        "test_region_registry": "WestUS2",
        "agents": [{"name": "weather-agent", "issues": [{"issue_id": "issue-001"}]}],
    }
    work_items = {"schema_version": "synthetic"}
    manifest_path = state / "run-manifest.json"
    work_items_path = private_root / "work-items.json"
    atomic_json(manifest_path, manifest)
    atomic_json(work_items_path, work_items)
    atomic_json(
        state / "work-items-reference.json",
        {
            "schema_version": "1.0.0",
            "run_id": manifest["run_id"],
            "report_date": manifest["report_date"],
            "content_digest": content_hash(work_items),
        },
    )
    report = {
        "profile": "daily",
        "report_date": "2026-08-28",
        "run_id": manifest["run_id"],
        "summary": {"quality_score": 77.3},
        "delivery": {"content_digest": "sha256:" + "0" * 64},
    }
    improvement_analysis = {
        "schema_version": "1.0.0",
        "model": "gpt-5.6-sol",
        "executive_summary": "No cross-Agent pattern was identified.",
        "patterns": [],
        "isolated_observations": [],
        "improvement_priorities": [],
        "exclusions": [],
    }
    improvement_path = state / "improvement-analysis.json"
    atomic_json(improvement_path, improvement_analysis)

    monkeypatch.setattr(cli, "load_catalogs", lambda: ({"agents": []}, {"issues": []}))
    monkeypatch.setattr(cli, "catalog_hashes", lambda *_args: {})
    monkeypatch.setattr(cli, "validate_manifest", lambda _value: None)
    monkeypatch.setattr(
        cli,
        "assert_daily_finalization_inputs",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(cli, "load_quality_work_items", lambda *_args, **_kwargs: work_items)
    monkeypatch.setattr(cli, "load_assessments", lambda *_args: {})
    monkeypatch.setattr(cli, "load_baseline_assessments", lambda *_args: {})
    monkeypatch.setattr(cli, "build_report", lambda *_args: report)
    monkeypatch.setattr(
        cli,
        "build_normalized_summary",
        lambda _report: {
            "coverage": {},
            "insight_engine_findings": [],
            "exclusions": [],
        },
    )
    monkeypatch.setattr(cli, "apply_score_comparison", lambda *_args: None)
    monkeypatch.setattr(
        cli,
        "resolve_recipient",
        lambda *, test_run: "synthetic-user@microsoft.com"
        if test_run
        else pytest.fail("official recipient used"),
    )
    monkeypatch.setattr(
        cli.RuntimeProfile,
        "from_env",
        staticmethod(lambda _profile: SimpleNamespace()),
    )
    monkeypatch.setattr(cli, "build_runtime_links", lambda *_args: (None, {}))
    monkeypatch.setattr(
        cli,
        "publish_daily_report_best_effort",
        lambda *_args, **_kwargs: pytest.fail("ADX publication attempted"),
    )
    monkeypatch.setattr(
        cli,
        "resolve_dashboard_link",
        lambda: pytest.fail("dashboard resolution attempted"),
    )

    report_link_flags = []

    def write_report(
        value: dict,
        output: Path,
        **kwargs,
    ) -> None:
        report_link_flags.append(kwargs["include_improvement_link"])
        output.mkdir(parents=True, exist_ok=True)
        atomic_json(output / "report.json", value)

    def create_request(_report: dict, _recipient: str, **kwargs) -> dict:
        assert kwargs["test_run"] is True
        assert kwargs["dashboard_link"] is None
        assert kwargs["adx_publication"] == {
            "status": "skipped_test",
            "error_code": None,
        }
        return {
            "content_digest": "sha256:" + "a" * 64,
            "html": "<!doctype html><html></html>",
        }

    monkeypatch.setattr(cli, "write_report", write_report)
    previews = []
    monkeypatch.setattr(
        cli,
        "write_improvement_preview",
        lambda **kwargs: previews.append(kwargs),
    )
    monkeypatch.setattr(cli, "create_request", create_request)

    args = cli.build_parser().parse_args(
        [
            "finalize",
            "--manifest",
            str(manifest_path),
            "--work-items",
            str(work_items_path),
            "--assessment",
            str(state / "assessment.json"),
            "--baseline-assessment",
            str(state / "baseline-assessment.json"),
            "--output-root",
            str(output_root),
            "--improvement-analysis",
            str(improvement_path),
        ]
    )
    result = json.loads(cli._dispatch(args) or "{}")

    assert result["delivery_mode"] == "test_email_only"
    assert result["adx_publication"] == "skipped_test"
    assert result["generated_report"] is False
    assert result["pull_request"] == "skipped_test"
    assert (state / "final-report" / "report.json").is_file()
    assert len(previews) == 1
    assert report_link_flags == [False, False]
    assert not output_root.exists()


def test_official_daily_defers_report_to_atomic_memory_publication(
    monkeypatch,
    tmp_path: Path,
) -> None:
    private_root = tmp_path / ".aiq-runtime" / "runtime"
    state = private_root / "daily" / "aiq-20260828"
    output_root = tmp_path / "reports"
    state.mkdir(parents=True)
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", str(private_root))
    manifest = {
        "schema_version": "5.0.0",
        "run_id": "aiq-20260828",
        "profile": "daily",
        "delivery_mode": "official",
        "report_date": "2026-08-28",
        "test_region": "WestUS2",
        "test_region_registry": "WestUS2",
        "agents": [{"name": "weather-agent", "issues": [{"issue_id": "issue-001"}]}],
    }
    work_items = {"schema_version": "synthetic"}
    manifest_path = state / "run-manifest.json"
    work_items_path = private_root / "work-items.json"
    atomic_json(manifest_path, manifest)
    atomic_json(work_items_path, work_items)
    atomic_json(
        state / "work-items-reference.json",
        {
            "schema_version": "1.0.0",
            "run_id": manifest["run_id"],
            "report_date": manifest["report_date"],
            "content_digest": content_hash(work_items),
        },
    )
    report = {
        "profile": "daily",
        "report_date": "2026-08-28",
        "run_id": manifest["run_id"],
        "summary": {"quality_score": 77.3},
        "delivery": {"content_digest": "sha256:" + "0" * 64},
    }
    improvement_path = state / "improvement-analysis.json"
    atomic_json(
        improvement_path,
        {
            "schema_version": "1.0.0",
            "model": "gpt-5.6-sol",
            "executive_summary": "No cross-Agent pattern was identified.",
            "patterns": [],
            "isolated_observations": [],
            "improvement_priorities": [],
            "exclusions": [],
        },
    )
    monkeypatch.setattr(cli, "load_catalogs", lambda: ({"agents": []}, {"issues": []}))
    monkeypatch.setattr(cli, "catalog_hashes", lambda *_args: {})
    monkeypatch.setattr(cli, "validate_manifest", lambda _value: None)
    monkeypatch.setattr(
        cli,
        "assert_daily_finalization_inputs",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(cli, "load_quality_work_items", lambda *_args, **_kwargs: work_items)
    monkeypatch.setattr(cli, "load_assessments", lambda *_args: {})
    monkeypatch.setattr(cli, "load_baseline_assessments", lambda *_args: {})
    monkeypatch.setattr(cli, "build_report", lambda *_args: report)
    monkeypatch.setattr(
        cli,
        "build_normalized_summary",
        lambda _report: {
            "coverage": {},
            "insight_engine_findings": [],
            "exclusions": [],
        },
    )
    monkeypatch.setattr(cli, "apply_score_comparison", lambda *_args: None)
    monkeypatch.setattr(cli, "resolve_recipient", lambda **_kwargs: "team@example.com")
    monkeypatch.setattr(
        cli.RuntimeProfile,
        "from_env",
        staticmethod(lambda _profile: SimpleNamespace()),
    )
    monkeypatch.setattr(cli, "build_runtime_links", lambda *_args: (None, {}))
    monkeypatch.setattr(cli, "resolve_dashboard_link", lambda: None)
    monkeypatch.setattr(
        cli,
        "write_report",
        lambda *_args, **_kwargs: pytest.fail(
            "Official report bypassed atomic publication"
        ),
    )

    def publish(_report, *, source_path, catalogs):
        del catalogs
        assert source_path.is_file()
        return {"status": "success", "error_code": None}

    monkeypatch.setattr(cli, "publish_daily_report_best_effort", publish)
    monkeypatch.setattr(
        cli,
        "create_request",
        lambda *_args, **_kwargs: {
            "content_digest": "sha256:" + "a" * 64,
            "html": "<!doctype html><html></html>",
        },
    )
    publications = []

    def publish_set(**kwargs):
        publications.append(kwargs)
        output = kwargs["report_output"]
        output.mkdir(parents=True, exist_ok=True)
        atomic_json(output / "report.json", kwargs["report"])
        (output / "report.md").write_text("synthetic report\n", encoding="utf-8")

    monkeypatch.setattr(cli, "write_improvement_memory", publish_set)
    monkeypatch.setattr(cli, "update_trend", lambda *_args: None)

    args = cli.build_parser().parse_args(
        [
            "finalize",
            "--manifest",
            str(manifest_path),
            "--work-items",
            str(work_items_path),
            "--assessment",
            str(state / "assessment.json"),
            "--baseline-assessment",
            str(state / "baseline-assessment.json"),
            "--output-root",
            str(output_root),
            "--improvement-analysis",
            str(improvement_path),
        ]
    )
    result = json.loads(cli._dispatch(args) or "{}")
    assert result["generated_report"] is True
    assert len(publications) == 1
    assert publications[0]["report"]["delivery"]["content_digest"] == (
        "sha256:" + "a" * 64
    )
    assert publications[0]["report_output"] == (
        output_root / "daily" / "2026" / "08" / "28"
    )
