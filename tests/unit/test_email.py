from pathlib import Path

import pytest

from agent_insights_quality.email import (
    EmailError,
    TEAM_RECIPIENT,
    claim_email,
    prepare_email,
    read_email,
    record_email_outcome,
)
from agent_insights_quality.results import (
    ExclusionReason, PlannedUnit, UnitId, UnitResult, aggregate_results,
)
from agent_insights_quality.report_context import ReportContextError, load_report_context
from agent_insights_quality.state import RuntimeStore, StateConflict, StateError


def result(*, failed=False):
    unit = UnitId("weather-agent", "issue-001")
    plan = (PlannedUnit(unit, "issue-001"),)
    return aggregate_results(plan, () if failed else (UnitResult(unit),)), plan


def prepare(outbox, *, failed=False, **kwargs):
    quality, plan = result(failed=failed)
    return prepare_email(
        outbox, "synthetic-delivery", quality, allowed_units=plan,
        report_date="2026-09-04", **kwargs,
    )


def test_official_test_and_failure_recipient_isolation(tmp_path):
    for mode in ("official", "test", "failure"):
        runtime = RuntimeStore("daily", root=tmp_path / mode)
        with runtime.ownership():
            request = prepare(
                runtime.outbox("email"), failed=mode == "failure",
                test_run=mode == "test", rerun=1 if mode == "test" else 0,
                test_recipient="private-test@example.test",
                failure_recipient="private-failure@example.test",
            )
        assert request.recipient == {
            "official": TEAM_RECIPIENT, "test": "private-test@example.test",
            "failure": "private-failure@example.test",
        }[mode]
        assert request.mode == mode
        assert ("TEST" in request.subject) == (mode == "test")


@pytest.mark.parametrize("kwargs", [
    {"test_run": True, "rerun": 0, "test_recipient": "test@example.test"},
    {"test_run": True, "rerun": 1},
    {"test_run": True, "rerun": 1, "test_recipient": TEAM_RECIPIENT},
    {"test_run": True, "rerun": 1, "test_recipient": "Team@example.test", "team_recipient": "team@example.test"},
    {"test_run": False, "rerun": 1},
    {"test_run": True, "rerun": 1, "test_recipient": "one@example.test,two@example.test"},
    {"test_run": True, "rerun": 1, "test_recipient": "one@example.test\r\nBcc: two@example.test"},
    {"failed": True, "failure_recipient": TEAM_RECIPIENT},
])
def test_invalid_delivery_mode_never_creates_a_request(tmp_path, kwargs):
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        with pytest.raises(EmailError):
            prepare(runtime.outbox("email"), **kwargs)
    assert not list(tmp_path.rglob("*.json"))


def test_claim_before_send_replay_content_conflict_and_acceptance_not_delivery(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        box = runtime.outbox("email")
        request = prepare(box)
        assert prepare(box) == request
        with pytest.raises(StateConflict):
            prepare(box, private_context="Changed content is a different delivery.")
        with pytest.raises(EmailError, match="claim_conflict"):
            record_email_outcome(
                box, request.delivery_id, claim_id="app-1",
                outcome="accepted", provider_result={"status": "accepted"},
            )
        assert claim_email(box, request.delivery_id, claim_id="app-1") == request
        with pytest.raises(EmailError, match="reconciliation_required"):
            claim_email(box, request.delivery_id, claim_id="app-1")
        with pytest.raises(EmailError, match="claim_conflict"):
            claim_email(box, request.delivery_id, claim_id="app-2")
        accepted = record_email_outcome(
            box, request.delivery_id, claim_id="app-1", outcome="accepted",
            provider_result={"status": "accepted", "id": "synthetic-provider-id"},
        )
        assert not accepted.inbox_delivery_confirmed
        assert prepare(box) == request
        with pytest.raises(EmailError, match="already_finalized"):
            claim_email(box, request.delivery_id, claim_id="app-1")
        delivered = record_email_outcome(
            box, request.delivery_id, claim_id="app-1", outcome="delivered",
            provider_result={"status": "delivered", "receipt": "synthetic-receipt"},
            reconciliation=True,
        )
        assert delivered.inbox_delivery_confirmed
        assert read_email(box, request.delivery_id) == delivered


def test_ambiguous_send_requires_reconciliation_and_keeps_exact_request(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        box = runtime.outbox("email")
        request = prepare(box)
        claim_email(box, request.delivery_id, claim_id="app-1")
        unknown = record_email_outcome(
            box, request.delivery_id, claim_id="app-1", outcome="unknown",
            provider_result={"status": "timeout"},
        )
        assert read_email(box, request.delivery_id) == unknown
        with pytest.raises(EmailError, match="reconciliation_required"):
            record_email_outcome(
                box, request.delivery_id, claim_id="app-1",
                outcome="accepted", provider_result={"status": "accepted"},
            )
        reconciled = record_email_outcome(
            box, request.delivery_id, claim_id="app-1", outcome="accepted",
            provider_result={"status": "accepted"}, reconciliation=True,
        )
        assert reconciled.request == request
        assert not reconciled.inbox_delivery_confirmed


def test_private_context_is_escaped_and_test_mode_only_writes_private_outbox(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        request = prepare(
            runtime.outbox("email"), test_run=True, rerun=4,
            test_recipient="test@example.test",
            private_context="<script>synthetic private work-item text</script>",
            warnings=("work_item_unavailable",),
        )
    assert "<script>" not in request.html
    assert "&lt;script&gt;synthetic private" in request.html
    files = list(tmp_path.rglob("*.json"))
    assert len(files) == 1
    assert files[0].relative_to(runtime.directory).parts[:3] == ("outboxes", "email", "progress")


def test_no_claim_can_escape_when_checkpoint_fails_or_ownership_is_absent(tmp_path, monkeypatch):
    runtime = RuntimeStore("daily", root=tmp_path)
    box = runtime.outbox("email")
    with pytest.raises(StateError, match="state_not_owned"):
        prepare(box)
    with runtime.ownership():
        request = prepare(box)
        def fail(*args):
            raise StateError("state_checkpoint_failed")
        monkeypatch.setattr(box, "save_progress", fail)
        with pytest.raises(StateError, match="checkpoint_failed"):
            claim_email(box, request.delivery_id, claim_id="app-1")
    assert read_email(box, request.delivery_id).status == "prepared"


def test_terminal_outcome_replay_is_idempotent_but_conflicts_are_not_overwritten(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        box = runtime.outbox("email")
        request = prepare(box)
        claim_email(box, request.delivery_id, claim_id="app-1")
        kwargs = dict(
            claim_id="app-1", outcome="rejected", provider_result={"status": "rejected"},
        )
        original = record_email_outcome(box, request.delivery_id, **kwargs)
        assert record_email_outcome(box, request.delivery_id, **kwargs) == original
        with pytest.raises(EmailError, match="outcome_conflict"):
            record_email_outcome(
                box, request.delivery_id, claim_id="app-1",
                outcome="accepted", provider_result={"status": "accepted"}, reconciliation=True,
            )


@pytest.mark.parametrize("failed", [False, True])
def test_email_uses_reviewed_context_and_exact_metadata_including_private_failure(tmp_path, failed):
    quality, plan = result(failed=failed)
    context = load_report_context(Path(__file__).resolve().parents[2], allowed_units=plan)
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        box = runtime.outbox("email")
        request = prepare_email(
            box, "context-delivery", quality, allowed_units=plan, report_date="2026-09-04",
            test_run=True, rerun=1, test_recipient="test@example.test",
            report_context=context, region_display="Sweden Central", source_revision="a" * 40,
            private_context="synthetic private work-item context",
            warnings=("work_item_unavailable",),
        )
        assert claim_email(box, request.delivery_id, claim_id="native-app") == request
    assert "Unsupported factual answer" in request.html
    assert "agents/weather-agent/issues/issue-001/traffic.json" not in request.html
    assert "What needs improvement" in request.html
    assert "TEST RUN" in request.html
    assert "Report date: 2026-09-04" in request.html and "2026-09-04" in request.subject
    assert "Region: Sweden Central" in request.html
    assert "Source commit: " + "a" * 40 in request.html
    assert "synthetic private work-item context" in request.html
    assert "Optional work-item context is unavailable." in request.html
    assert read_email(runtime.outbox("email"), request.delivery_id).request == request
    assert len(list(tmp_path.rglob("*.json"))) == 1


@pytest.mark.parametrize("metadata", [
    {"region_display": "Sweden Central"},
    {"source_revision": "a" * 40},
    {"region_display": "", "source_revision": "a" * 40},
    {"region_display": "Sweden Central", "source_revision": "main"},
])
def test_incomplete_or_invalid_metadata_does_not_prepare_a_request(tmp_path, metadata):
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership(), pytest.raises((EmailError, ReportContextError)):
        prepare(runtime.outbox("email"), **metadata)
    assert not list(tmp_path.rglob("*.json"))


def test_metadata_changes_cannot_rewrite_prepared_or_claimed_email(tmp_path):
    runtime = RuntimeStore("daily", root=tmp_path)
    metadata = {"region_display": "SwedenCentral", "source_revision": "a" * 40}
    with runtime.ownership():
        box = runtime.outbox("email")
        original = prepare(box, **metadata)
        assert prepare(box, **metadata) == original
        for change in ({"source_revision": "b" * 40}, {"region_display": "Sweden Central"}):
            with pytest.raises(StateConflict):
                prepare(box, **{**metadata, **change})
        claim_email(box, original.delivery_id, claim_id="native-app")
        with pytest.raises(StateConflict):
            prepare(box, **{**metadata, "source_revision": "b" * 40})
    assert read_email(box, original.delivery_id).request == original


@pytest.mark.parametrize("excluded", [0, 1, 2, 3])
def test_coverage_labels_are_hidden_without_changing_private_failure_routing(tmp_path, excluded):
    plan = (PlannedUnit(UnitId("weather-agent", "v0")),) + tuple(
        PlannedUnit(UnitId("weather-agent", f"issue-{index:03d}"), f"issue-{index:03d}")
        for index in range(1, 4)
    )
    quality = aggregate_results(plan, tuple(
        UnitResult(unit.unit_id, exclusion_reasons=(ExclusionReason.INCOMPLETE_EVIDENCE,)
                   if index < excluded else ())
        for index, unit in enumerate(plan)
    ))
    runtime = RuntimeStore("daily", root=tmp_path)
    with runtime.ownership():
        request = prepare_email(
            runtime.outbox("email"), "coverage-email", quality, allowed_units=plan,
            report_date="2026-09-05", failure_recipient="personal@example.test",
        )
    assert "Full" not in request.subject + request.html
    assert "Partial" not in request.subject + request.html
    if excluded > 2:
        assert request.mode == "failure"
        assert request.recipient == "personal@example.test"
        assert "Measurement unavailable" in request.subject
        assert "/100" not in request.subject
    else:
        assert request.mode == "official"
        assert request.recipient == TEAM_RECIPIENT
        assert "0.0/100" in request.subject
    assert f"excluded whole units: {excluded}" in request.html
    assert read_email(runtime.outbox("email"), "coverage-email").status == "prepared"
