from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality import cli
from agent_insights_quality.util import ContractError, atomic_json, content_hash


def test_test_run_requires_nonzero_rerun(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "load_catalogs", lambda: ({}, {}))
    monkeypatch.setattr(cli, "catalog_hashes", lambda *_args: {})
    args = cli.build_parser().parse_args(
        [
            "run-daily",
            "--report-date",
            "2026-08-28",
            "--work-items",
            str(tmp_path / "work-items.json"),
            "--test-run",
        ]
    )
    with pytest.raises(ContractError, match="nonzero --rerun"):
        cli._dispatch(args)


def test_unresolved_insight_state_blocks_immutable_manifest() -> None:
    store = SimpleNamespace(has_unresolved_insight_state=lambda: True)

    with pytest.raises(ContractError, match="resume before creating"):
        cli._assert_insight_state_resolved(store)


def test_post_manifest_failure_does_not_publish_operational_result(
    monkeypatch,
    tmp_path: Path,
) -> None:
    private_root = tmp_path / ".aiq-runtime" / "runtime"
    monkeypatch.setenv("AIQ_RUNTIME_ROOT", str(private_root))
    monkeypatch.setattr(cli, "load_catalogs", lambda: ({}, {}))
    monkeypatch.setattr(
        cli,
        "catalog_hashes",
        lambda *_args: {"issues": "sha256:" + "1" * 64},
    )
    monkeypatch.setattr(
        cli,
        "load_automation_policy",
        lambda: SimpleNamespace(
            insight_lookback_hours=0.1,
            clean_window_poll_seconds=1,
            clean_window_ingestion_margin_seconds=1,
            clean_window_max_wait_seconds=1,
            trace_assertion_stabilization_seconds=1,
            insight_start_margin_seconds=1,
            max_recovery_versions=3,
            agent_start_stagger_seconds=1,
            telemetry_resource_set="g29",
        ),
    )
    monkeypatch.setattr(cli, "load_quality_work_items", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(cli, "select_daily", lambda *_args: {})
    profile = SimpleNamespace(
        registry_path=private_root / "registry.json",
        assert_insights_connection=lambda: None,
        assert_test_agent_model=lambda _model: None,
        resolve_test_region=lambda: "WestUS2",
    )
    monkeypatch.setattr(
        cli.RuntimeProfile,
        "from_env",
        staticmethod(lambda _profile: profile),
    )
    monkeypatch.setattr(cli, "agent_model_contract", lambda _agents: {})
    monkeypatch.setattr(cli, "sync_registry", lambda _profile: None)
    monkeypatch.setattr(
        cli,
        "load_registry",
        lambda *_args, **_kwargs: {"test_region": "WestUS2"},
    )
    monkeypatch.setattr(cli, "LiveRuntime", lambda _profile: SimpleNamespace())
    monkeypatch.setattr(cli, "_run_contract_digest", lambda **_kwargs: "digest")
    monkeypatch.setattr(cli, "execute", lambda **_kwargs: [])
    monkeypatch.setattr(
        cli,
        "build_manifest",
        lambda **_kwargs: {"run_id": "aiq-20260828", "checkpointed": True},
    )
    monkeypatch.setattr(
        cli,
        "_rehydrate_with_retries",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("synthetic lock release failure")
        ),
    )
    monkeypatch.setattr(
        cli,
        "publish_daily_report_best_effort",
        lambda *_args, **_kwargs: pytest.fail("ADX publication attempted"),
    )
    monkeypatch.setattr(
        cli,
        "build_operational_failure_report",
        lambda **_kwargs: pytest.fail("operational report built"),
    )
    args = cli.build_parser().parse_args(
        [
            "run-daily",
            "--report-date",
            "2026-08-28",
            "--work-items",
            str(private_root / "work-items.json"),
            "--state-root",
            str(private_root),
        ]
    )

    with pytest.raises(ContractError, match="evidence was checkpointed"):
        cli._dispatch(args)


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
        "status": "FAIL",
        "profile": "daily",
        "report_date": "2026-08-28",
        "run_id": manifest["run_id"],
        "summary": {},
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
