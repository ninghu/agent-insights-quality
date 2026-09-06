from copy import deepcopy
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_insights_quality import cli, email, email_preview
from agent_insights_quality.email import EmailRecord, EmailRequest, TEAM_RECIPIENT
from agent_insights_quality.email_preview import PreviewError, export_email_preview
from agent_insights_quality.errors import QualityError
from agent_insights_quality.report_context import load_report_context
from agent_insights_quality.results import (
    CardVerdict, CoreVerdict, PlannedUnit, UnitId, UnitResult, aggregate_results,
)
from agent_insights_quality.state import CheckpointError, RuntimeStore, StateConflict, StateError


ROOT = Path(__file__).resolve().parents[2]
DAY = "2026-09-04"
SOURCE = "a" * 40
RENDERER = "b" * 40
PRIVATE = "synthetic-preview@example.invalid"
_actual_provenance = email_preview._renderer_provenance


@pytest.fixture(autouse=True)
def offline_renderer(monkeypatch):
    monkeypatch.setattr(email_preview, "_renderer_provenance", lambda root: {
        "source_revision": RENDERER, "module_sha256": {"synthetic.py": "c" * 64},
        "content_sha256": "e" * 64,
    })


def quality(*, failed=False, scoring_policy=None):
    baseline = PlannedUnit(UnitId("weather-agent", "v0"))
    issue = PlannedUnit(UnitId("weather-agent", "issue-001"), "issue-001")
    missed = PlannedUnit(UnitId("weather-agent", "issue-002"), "issue-002")
    plan = (baseline, issue, missed)
    measured = (
        UnitResult(baseline.unit_id, cards=(CardVerdict("card-0001", CoreVerdict.INCORRECT),)),
        UnitResult(issue.unit_id, cards=(
            CardVerdict("card-0001", CoreVerdict.CORRECT, "issue-001"),
            CardVerdict("card-0002", CoreVerdict.CORRECT, "issue-001"),
        )),
        UnitResult(missed.unit_id),
    )
    kwargs = {"scoring_policy": scoring_policy} if scoring_policy is not None else {}
    return aggregate_results(plan, () if failed else measured, **kwargs), plan


def seed(
    runtime, *, mode="test", frozen=True, mutate=None, status="prepared", html=None,
    scoring_policy=None, delivery_binding=None,
):
    failed = mode in {"failure", "failed-test"}
    test_run = mode in {"test", "failed-test"}
    result, plan = quality(failed=failed, scoring_policy=scoring_policy)
    delivery_id = "daily-" + DAY + ("-test-2" if test_run else "")
    request = EmailRequest(
        delivery_id, PRIVATE if test_run or failed else (
            delivery_binding["to_address"] if delivery_binding else TEAM_RECIPIENT
        ),
        "[TEST Full] Original frozen subject — café ☕",
        html or (
            "<!doctype html><html><body><h1>Original frozen presentation — café ☕</h1>"
            f"<p>Original score: {result.score}</p></body></html>"
        ),
        "test" if test_run else mode, DAY, test_run, 2 if test_run else 0,
        delivery_binding=delivery_binding,
    )
    with runtime.ownership():
        box = runtime.outbox("email")
        box.save_progress(delivery_id, EmailRecord(
            request, status, None if status == "prepared" else "existing-app",
            {"synthetic": "existing-outcome"} if status not in {"prepared", "claimed"} else None,
        ).to_private_dict())
        records = runtime.run(delivery_id)
        records.save_completed("run", {
            "kind": "daily", "report_date": DAY, "source_revision": SOURCE,
            "test_run": test_run, "rerun": request.rerun,
            "targets": [f"{unit.unit_id.agent}/{unit.unit_id.logical_version}" for unit in plan],
            "lane_run_id": delivery_id, "reuse_run_id": None,
        })
        # A mutable latest pointer must never supply the preview's measurement.
        records.save_progress("quality-result", {"artifact": "nonexistent-later-result"})
        if frozen:
            inputs = {
                "report": result.to_dict(), "test_run": test_run, "rerun": request.rerun,
                "report_date": DAY, "region_display": "Sweden Central", "source_revision": SOURCE,
                "private_context": "Synthetic frozen private optional context",
                "warnings": ["work_item_unavailable"], "recipient": PRIVATE,
                "report_context": load_report_context(ROOT, allowed_units=plan).to_private_dict(),
                "configured_assessor": None,
                **({"delivery_binding": delivery_binding} if delivery_binding is not None else {}),
            }
            if mutate:
                mutate(inputs)
            records.save_completed("delivery-inputs", inputs)
    return request, result, plan


@pytest.fixture
def runtime(tmp_path):
    return RuntimeStore("daily", root=tmp_path / "private")


def originals(runtime):
    return {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for directory in (runtime.directory / "runs", runtime.directory / "outboxes")
        for path in directory.rglob("*") if path.is_file()
    }


def exported(runtime, request, **kwargs):
    with runtime.ownership():
        return export_email_preview(runtime, request.delivery_id, root=ROOT, **kwargs)


def message(preview):
    return BytesParser(policy=policy.default).parsebytes((preview.directory / "email.eml").read_bytes())


@pytest.mark.parametrize("status", ["prepared", "claimed", "accepted", "delivered", "unknown"])
def test_exact_exports_preserve_prepared_fields_and_every_existing_record(runtime, status):
    request, result, _ = seed(runtime, status=status)
    before = originals(runtime)
    preview = exported(runtime, request)
    mail = message(preview)
    assert str(mail["To"]) == request.recipient
    assert str(mail["Subject"]) == request.subject
    assert mail["X-Unsent"] == "1"
    assert mail.get_body(preferencelist=("html",)).get_content() == request.html
    assert (preview.directory / "email.html").read_bytes() == request.html.encode("utf-8")
    attachment, = mail.iter_attachments()
    assert attachment.get_filename() == "report.md"
    assert attachment.get_content() == (preview.directory / "report.md").read_text(encoding="utf-8")
    assert str(result.score) in attachment.get_content()
    manifest = json.loads((preview.directory / "manifest.json").read_text())
    assert manifest["export_kind"] == "exact_prepared_request"
    assert manifest["prepared_status_observed"] == status
    assert not manifest["send_authorized"] and not manifest["measurement_changed"]
    assert manifest["measurement_source_revision"] == SOURCE
    assert preview.directory.parent.parent == runtime.directory / "previews"
    assert originals(runtime) == before


@pytest.mark.parametrize("mode", ["official", "failure"])
@pytest.mark.parametrize("policy_name", ["v1", "v2"])
def test_unified_official_routing_survives_exact_restyle_and_rescore_preview(runtime, mode, policy_name):
    from datetime import date
    from agent_insights_quality.automation_launch import resolve_launch
    from agent_insights_quality.delivery_recipient import freeze_private_recipient
    from agent_insights_quality.scoring import SCORING_POLICY, LEGACY_SCORING_POLICY
    with runtime.ownership():
        config = runtime.root / "config" / "email-recipient.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({
            "schema_version": "1.0.0", "purpose": "daily_test", "recipient": PRIVATE,
        }))
        launch = resolve_launch(
            runtime, report_mode="official", to_address="authorized-official@example.test",
            today=date.fromisoformat(DAY), source=lambda: SOURCE,
        )
        freeze_private_recipient(runtime, launch["run_id"], test_run=False)
    request, _, _ = seed(
        runtime, mode=mode, delivery_binding=launch,
        scoring_policy=LEGACY_SCORING_POLICY if policy_name == "v1" else SCORING_POLICY,
    )
    before = originals(runtime)
    for flags in ({}, {"restyle": True}, {"restyle": True, "rescore": True}):
        preview = exported(runtime, request, **flags)
        assert str(message(preview)["To"]) == request.recipient
        assert originals(runtime) == before
    with runtime.ownership():
        assert email.claim_email(runtime.outbox("email"), request.delivery_id, claim_id="native") == request


def test_restyle_uses_frozen_result_current_style_and_distinct_local_identity(runtime):
    request, result, _ = seed(runtime)
    before = originals(runtime)
    exact = exported(runtime, request)
    styled = exported(runtime, request, restyle=True)
    html = (styled.directory / "email.html").read_text(encoding="utf-8")
    detailed = (styled.directory / "report.html").read_text(encoding="utf-8")
    mail = message(styled)
    eml_html = mail.get_body(preferencelist=("html",)).get_content()
    assert styled.directory != exact.directory
    assert "Original frozen presentation" not in html
    assert f"{result.score:.1f}" in html and "LOCAL PRESENTATION PREVIEW" in html
    assert SOURCE not in html and SOURCE in detailed and "renderer provenance are in manifest.json" in html
    assert "Full" not in str(mail["Subject"]) and "Partial" not in str(mail["Subject"])
    assert "LOCAL PRESENTATION PREVIEW" in mail["Subject"]
    assert "report.html" in html
    assert 'href="report.html' not in eml_html
    assert "Open the attached report.md" in eml_html
    assert "cid:" not in eml_html and "file:" not in eml_html
    assert "Synthetic frozen private optional context" not in eml_html
    assert "Unsupported factual answer" in detailed
    assert 'id="weather-agent"' in detailed
    assert "Generated insight(s)" in detailed
    assert mail["X-AIQ-Local-Preview"] == "presentation-restyle"
    assert originals(runtime) == before
    manifest = json.loads((styled.directory / "manifest.json").read_text())
    assert manifest["reviewed_units_match"] is True
    assert manifest["renderer"]["source_revision"] == RENDERER
    assert manifest["measurement_source_revision"] == SOURCE
    assert manifest["prepared_subject"] == request.subject


def test_rescoring_exports_new_policy_without_changing_measurement_or_prepared_mail(runtime):
    from agent_insights_quality.privacy import restore_public_result
    from agent_insights_quality.scoring import LEGACY_SCORING_POLICY, SCORING_POLICY

    request, original, plan = seed(runtime, scoring_policy=LEGACY_SCORING_POLICY)
    before = originals(runtime)
    exact = exported(runtime, request)
    preview = exported(runtime, request, restyle=True, rescore=True)
    assert preview.rescored and preview.directory != exact.directory
    derived = json.loads((preview.directory / "result.json").read_text())
    result = restore_public_result(derived, allowed_units=plan)
    assert original.score == 30.8 and result.score == 36.4
    assert result.scoring_policy == SCORING_POLICY
    assert result.units == original.units and result.counts == original.counts
    assert result.coverage == original.coverage and result.status == original.status
    assert result.failure_reasons == original.failure_reasons
    html = (preview.directory / "email.html").read_text(encoding="utf-8")
    assert "LOCAL SCORING PREVIEW" in html and "36.4" in html
    assert "report.html" in html and "?sig=" not in html
    mail = message(preview)
    assert mail["X-AIQ-Local-Preview"] == "scoring-rescore"
    assert "LOCAL SCORING PREVIEW" in mail["Subject"] and "36.4" in mail["Subject"]
    assert str(mail["To"]) == request.recipient
    assert originals(runtime) == before
    manifest = json.loads((preview.directory / "manifest.json").read_text())
    assert manifest["export_kind"] == "local_scoring_preview"
    assert not manifest["send_authorized"] and not manifest["measurement_changed"]
    provenance = manifest["scoring_derivation"]
    assert provenance["source_policy"] == original.to_dict()["scoring_policy"]
    assert provenance["derived_policy"] == result.to_dict()["scoring_policy"]
    assert provenance["source_score"] == 30.8 and provenance["derived_score"] == 36.4
    assert not provenance["classifications_changed"] and not provenance["coverage_changed"]
    assert preview.to_dict()["result_path"] == str(preview.directory / "result.json")
    repeated = exported(runtime, request, restyle=True, rescore=True)
    assert repeated.directory == preview.directory and originals(runtime) == before


def test_rescoring_keeps_failed_measurements_unscored(runtime):
    from agent_insights_quality.scoring import LEGACY_SCORING_POLICY

    request, original, _ = seed(runtime, mode="failed-test", scoring_policy=LEGACY_SCORING_POLICY)
    before = originals(runtime)
    preview = exported(runtime, request, restyle=True, rescore=True)
    result = json.loads((preview.directory / "result.json").read_text())
    assert result["score"] is None and result["status"] == original.status.value
    assert result["failure_reasons"] == original.to_dict()["failure_reasons"]
    assert "Measurement unavailable" in message(preview)["Subject"]
    assert originals(runtime) == before


def test_rescoring_requires_explicit_restyle_and_preserved_frozen_inputs(runtime):
    request, _, _ = seed(runtime, frozen=False)
    with pytest.raises(PreviewError, match="rescore_requires_restyle"):
        exported(runtime, request, rescore=True)
    with pytest.raises(PreviewError, match="frozen_inputs_missing"):
        exported(runtime, request, restyle=True, rescore=True)
    with pytest.raises(PreviewError, match="identity_invalid"):
        exported(runtime, request, restyle=True, rescore="yes")


def test_cli_rescoring_returns_the_derived_email_and_result_paths(runtime, monkeypatch, capsys):
    from agent_insights_quality.scoring import LEGACY_SCORING_POLICY

    request, _, _ = seed(runtime, scoring_policy=LEGACY_SCORING_POLICY)
    before = originals(runtime)
    monkeypatch.setattr(cli, "_catalog", lambda *args: pytest.fail("Preview started qualification"))
    assert cli.main(
        ["email-preview", "--delivery-id", request.delivery_id, "--restyle", "--rescore"],
        root=ROOT, runtime_factory=lambda _: runtime,
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["rescored"] and output["restyled"]
    assert json.loads(Path(output["result_path"]).read_text())["score"] == 36.4
    assert "36.4" in Path(output["email_html_path"]).read_text(encoding="utf-8")
    assert originals(runtime) == before


def test_legacy_restyle_without_receipt_never_substitutes_current_policy_guide(runtime, monkeypatch):
    from agent_insights_quality.scoring import LEGACY_SCORING_POLICY

    request, original, _ = seed(runtime, scoring_policy=LEGACY_SCORING_POLICY)
    before = originals(runtime)
    monkeypatch.setattr(
        email_preview, "configured_scoring_link",
        lambda *args: pytest.fail("Legacy restyle requested current policy guide"),
    )
    preview = exported(runtime, request, restyle=True)
    assert "scoring_link_publication_required" in preview.blockers
    manifest = json.loads((preview.directory / "manifest.json").read_text())
    assert manifest["links"]["scoring"] is None
    assert f"{original.score:.1f}" in (preview.directory / "email.html").read_text()
    assert originals(runtime) == before


def test_explicit_current_guide_cannot_relabel_a_legacy_restyle(runtime, monkeypatch):
    from agent_insights_quality.scoring import LEGACY_SCORING_POLICY
    from agent_insights_quality.report_links import VerifiedScoringLink

    request, _, _ = seed(runtime, scoring_policy=LEGACY_SCORING_POLICY)
    content = (ROOT / "docs" / "QUALITY_BAR.md").read_bytes()
    link = VerifiedScoringLink(ROOT, RENDERER, fetch=lambda _: content)
    monkeypatch.setattr(email_preview, "VerifiedScoringLink", lambda *args: link)
    with pytest.raises(PreviewError, match="scoring_link_policy_mismatch"):
        exported(runtime, request, restyle=True, scoring_revision=RENDERER)


@pytest.mark.parametrize("mode", ["official", "test", "failure", "failed-test"])
def test_restyle_never_changes_original_private_test_or_failure_routing(runtime, mode):
    request, result, _ = seed(runtime, mode=mode)
    preview = exported(runtime, request, restyle=True)
    mail = message(preview)
    manifest = json.loads((preview.directory / "manifest.json").read_text())
    assert str(mail["To"]) == request.recipient
    assert manifest["mode"] == request.mode
    assert manifest["test_run"] is request.test_run
    if not result.team_report_eligible:
        assert str(mail["To"]) != TEAM_RECIPIENT
        assert "Measurement unavailable" in str(mail["Subject"])


def test_old_request_without_frozen_inputs_is_exact_only_and_does_not_load_catalog(runtime, monkeypatch):
    request, _, _ = seed(runtime, frozen=False)
    monkeypatch.setattr(email_preview, "load_report_context", lambda *a, **k: pytest.fail("Current catalog"))
    preview = exported(runtime, request)
    assert (preview.directory / "email.html").read_bytes() == request.html.encode("utf-8")
    assert "Detailed report unavailable" in (preview.directory / "report.md").read_text()
    assert message(preview).get_body().get_content() == request.html
    assert [part.get_filename() for part in message(preview).iter_attachments()] == ["report.md"]
    manifest = json.loads((preview.directory / "manifest.json").read_text())
    assert manifest["report_kind"] == "frozen_inputs_unavailable_notice"
    assert manifest["measurement_source_revision"] is None
    with pytest.raises(PreviewError, match="frozen_inputs_missing"):
        exported(runtime, request, restyle=True)


def test_exact_preserves_legacy_relative_link_html_without_rewriting_link_notices(runtime):
    html = '<html><body><a href="report.html">Open the original report</a></body></html>'
    request, _, _ = seed(runtime, frozen=False, html=html)
    preview = exported(runtime, request)
    assert message(preview).get_body().get_content() == html


def test_catalog_root_can_move_but_reviewed_unit_text_must_match(runtime):
    request, _, _ = seed(runtime, mutate=lambda value: value["report_context"].update(
        catalog_root=r"Z:\synthetic-original-checkout",
    ))
    assert exported(runtime, request, restyle=True).restyled


def test_changed_reviewed_context_rejects_restyle_but_exact_request_survives(runtime):
    def change(value):
        value["report_context"]["units"][1]["title"] = "An older reviewed title"
    request, _, _ = seed(runtime, mutate=change)
    exact = exported(runtime, request)
    assert (exact.directory / "email.html").read_text(encoding="utf-8") == request.html
    with pytest.raises(PreviewError, match="reviewed_context_changed"):
        exported(runtime, request, restyle=True)


def work_items():
    return {
        "schema_version": "1.0", "status": "available", "snapshot": {
            "report_date": DAY,
            "query_url": "https://dev.azure.com/synthetic/project/_queries/query/"
                         "11111111-1111-1111-1111-111111111111",
            "window": {
                "start": "2026-08-28T17:00:00+00:00", "end": "2026-09-04T17:00:00+00:00",
                "timezone": "America/Los_Angeles", "basis": "initial_lookback",
                "previous_delivery_id": None,
            },
            "active": [{
                "id": 12, "type": "Bug", "state": "Active", "title": "Synthetic retained item",
                "owner": "Synthetic owner",
                "url": "https://dev.azure.com/synthetic/project/_workitems/edit/12",
            }],
            "closed": [],
        },
    }


def legacy_work_items():
    return {
        "status": "available",
        "snapshot": {
            "report_date": DAY, "closed_on": "2026-09-03",
            "active": [{
                "id": 12, "state": "Active", "title": "Synthetic legacy work item",
                "owner": "Synthetic legacy owner",
                "url": "https://dev.azure.com/synthetic/project/_workitems/edit/12",
            }],
            "closed": [{
                "id": 13, "state": "Closed", "title": "Synthetic legacy closed item",
                "owner": "",
                "url": "https://dev.azure.com/synthetic/project/_workitems/edit/13",
            }],
        },
        "text": "Opaque saved legacy text: Synthetic legacy work item\nSaved verbatim, not parsed.",
    }


def seed_legacy(runtime, *, change=None, private_context=None):
    retained = legacy_work_items()
    notes = "\n\nConfigured assessment (intent, not observed serving metadata)\nModel: synthetic assessor"
    if change:
        change(retained)
    request, result, plan = seed(runtime, mutate=lambda value: value.update(
        private_context=retained["text"] + notes if private_context is None else private_context,
    ))
    with runtime.ownership():
        runtime.run(request.delivery_id).save_completed("work-item-context", retained)
    return request, result, plan, retained, notes


def test_legacy_same_run_snapshot_restyles_as_disclosed_table_without_parsing_or_duplicate_text(runtime, monkeypatch):
    request, _, _, retained, notes = seed_legacy(runtime)
    before = originals(runtime)
    actual = email.render_email_content
    contexts = []
    def render(result, **kwargs):
        contexts.append(kwargs["private_context"])
        return actual(result, **kwargs)
    monkeypatch.setattr(email, "render_email_content", render)
    preview = exported(runtime, request, restyle=True)
    html = message(preview).get_body().get_content()
    assert html.count("Synthetic legacy work item") == 1
    assert "Synthetic legacy closed item" in html and "Synthetic legacy owner" in html
    assert "Not recorded" in html and "2026-09-03" in html
    assert "legacy" in html.lower()
    assert "Initial 7-day lookback" not in html
    assert "Since the previous successfully submitted official report snapshot" not in html
    assert "Saved verbatim, not parsed." not in html
    assert "synthetic assessor" not in html and contexts == [notes, notes]
    manifest = json.loads((preview.directory / "manifest.json").read_text())
    assert manifest["work_item_presentation"]["source"] == "same_run_legacy_snapshot"
    assert manifest["work_item_presentation"]["retained_text_prefix_removed"] is True
    assert len(manifest["work_item_presentation"]["checkpoint_sha256"]) == 64
    assert originals(runtime) == before
    assert runtime.run(request.delivery_id).read_completed("work-item-context") == retained


def test_legacy_prefix_removal_is_not_a_global_replacement_or_strip(runtime, monkeypatch):
    retained = legacy_work_items()
    suffix = "\n\nAssessor notes quote retained text:\n" + retained["text"] + "\n  "
    request, *_ = seed_legacy(runtime, private_context=retained["text"] + suffix)
    actual = email.render_email_content
    captured = []
    def render(result, **kwargs):
        captured.append(kwargs["private_context"])
        return actual(result, **kwargs)
    monkeypatch.setattr(email, "render_email_content", render)
    exported(runtime, request, restyle=True)
    assert captured == [suffix, suffix]


@pytest.mark.parametrize("private_context", [
    "Unrelated saved notes", "prefix:" + legacy_work_items()["text"],
    legacy_work_items()["text"] + "not-a-paragraph-boundary",
])
def test_legacy_snapshot_requires_exact_saved_text_prefix(runtime, private_context):
    request, *_ = seed_legacy(runtime, private_context=private_context)
    before = originals(runtime)
    with pytest.raises(PreviewError, match="work_item_context_mismatch"):
        exported(runtime, request, restyle=True)
    assert originals(runtime) == before
    assert not (runtime.directory / "previews").exists()


@pytest.mark.parametrize("corruption", ["date", "closed_on", "url"])
def test_corrupt_legacy_snapshot_does_not_gain_a_guessed_table(runtime, corruption):
    def change(retained):
        if corruption == "date":
            retained["snapshot"]["report_date"] = "2026-09-03"
        elif corruption == "closed_on":
            retained["snapshot"]["closed_on"] = "not-a-date"
        else:
            retained["snapshot"]["active"][0]["url"] = "javascript:alert(1)"
    request, *_ = seed_legacy(runtime, change=change)
    with pytest.raises(QualityError):
        exported(runtime, request, restyle=True)
    assert not (runtime.directory / "previews").exists()


def test_exact_export_does_not_read_or_reinterpret_retained_legacy_context(runtime):
    request, *_ = seed_legacy(runtime, private_context="Not a prefix of the retained text")
    preview = exported(runtime, request)
    assert message(preview).get_body().get_content() == request.html
    manifest = json.loads((preview.directory / "manifest.json").read_text())
    assert manifest["work_item_presentation"] is None


def test_legacy_context_never_falls_back_to_another_run(runtime):
    request, _, _ = seed(runtime)
    with runtime.ownership():
        runtime.run("daily-" + DAY + "-test-1").save_completed("work-item-context", legacy_work_items())
    preview = exported(runtime, request, restyle=True)
    html = message(preview).get_body().get_content()
    assert "Synthetic legacy work item" not in html
    assert "Synthetic frozen private optional context" not in html
    manifest = json.loads((preview.directory / "manifest.json").read_text())
    assert manifest["work_item_presentation"]["source"] == "legacy_snapshot_unavailable"


def test_retained_legacy_unavailable_context_preserves_notes_without_fabricating_empty_results(runtime):
    request, _, _ = seed(runtime)
    with runtime.ownership():
        runtime.run(request.delivery_id).save_completed("work-item-context", {
            "status": "unavailable", "code": "work_item_unavailable", "text": None,
        })
    preview = exported(runtime, request, restyle=True)
    html = message(preview).get_body().get_content()
    assert "Unavailable." in html and "This is not an empty result." in html
    assert "Synthetic frozen private optional context" not in html
    assert "None in this snapshot." not in html
    manifest = json.loads((preview.directory / "manifest.json").read_text())
    assert manifest["work_item_presentation"]["retained_text_prefix_removed"] is False


def test_legacy_checkpoint_symlink_is_rejected_before_local_export(runtime, tmp_path):
    request, *_ = seed_legacy(runtime)
    path = runtime.run(request.delivery_id)._path("completed", "work-item-context")
    destination = tmp_path / "synthetic-legacy-copy.json"
    destination.write_bytes(path.read_bytes())
    path.unlink()
    try:
        os.symlink(destination, path)
    except OSError:
        pytest.skip("Symlink creation privilege unavailable")
    before = destination.read_bytes()
    with pytest.raises(StateError, match="state_path_invalid"):
        exported(runtime, request, restyle=True)
    assert destination.read_bytes() == before
    assert not (runtime.directory / "previews").exists()


def test_restyle_uses_frozen_work_item_snapshot_and_exact_same_result_for_both_renderings(runtime, monkeypatch):
    snapshot = work_items()
    request, result, _ = seed(runtime, mutate=lambda value: value.update(work_item_context=snapshot))
    # A complete frozen typed context never consults a later/legacy checkpoint.
    with runtime.ownership():
        runtime.run(request.delivery_id).save_completed("work-item-context", {"invalid": "not consulted"})
    actual = email.render_email_content
    calls = []
    def render(result, **kwargs):
        calls.append((result.to_dict(), deepcopy(kwargs["work_item_context"]), kwargs["details_href"]))
        return actual(result, **kwargs)
    monkeypatch.setattr(email, "render_email_content", render)
    preview = exported(runtime, request, restyle=True)
    assert calls == [(result.to_dict(), snapshot, "report.html"), (result.to_dict(), snapshot, None)]
    html = message(preview).get_body().get_content()
    assert "Synthetic retained item" in html and "Synthetic owner" in html
    assert 'href="https://dev.azure.com/synthetic/project/_workitems/edit/12"' in html
    assert "Initial 7-day lookback" in html


@pytest.mark.parametrize("corruption", ["shape", "date", "url"])
def test_frozen_work_items_are_validated_even_for_exact_exports(runtime, corruption):
    snapshot = work_items()
    if corruption == "shape":
        snapshot["unapproved"] = True
    elif corruption == "date":
        snapshot["snapshot"]["report_date"] = "2026-09-03"
    else:
        snapshot["snapshot"]["active"][0]["url"] = "javascript:alert(1)"
    request, _, _ = seed(runtime, mutate=lambda value: value.update(work_item_context=snapshot))
    with pytest.raises(QualityError):
        exported(runtime, request)
    assert not (runtime.directory / "previews").exists()


@pytest.mark.parametrize("field,value", [
    ("test_run", False), ("rerun", True), ("report_date", "2026-09-03"),
    ("source_revision", "not-a-revision"), ("source_revision", "d" * 40),
    ("recipient", "someone-else@example.invalid"), ("recipient", TEAM_RECIPIENT),
    ("region_display", "unreviewed-region"), ("private_context", {}),
    ("warnings", ["invented_warning"]), ("report_context", {}),
])
def test_corrupt_or_mismatched_frozen_fields_are_rejected_without_export(runtime, field, value):
    request, _, _ = seed(runtime, mutate=lambda inputs: inputs.update({field: value}))
    with pytest.raises(QualityError):
        exported(runtime, request, restyle=True)
    assert not (runtime.directory / "previews").exists()


def test_corrupt_result_cannot_supply_invented_score(runtime):
    request, _, _ = seed(runtime, mutate=lambda inputs: inputs["report"].update(score=100))
    with pytest.raises(QualityError, match="public_projection_invalid"):
        exported(runtime, request)
    assert not (runtime.directory / "previews").exists()


def test_wrong_frozen_plan_is_not_accepted_as_current_run(runtime):
    def change(inputs):
        other_plan = (PlannedUnit(UnitId("weather-agent", "issue-001"), "issue-001"),)
        inputs["report"] = aggregate_results(other_plan, (UnitResult(other_plan[0].unit_id),)).to_dict()
        inputs["report_context"] = load_report_context(ROOT, allowed_units=other_plan).to_private_dict()
    request, _, _ = seed(runtime, mutate=change)
    with pytest.raises(PreviewError, match="delivery_mismatch"):
        exported(runtime, request, restyle=True)


@pytest.mark.parametrize("identity", ["../daily", r"..\daily", "a/b", "con", "nul", "", "a" * 81])
def test_unsafe_delivery_ids_fail_before_writes(runtime, identity):
    with runtime.ownership(), pytest.raises(QualityError):
        export_email_preview(runtime, identity, root=ROOT)
    assert not (runtime.directory / "previews").exists()


@pytest.mark.parametrize("markup", [
    '<a href="javascript:alert(1)">unsafe</a>',
    '<a href="file:///private/secret">unsafe</a>',
    '<a href="cid:missing">unsafe</a>',
    '<a href="../escape.html">unsafe</a>',
    '<a href="https://user:secret@example.invalid">unsafe</a>',
    '<a href="//example.invalid">unsafe</a>',
    '<a href="java&#115;cript:alert(1)">unsafe</a>',
    '<img src="https://example.invalid/tracker">',
    '<script>alert(1)</script>',
    '<p onclick="alert(1)">unsafe</p>',
    '<style>@import "https://example.invalid/style";</style>',
    '<p style="background:url(https://example.invalid)">unsafe</p>',
    '<!--[if mso]><a href="javascript:alert(1)">unsafe</a><![endif]-->',
])
def test_unsafe_prepared_html_is_rejected_not_sanitized(runtime, markup):
    request, _, _ = seed(runtime, frozen=False, html="<html><body>" + markup + "</body></html>")
    before = originals(runtime)
    with pytest.raises(PreviewError, match="unsafe"):
        exported(runtime, request)
    assert originals(runtime) == before
    assert not (runtime.directory / "previews").exists()


def test_restyle_rejects_relative_links_from_the_mime_renderer(runtime, monkeypatch):
    request, _, _ = seed(runtime)
    monkeypatch.setattr(email, "render_email_content", lambda *a, **k: (
        "Synthetic rendered subject", '<html><body><a href="report.html">Details</a></body></html>',
    ))
    with pytest.raises(PreviewError, match="unsafe_link"):
        exported(runtime, request, restyle=True)
    assert not (runtime.directory / "previews").exists()


def test_export_requires_normal_runtime_ownership_and_daily_profile(runtime):
    request, _, _ = seed(runtime)
    with pytest.raises(StateError, match="state_not_owned"):
        export_email_preview(runtime, request.delivery_id, root=ROOT)
    staging = RuntimeStore("staging", root=runtime.root)
    with staging.ownership(), pytest.raises(PreviewError, match="daily_only"):
        export_email_preview(staging, request.delivery_id, root=ROOT)


def test_active_daily_writer_blocks_preview_without_touching_run(runtime, capsys):
    request, _, _ = seed(runtime)
    before = originals(runtime)
    with runtime.ownership():
        assert cli.main(
            ["email-preview", "--delivery-id", request.delivery_id], root=ROOT,
            runtime_factory=lambda profile: RuntimeStore(profile, root=runtime.root),
        ) == 2
    assert "state_owned" in capsys.readouterr().err
    assert originals(runtime) == before
    assert not (runtime.directory / "previews").exists()


def test_export_is_idempotent_and_recovers_partial_bundle(runtime):
    request, _, _ = seed(runtime)
    first = exported(runtime, request)
    before = {path: path.read_bytes() for path in first.directory.iterdir()}
    assert exported(runtime, request) == first
    (first.directory / "email.eml").unlink()
    (first.directory / "manifest.json").unlink()
    assert exported(runtime, request) == first
    assert {path: path.read_bytes() for path in first.directory.iterdir()} == before


def test_bundle_conflict_never_overwrites_or_repairs_other_files(runtime):
    request, _, _ = seed(runtime)
    first = exported(runtime, request)
    (first.directory / "email.eml").write_bytes(b"conflicting bytes")
    (first.directory / "email.html").unlink()
    before = {path: path.read_bytes() for path in first.directory.iterdir()}
    with pytest.raises(StateConflict):
        exported(runtime, request)
    assert {path: path.read_bytes() for path in first.directory.iterdir()} == before


def test_manifest_is_committed_last_and_atomic_failure_is_recoverable(runtime, monkeypatch):
    request, _, _ = seed(runtime)
    write = email_preview._atomic_write
    written = []
    def fail_manifest(path, content):
        written.append(path.name)
        if path.name == "manifest.json":
            raise CheckpointError()
        write(path, content)
    monkeypatch.setattr(email_preview, "_atomic_write", fail_manifest)
    with pytest.raises(CheckpointError):
        exported(runtime, request)
    assert written == ["email.html", "email.eml", "report.md", "report.html", "manifest.json"]
    assert not list((runtime.directory / "previews").rglob("manifest.json"))
    monkeypatch.setattr(email_preview, "_atomic_write", write)
    assert (exported(runtime, request).directory / "manifest.json").is_file()


@pytest.mark.parametrize("target", ["preview-directory", "preview-file", "request", "frozen"])
def test_symlink_paths_are_rejected_without_writing_through(runtime, tmp_path, target):
    request, _, _ = seed(runtime)
    preview = exported(runtime, request)
    outside = tmp_path / "synthetic-outside"
    outside.mkdir()
    if target == "preview-directory":
        path = runtime.directory / "previews" / request.delivery_id
        for child in preview.directory.iterdir():
            child.unlink()
        preview.directory.rmdir()
        path.rmdir()
        destination = outside
    else:
        path = {
            "preview-file": preview.directory / "email.html",
            "request": runtime.outbox("email")._path("progress", request.delivery_id),
            "frozen": runtime.run(request.delivery_id)._path("completed", "delivery-inputs"),
        }[target]
        destination = outside / "synthetic.json"
        destination.write_bytes(path.read_bytes())
        path.unlink()
    try:
        os.symlink(destination, path, target_is_directory=target == "preview-directory")
    except OSError:
        pytest.skip("Symlink creation privilege unavailable")
    before = {file: file.read_bytes() for file in outside.iterdir() if file.is_file()}
    with pytest.raises(StateError, match="state_path_invalid"):
        exported(runtime, request)
    assert {file: file.read_bytes() for file in outside.iterdir() if file.is_file()} == before


@pytest.mark.parametrize("restyle", [False, True])
def test_cli_preview_constructs_no_ports_integrations_claims_or_public_sinks(runtime, capsys, monkeypatch, restyle):
    request, _, _ = seed(runtime)
    before = originals(runtime)
    def forbidden(*args, **kwargs):
        pytest.fail("Preview constructed a live/public/send boundary")
    monkeypatch.setattr(email, "claim_email", forbidden)
    monkeypatch.setattr(email, "record_email_outcome", forbidden)
    monkeypatch.setattr(cli, "_catalog", forbidden)
    args = ["email-preview", "--delivery-id", request.delivery_id]
    if restyle:
        args.append("--restyle")
    assert cli.main(
        args, root=ROOT, runtime_factory=lambda profile: runtime,
        ports=forbidden, integrations=forbidden, metrics_factory=forbidden,
    ) == 0
    output = capsys.readouterr()
    value = json.loads(output.out)
    assert not output.err
    assert value["status"] == "local_preview" and value["restyled"] is restyle
    assert Path(value["manifest_path"]).is_relative_to(runtime.directory / "previews")
    assert PRIVATE not in output.out and request.subject not in output.out
    assert not (runtime.directory / "outboxes" / "publication").exists()
    assert originals(runtime) == before


def test_cli_does_not_offer_output_or_recipient_overrides():
    for arguments in (
        ["--output", r"C:\synthetic"],
        ["--recipient", "other@example.invalid"],
        ["--runtime-root", r"C:\synthetic"],
    ):
        with pytest.raises(SystemExit):
            cli.parser().parse_args(["email-preview", "--delivery-id", "synthetic", *arguments])


def test_renderer_provenance_records_actual_module_bytes_separately_from_git_head(monkeypatch):
    monkeypatch.setattr(email_preview.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=RENDERER + "\n",
    ))
    value = _actual_provenance(ROOT)
    assert value["source_revision"] == RENDERER
    assert "email_preview.py" in value["module_sha256"]
    assert all(len(digest) == 64 for digest in value["module_sha256"].values())
    assert len(value["content_sha256"]) == 64


def test_changed_renderer_gets_distinct_export_without_modifying_first(runtime, monkeypatch):
    request, _, _ = seed(runtime)
    first = exported(runtime, request)
    before = {path: path.read_bytes() for path in first.directory.iterdir()}
    provenance = deepcopy(email_preview._renderer_provenance(ROOT))
    provenance["module_sha256"]["synthetic.py"] = "d" * 64
    monkeypatch.setattr(email_preview, "_renderer_provenance", lambda root: provenance)
    second = exported(runtime, request)
    assert first.directory != second.directory
    assert {path: path.read_bytes() for path in first.directory.iterdir()} == before
