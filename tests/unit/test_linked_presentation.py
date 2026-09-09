"""Linked presentation delivery is separate from the original measured email."""

from dataclasses import asdict
from html.parser import HTMLParser
from pathlib import Path

import pytest

from agent_insights_quality import cli
from agent_insights_quality.email import claim_email, read_email, record_email_outcome
from agent_insights_quality.errors import QualityError
from agent_insights_quality.linked_presentation import (
    LinkedPresentationError, prepare_linked_test_presentation, presentation_email_outbox,
    validate_presentation_email,
)
from agent_insights_quality.private_publication import PrivateReportOutbox
from agent_insights_quality.report_access import ReportAccessError
from agent_insights_quality.settings import AssessmentSettings
from agent_insights_quality.state import RuntimeStore, StateError
from test_email_preview import ROOT, SOURCE, seed
from test_private_publication import ENVIRONMENT, FakeBlob
from test_runner import fake_storage


class Anchors(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.current = [dict(attrs)["href"], ""]

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data

    def handle_endtag(self, tag):
        if tag == "a":
            self.links.append(tuple(self.current))
            self.current = None


@pytest.fixture
def source(tmp_path, monkeypatch):
    fake_storage(monkeypatch)
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    request, result, plan = seed(runtime, status="accepted", categorized=True)
    with runtime.ownership():
        records = runtime.run(request.delivery_id)
        records.save_completed("environment", asdict(ENVIRONMENT))
        records.save_completed("assessment-settings", AssessmentSettings().to_dict())
        records.save_artifact("results/final", result.to_dict())
        records.save_progress("quality-result", {
            "artifact": "results/final", "source_revision": SOURCE,
        })
        publisher = PrivateReportOutbox(runtime, request.delivery_id)
        publisher.prepare(
            ROOT, result, allowed_units=plan, environment=ENVIRONMENT,
            source_revision=SOURCE, report_date=request.report_date, test_run=True,
        )
        client = FakeBlob()
        assert publisher.flush(client)["status"] == "delivered"
    return runtime, request, publisher, client


def test_linked_email_restores_both_circled_link_surfaces_without_changing_source(source):
    runtime, original, publisher, client = source
    source_request = publisher.request()
    before = {
        path: path.read_bytes()
        for directory in (runtime.directory / "runs", runtime.directory / "outboxes")
        for path in directory.rglob("*") if path.is_file()
    }
    with runtime.ownership():
        output = prepare_linked_test_presentation(
            runtime, original.delivery_id, root=ROOT, blob_factory=lambda _: client,
        )
        outbox = presentation_email_outbox(runtime, output["presentation_id"])
        record = read_email(outbox, original.delivery_id)
        assert record.status == "prepared" and record.request.recipient == original.recipient
        html = record.request.html
        links = Anchors(html).links
        report_links = [(href, text) for href, text in links if "/quality-artifacts/" in href]
        assert report_links
        assert any(text == "View report" for _, text in report_links)
        assert any("weather-agent / issue-002" in text for _, text in report_links)
        assert any("weather-agent / v0" in text for _, text in report_links)
        assert all(
            f"/{output['presentation_id']}/agents/weather-agent/report.html?" in href
            for href, _ in report_links
        )
        assert "Download MD" not in html and "Attached report.md:" not in html
        assert 'href="report.html' not in html
        assert record.request.subject.startswith("[PRESENTATION UPDATE] [TEST]")
        report = Path(output["report_html_path"]).read_text(encoding="utf-8")
        assert ">Issue definition</th>" in report and ">Notes</th>" not in report
        assert not output["measurement_changed"] and not output["send_performed"]
        assert "sig=" not in str(output)
        assert read_email(runtime.outbox("email"), original.delivery_id).request == original
        assert publisher.request() == source_request
    assert all(path.read_bytes() == content for path, content in before.items())


def test_linked_delivery_has_its_own_claim_and_outcome_and_replay_is_not_resend(source):
    runtime, original, _, client = source
    with runtime.ownership():
        output = prepare_linked_test_presentation(
            runtime, original.delivery_id, root=ROOT, blob_factory=lambda _: client,
        )
        outbox = presentation_email_outbox(runtime, output["presentation_id"])
        claim = claim_email(outbox, original.delivery_id, claim_id="presentation-app")
        assert claim.report_access
        record_email_outcome(
            outbox, original.delivery_id, claim_id="presentation-app",
            outcome="accepted", provider_result={"statusCode": 202},
        )
        writes, signings = len(client.writes), len(client.signings)

        def forbidden(_):
            pytest.fail("Completed presentation replay must not create another provider")

        replay = prepare_linked_test_presentation(
            runtime, original.delivery_id, root=ROOT, blob_factory=forbidden,
        )
        assert replay["status"] == "accepted"
        assert len(client.writes) == writes and len(client.signings) == signings
        assert read_email(runtime.outbox("email"), original.delivery_id).request == original


def test_missing_publication_does_not_fall_back_to_attachment_or_prepare_email(source):
    runtime, original, _, _ = source
    client = FakeBlob()
    client.private = False
    with runtime.ownership():
        with pytest.raises(QualityError, match="private_report_container_not_private|publication_incomplete"):
            prepare_linked_test_presentation(
                runtime, original.delivery_id, root=ROOT, blob_factory=lambda _: client,
            )
    assert client.closed
    assert not list((runtime.directory / "outboxes").glob("pemail-*/progress/*.json"))


@pytest.mark.parametrize("state", ["prepared", "claimed", "unknown", "rejected"])
def test_presentation_cannot_bypass_an_unfinished_or_rejected_source_test(tmp_path, state):
    runtime = RuntimeStore("daily", root=tmp_path / "private")
    original, _, _ = seed(runtime, status=state)
    with runtime.ownership():
        with pytest.raises(LinkedPresentationError, match="completed_test"):
            prepare_linked_test_presentation(runtime, original.delivery_id, root=ROOT)


def test_presentation_requires_runtime_ownership_and_strict_selector(source):
    runtime, original, _, _ = source
    with pytest.raises(StateError, match="state_not_owned"):
        prepare_linked_test_presentation(runtime, original.delivery_id, root=ROOT)
    for selector in ("", "../email", "A" * 64, "a" * 63):
        with pytest.raises(LinkedPresentationError, match="identity_invalid"):
            presentation_email_outbox(runtime, selector)


def test_cli_presentation_options_are_explicit_and_do_not_offer_new_measurement_routing():
    parsed = cli.parser().parse_args([
        "prepare-test-presentation", "--delivery-id", "daily-2031-02-03-test-1",
    ])
    assert not hasattr(parsed, "to_address") and not hasattr(parsed, "rerun")
    claimed = cli.parser().parse_args([
        "email-claim", "--delivery-id", parsed.delivery_id, "--claim-id", "app",
        "--presentation-id", "a" * 64,
    ])
    assert claimed.presentation_id == "a" * 64


def test_selected_presentation_cannot_claim_a_different_access_record(source):
    runtime, original, _, client = source
    with runtime.ownership():
        output = prepare_linked_test_presentation(
            runtime, original.delivery_id, root=ROOT, blob_factory=lambda _: client,
        )
        outbox = presentation_email_outbox(runtime, output["presentation_id"])
        validate_presentation_email(outbox, original.delivery_id, output["presentation_id"])
        with pytest.raises(LinkedPresentationError, match="binding_invalid"):
            validate_presentation_email(outbox, original.delivery_id, "f" * 64)


def test_cli_claim_and_outcome_target_the_sidecar_not_the_original_email(source, capsys):
    import json

    runtime, original, _, client = source
    with runtime.ownership():
        output = prepare_linked_test_presentation(
            runtime, original.delivery_id, root=ROOT, blob_factory=lambda _: client,
        )
    def factory(profile):
        return RuntimeStore(profile, root=runtime.root)
    assert cli.main([
        "email-claim", "--delivery-id", original.delivery_id, "--claim-id", "native-app",
        "--presentation-id", output["presentation_id"],
    ], root=ROOT, runtime_factory=factory) == 0
    claimed = json.loads(capsys.readouterr().out)
    packet = json.loads(Path(claimed["request_path"]).read_text(encoding="utf-8"))
    assert packet["subject"].startswith("[PRESENTATION UPDATE]")
    assert packet["recipient"] == original.recipient
    receipt = runtime.root / "native-result.json"
    receipt.write_text('{"statusCode":202}', encoding="utf-8")
    assert cli.main([
        "email-result", "--delivery-id", original.delivery_id, "--claim-id", "native-app",
        "--presentation-id", output["presentation_id"],
        "--outcome", "accepted", "--result-file", str(receipt),
    ], root=ROOT, runtime_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "accepted"
    assert read_email(runtime.outbox("email"), original.delivery_id).request == original


@pytest.mark.parametrize("state", ["prepared", "claimed", "unknown"])
def test_renderer_changes_cannot_bypass_outstanding_presentation_delivery(source, monkeypatch, state):
    from agent_insights_quality import private_publication

    runtime, original, _, client = source
    with runtime.ownership():
        first = prepare_linked_test_presentation(
            runtime, original.delivery_id, root=ROOT, blob_factory=lambda _: client,
        )
        outbox = presentation_email_outbox(runtime, first["presentation_id"])
        if state != "prepared":
            claim_email(outbox, original.delivery_id, claim_id="first-send")
        if state == "unknown":
            record_email_outcome(
                outbox, original.delivery_id, claim_id="first-send",
                outcome="unknown", provider_result={"status": "ambiguous"},
            )
        renderer = private_publication.render_private_markdown

        def updated(*args, **kwargs):
            return renderer(*args, **kwargs) + "\nPresentation-only formatting revision.\n"

        monkeypatch.setattr(private_publication, "render_private_markdown", updated)
        writes, signings = len(client.writes), len(client.signings)
        with pytest.raises(LinkedPresentationError, match="reconciliation_required"):
            prepare_linked_test_presentation(
                runtime, original.delivery_id, root=ROOT, blob_factory=lambda _: client,
            )
        assert len(client.writes) == writes and len(client.signings) == signings
        assert read_email(outbox, original.delivery_id).status == state
        active = runtime.outbox("presentation-deliveries").read(original.delivery_id)
        assert active["presentation_id"] == first["presentation_id"]


def test_interrupted_signing_requires_and_resumes_an_explicit_access_revision(source):
    runtime, original, _, client = source
    client.sign_error = OSError("Synthetic signing reply lost")
    with runtime.ownership():
        with pytest.raises(OSError):
            prepare_linked_test_presentation(
                runtime, original.delivery_id, root=ROOT, blob_factory=lambda _: client,
            )
        assert len(client.signings) == 1
        client.sign_error = None
        with pytest.raises(ReportAccessError, match="interrupted_needs_new_revision"):
            prepare_linked_test_presentation(
                runtime, original.delivery_id, root=ROOT, blob_factory=lambda _: client,
            )
        assert len(client.signings) == 1
        resumed = prepare_linked_test_presentation(
            runtime, original.delivery_id, root=ROOT, blob_factory=lambda _: client,
            access_revision="explicit-recovery",
        )
        assert len(client.signings) == 2
        outbox = presentation_email_outbox(runtime, resumed["presentation_id"])
        request = read_email(outbox, original.delivery_id).request
        assert request.report_access["record_key"].endswith("/access/explicit-recovery")
        assert prepare_linked_test_presentation(
            runtime, original.delivery_id, root=ROOT, blob_factory=lambda _: client,
        ) == resumed
        assert len(client.signings) == 2
        with pytest.raises(LinkedPresentationError, match="access_revision_frozen"):
            prepare_linked_test_presentation(
                runtime, original.delivery_id, root=ROOT, blob_factory=lambda _: client,
                access_revision="do-not-rewrite-prepared-mail",
            )
