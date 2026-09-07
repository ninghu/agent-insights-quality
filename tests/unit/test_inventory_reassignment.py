"""Synthetic frozen inventories remain historical, not current-catalog aliases."""

from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest
import yaml

from agent_insights_quality.catalogs import load_catalog
from agent_insights_quality.email import EmailRecord, EmailRequest
from agent_insights_quality.email_preview import _frozen_inputs, export_email_preview
from agent_insights_quality.private_publication import PrivateReportOutbox
from agent_insights_quality.privacy import restore_public_result
from agent_insights_quality.report_context import ReportContextError, load_report_context
from agent_insights_quality.results import (
    CardVerdict, CoreVerdict, PlannedUnit, UnitResult, aggregate_results,
)
from agent_insights_quality.runner import choose_staging, reconcile_staging_work
from agent_insights_quality.scoring import LEGACY_SCORING_POLICY, SCORING_POLICY
from agent_insights_quality.selection import SourceChanges, select_daily
from agent_insights_quality.state import RuntimeStore
from agent_insights_quality.errors import QualityError
from test_private_publication import ENVIRONMENT, SOURCE, seed
from test_runner import fake_storage


ROOT = Path(__file__).resolve().parents[2]
DAY = date(2026, 9, 6)


@pytest.fixture
def historical_catalog(tmp_path, monkeypatch):
    fake_storage(monkeypatch)
    documents = deepcopy(load_catalog(ROOT)._documents)
    agents, issues = documents
    by_name = {agent["name"]: agent for agent in agents["agents"]}
    by_name["healthcare-agent"]["issue_ids"].insert(0, "issue-007")
    by_name["support-ticket-agent"]["issue_ids"].remove("issue-007")
    issue = next(issue for issue in issues["issues"] if issue["id"] == "issue-007")
    issue.update(
        agent="healthcare-agent", implementation="agents/healthcare-agent/issues/issue-007",
        title="Scheduling handoff fields omitted", validation_mode="model_mediated",
        root_cause="Synthetic historical handoff behavior under the earlier reviewed prompt contract.",
        expected_fix="Preserve the earlier reviewed scheduling handoff requirement.",
    )
    root = tmp_path / "historical-reviewed-source"
    (root / "catalogs").mkdir(parents=True)
    for name, doc in zip(("AGENT_CATALOG.yaml", "ISSUE_CATALOG.yaml"), documents, strict=True):
        (root / "catalogs" / name).write_text(yaml.safe_dump(doc), encoding="utf-8")
    return load_catalog(root)


def test_current_report_context_uses_hosted_handoff_paths_and_support_assignment():
    catalog = load_catalog(ROOT)
    target = catalog.target("support-ticket-agent/issue-007")
    plan = (PlannedUnit(target.unit_id, "issue-007"),)
    context = load_report_context(ROOT, allowed_units=plan)
    row = context.for_plan(plan)[target.unit_id]
    assert row.title == "Support handoff fields omitted"
    assert row.source_path == "agents/support-ticket-agent/issues/issue-007/source/"
    assert row.traffic_path == "agents/support-ticket-agent/issues/issue-007/traffic.json"
    owner = next(
        agent["owner"] for agent in catalog._documents[0]["agents"]
        if agent["name"] == "support-ticket-agent"
    )
    assert context.assignments[target.unit_id.agent] == owner


@pytest.mark.parametrize("rerun,policy", [(7, LEGACY_SCORING_POLICY), (8, SCORING_POLICY)])
def test_exact_frozen_export_and_publication_restore_without_current_rotation(
    historical_catalog, tmp_path, monkeypatch, rerun, policy,
):
    from agent_insights_quality import email_preview

    monkeypatch.setattr(email_preview, "_renderer_provenance", lambda root: {
        "source_revision": "b" * 40, "module_sha256": {"synthetic.py": "c" * 64},
        "content_sha256": "d" * 64,
    })
    targets = select_daily(historical_catalog, DAY, test_run=True)
    plan = tuple(PlannedUnit(t.unit_id, None if t.is_baseline else t.unit_id.logical_version) for t in targets)
    current_targets = select_daily(load_catalog(ROOT), DAY, test_run=True)
    current_plan = tuple(
        PlannedUnit(t.unit_id, None if t.is_baseline else t.unit_id.logical_version)
        for t in current_targets
    )
    assert len(plan) == len(current_plan) == 25 and plan != current_plan
    for agent in ("weather-agent", "finance-agent", "travel-agent"):
        assert [p for p in plan if p.unit_id.agent == agent] == [
            p for p in current_plan if p.unit_id.agent == agent
        ]
    assert sum(p.expected_issue_alias is not None for p in plan) == 20
    assert any(t.key == "healthcare-agent/issue-007" for t in targets)
    result = aggregate_results(plan, tuple(
        UnitResult(p.unit_id, cards=(
            (CardVerdict("card-0001", CoreVerdict.CORRECT, p.expected_issue_alias),)
            if p.expected_issue_alias else ()
        )) for p in plan
    ), scoring_policy=policy)
    runtime = RuntimeStore("daily", root=tmp_path / "synthetic-private")
    with runtime.ownership():
        run_id = seed(runtime, result, plan, day=DAY.isoformat(), test_run=True, rerun=rerun)
        outbox = PrivateReportOutbox(runtime, run_id)
        archive = outbox.prepare(
            historical_catalog.root, result, allowed_units=plan, environment=ENVIRONMENT,
            source_revision=SOURCE, report_date=DAY.isoformat(), test_run=True,
        )
        request = EmailRequest(
            run_id, "synthetic-history@example.invalid", "[TEST] Frozen historical presentation",
            "<html><body>Frozen synthetic historical report</body></html>", "test",
            DAY.isoformat(), True, rerun,
        )
        runtime.outbox("email").save_progress(run_id, EmailRecord(request, "prepared").to_private_dict())
        runtime.run(run_id).save_completed("delivery-inputs", {
            "report": result.to_dict(), "test_run": True, "rerun": rerun,
            "report_date": DAY.isoformat(), "region_display": ENVIRONMENT.region_display,
            "source_revision": SOURCE, "private_context": None, "warnings": [],
            "recipient": request.recipient,
            "report_context": load_report_context(historical_catalog.root, allowed_units=plan).to_private_dict(),
        })
    before = {path: path.read_bytes() for path in runtime.directory.rglob("*") if path.is_file()}
    _, restored_plan, restored, metadata = _frozen_inputs(runtime, request)
    assert restored_plan == plan and restored.to_dict() == result.to_dict()
    assert metadata.report_date == DAY.isoformat() and restored.scoring_policy == policy
    assert PrivateReportOutbox(runtime, run_id).request() == archive
    with pytest.raises(QualityError):
        restore_public_result(result.to_dict(), allowed_units=current_plan)
    with runtime.ownership():
        exact = export_email_preview(runtime, run_id, root=ROOT)
        assert (exact.directory / "email.html").read_bytes() == request.html.encode("utf-8")
        with pytest.raises(ReportContextError, match="report_context_identity_mismatch"):
            export_email_preview(runtime, run_id, root=ROOT, restyle=True)
    assert all(path.read_bytes() == content for path, content in before.items())


def test_retired_staging_binding_is_preserved_and_new_owner_is_not_evidence_reuse(
    historical_catalog, tmp_path,
):
    current = load_catalog(ROOT)
    runtime = RuntimeStore("staging", root=tmp_path / "synthetic-private")
    run_id = "staging-synthetic-history"
    retired = "healthcare-agent/issue-007"
    new = "support-ticket-agent/issue-007"
    with runtime.ownership():
        records = runtime.run(run_id)
        records.save_completed("run", {
            "kind": "staging", "started_at": "2026-09-06T10:00:00Z",
            "targets": [target.key for target in historical_catalog.targets],
        })
        records.save_completed("environment", {"profile": "staging"})
        for target in historical_catalog.targets:
            binding = {
                "source_revision": SOURCE, "traffic_source_revision": SOURCE,
                "traffic_run_id": run_id, "work_key": f"targets/{target.key}/work-original",
                "tested_at": "2026-09-06", "status": "FAIL" if target.key == retired else "PASS",
                "result": {"status": "FAIL" if target.key == retired else "PASS"},
            }
            records.save_progress(f"targets/{target.key}/source", binding)
            runtime.staging_index.save_progress(target.key, {**binding, "binding_run_id": run_id})
            runtime.outbox("staging").save_progress(f"targets/{target.key}", {
                "run_id": run_id, "work_key": binding["work_key"],
            })
        retired_bytes = {
            path: path.read_bytes()
            for path in runtime.directory.rglob("*.json")
            if "healthcare-agent" in path.parts and ("issue-007" in path.parts or path.stem == "issue-007")
        }
        assert len(retired_bytes) == 3
        retired_index = runtime.staging_index.read(retired)
        reconcile_staging_work(current, runtime)
    selections = choose_staging(current, runtime, changes_since=lambda _: SourceChanges(()))
    assert [(s.target.key, s.action, s.reasons) for s in selections] == [(new, "traffic", ("missing",))]
    assert runtime.staging_index.read(retired) == retired_index
    assert runtime.staging_index.read(new, missing_ok=True) is None
    assert all(path.read_bytes() == content for path, content in retired_bytes.items())

    paths = [
        Path("catalogs/AGENT_CATALOG.yaml"), Path("catalogs/ISSUE_CATALOG.yaml"),
        *[target.version_root / "source" / "domain.py" for target in current.for_agent("support-ticket-agent")],
        current.target("support-ticket-agent/v0").version_root / "traffic.json",
    ]
    selections = choose_staging(current, runtime, changes_since=lambda _: SourceChanges(tuple(paths)))
    traffic = {s.target.key for s in selections if s.action == "traffic"}
    assert traffic == {t.key for t in current.for_agent("support-ticket-agent")}
    assert len(traffic) == 10
    assert len([s for s in selections if s.action == "reassess"]) == 31
    assert retired not in {s.target.key for s in selections}
    assert runtime.staging_index.read(retired) == retired_index
